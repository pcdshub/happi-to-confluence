import inspect
import json
import logging
import os
from typing import List, Tuple

import jinja2
import numpydoc
import numpydoc.docscrape
import pcdsutils.utils
import requests
import requests.exceptions
from atlassian import Confluence

logging.basicConfig(level="INFO")

logger = logging.getLogger(__name__)
logger.setLevel("DEBUG")

# space = "PCDS"
SPACE = "~klauer"
DOCUMENTATION_ROOT_TITLE = "Typhos Documentation Root"
PAGE_TITLE_MARKER = " (Typhos)"
USER_PAGE_SUFFIX = " - Notes"
RELATED_TITLE_SKIPS = (
    lambda title, page: PAGE_TITLE_MARKER in title,
    lambda title, page: "checkout" in title.lower(),
    lambda title, page: HAPPI_TO_CONFLUENCE_LABEL in page["labels"]
)
HAPPI_TO_CONFLUENCE_LABEL = "happi-to-confluence"
NO_OVERWRITE_LABEL = "no-overwrite"


def wrap_html(html):
    return f"""\
<ac:structured-macro ac:name="html">
  <ac:plain-text-body><![CDATA[{html}]]></ac:plain-text-body>
</ac:structured-macro>
"""


def get_md_for_confluence(md):
    def get_value(value):
        if isinstance(value, list):
            if len(value) == 1:
                return get_value(value[0])
            return [get_value(v) for v in value]
        return str(value)

    return {key: get_value(value) for key, value in md.items()}


def create_client() -> Confluence:
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {os.environ['CONFLUENCE_TOKEN']}"

    client = Confluence(
        (
            os.environ.get("CONFLUENCE_URL", "") or
            "https://confluence-test02.slac.stanford.edu"
        ),
        session=s,
    )
    return client


class NamedTemplate:
    filename: str
    titles: List[jinja2.Template]
    template: jinja2.Template
    labels: List[str]

    def __init__(self, fn: str):
        self.filename = fn
        self.labels = [HAPPI_TO_CONFLUENCE_LABEL]
        with open(fn, "rt") as fp:
            contents = fp.read().splitlines()

        title_lines = []
        for idx, line in enumerate(contents):
            if line.startswith("# "):
                line = line.strip("# ")
                directive, data = (item.strip() for item in line.split(":", 1))
                if directive == "title":
                    title_lines.append(data)
                elif directive == "label":
                    self.labels.append(data)
                else:
                    raise ValueError(f"Unknown directive: {directive} ({data})")
            else:
                contents = "\n".join(contents[idx:])
                break

        if not title_lines:
            raise ValueError(f"Template invalid: {fn} has no filename lines")

        self.titles = [jinja2.Template(title) for title in title_lines]
        self.template = jinja2.Template(contents)

    def render(self, **kwargs) -> Tuple[List[str], str]:
        return (
            [title.render(**kwargs) for title in self.titles],
            self.template.render(**kwargs)
        )


docstring_template = NamedTemplate("docstring.template")


def get_page_labels(client, page_id):
    return {
        label["name"]: label
        for label in client.get_page_labels(page_id)["results"]
    }


def best_effort_get_args(cls, happi_item):
    sig = inspect.signature(cls)
    kwargs = {
        param.name: param.default
        for param in sig.parameters.values()
        if param.default is not param.empty
    }
    try:
        bound = sig.bind(
            *happi_item.get("args", []),
            **happi_item.get("kwargs", {})
        )
    except TypeError:
        ...
    else:
        for arg, value in bound.arguments.items():
            kwargs.setdefault(arg, value)

    kwargs.update(**happi_item.get("kwargs"))
    return kwargs


def get_per_item_render_kwargs(happi_item_name, happi_item, state):
    device_class_name = happi_item["device_class"]
    try:
        cls = pcdsutils.utils.import_helper(device_class_name)
    except Exception:
        device_class_doc = "None"
        device_class_name = device_class_name.split(".")[-1]
    else:
        device_class_doc = inspect.getdoc(cls) or "None"
        device_class_name = cls.__name__

    _, rendered_docstring = docstring_template.render(
        sections=dict(numpydoc.docscrape.NumpyDocString(device_class_doc)),
        kwargs=best_effort_get_args(cls, happi_item),
        happi_item=happi_item,
    )

    if happi_item_name in state.setdefault("_related_pages", {}):
        related_pages = state["_related_pages"][happi_item_name]
    else:
        related_query = f"type = page and (title ~ {happi_item_name})"
        related_pages = client.cql(related_query, limit=5).get("results", [])
        for page in related_pages:
            page_api_space = page["content"]["_expandable"]["space"]
            page["space"] = page_api_space.split("/")[-1]
            page["labels"] = get_page_labels(client, page["content"]["id"])

        related_pages = [
            page
            for page in related_pages
            if not any(
                should_skip(page["content"]["title"], page)
                for should_skip in RELATED_TITLE_SKIPS
            )
        ]
        state["_related_pages"][happi_item_name] = related_pages
        logger.debug(
            "Found %d related pages for %s", len(related_pages), happi_item_name
        )

    relevant_pvs = happi_item.get("_whatrecord", {}).get("records", [])
    if not relevant_pvs:
        pvs_by_kind = {}
    else:
        pvs_by_kind = {
            "hinted": [],
            "normal": [],
        }
        for pv in sorted(relevant_pvs, key=lambda pv: pv["name"]):
            kind = pv["kind"].replace("Kind.", "")
            pvs_by_kind.setdefault(kind, []).append(pv)

    return dict(
        identifier=happi_item_name,
        device_name=happi_item_name,
        happi_item=happi_item,
        device_class=device_class_name,
        device_class_doc=rendered_docstring,
        relevant_pvs_by_kind=pvs_by_kind,
        page_title_marker=PAGE_TITLE_MARKER,
        user_page_suffix=USER_PAGE_SUFFIX,
        related_pages=related_pages,
        state=state,
    )


def get_view_render_kwargs(view, view_state, all_item_state):
    return dict(
        identifier=view.filename,
        all_item_state=all_item_state,
        view_state=view_state,
    )


def render_pages(
    client: Confluence,
    page_to_children,
    parent: dict,
    space: str,
    render_kw: dict,
    state: dict,
):
    if not page_to_children:
        return

    parent_id = parent["id"]

    for page_template, children in page_to_children.items():
        if not isinstance(page_template, NamedTemplate):
            continue
        logger.info("Rendering %s", page_template.filename)

        options = children.get("_options", {})
        titles, source = page_template.render(**render_kw)
        for title in titles:
            existing_page = client.get_page_by_title(title=title, space=space)
            if existing_page:
                labels = get_page_labels(client, existing_page["id"])
                if HAPPI_TO_CONFLUENCE_LABEL in labels:
                    # OK, even if it exists, this is our page
                    logger.info("Found a page we previously generated: %s (%s)",
                                title, list(labels))
                    break
            if not existing_page:
                logger.error("Available title: %s", title)
                labels = {}
                break
        else:
            logger.error("No available titles? %s", titles)
            continue

        if options.get("overwrite", True) and NO_OVERWRITE_LABEL not in labels:
            page_info = client.update_or_create(
                parent_id=parent_id,
                title=title,
                body=source,
            )
        else:
            try:
                page_info = client.create_page(
                    parent_id=parent_id,
                    title=title,
                    space=space,
                    body=source,
                )
            except requests.exceptions.HTTPError as ex:
                if "A page with this title already exists" not in str(ex):
                    raise
                logger.info("Page already exists: %s; not overwriting", title)
                page_info = client.get_page_by_title(space=space, title=title)

        for label in page_template.labels:
            client.set_page_label(page_info["id"], label)

        # for key, value in properties.items():
        #     client.set_page_property(
        #         page_info["id"],
        #         dict(key=key, value=value, version=dict(number=1, minorEdit=True, hidden=True)),
        #     )

        # state[title] = (page_template, page_info)

        identifier = render_kw["identifier"]
        identifier_state = state.setdefault(identifier, {})
        page_info["_template_"] = page_template
        identifier_state[page_template.filename] = page_info

        render_pages(
            client,
            children,
            render_kw=render_kw,
            parent=page_info,
            state=state,
            space=space,
        )

    return state


with open("happi_info.json", "rt") as fp:
    happi_info = json.load(fp)


per_device_hierarchy = {
    NamedTemplate("class.template"): {
        NamedTemplate("device.template"): {
            NamedTemplate("user.template"): {
                "_options": {
                    "overwrite": False,
                },
            }
        }
    }
}

matching_name_and_class_hierarchy = {
    NamedTemplate("device.template"): {
        NamedTemplate("user.template"): {
            "_options": {
                "overwrite": False,
            },
        }
    }
}

views = {
    NamedTemplate("all_devices.template"): {
    },
}

client = create_client()

root_page = client.get_page_by_title(space=SPACE, title=DOCUMENTATION_ROOT_TITLE)
if root_page is None:
    raise RuntimeError("No root page")

# Keys for the happi plugin are the happi item names
happi_items = happi_info["metadata_by_key"]

all_item_state = {}
for idx, (happi_name, happi_item) in enumerate(happi_items.items()):
    render_kw = get_per_item_render_kwargs(
        happi_name, happi_item, state=all_item_state
    )

    if happi_name.lower() == render_kw["device_class"].lower():
        # In cases of devices like AT1K4, its class and happi name are
        # the same; so we can't make it a subpage.. hmm
        to_render = matching_name_and_class_hierarchy
    else:
        to_render = per_device_hierarchy

    # properties = dict(
    #     device_name=happi_item_name,
    #     # happi_item=happi_item,
    #     device_class=device_class_name,
    # )
    render_pages(
        client=client, page_to_children=to_render,
        render_kw=render_kw, parent=root_page, space=SPACE,
        state=all_item_state,
    )
    all_item_state[happi_name]["happi_item"] = happi_item


view_state = {}
for view, view_children in views.items():
    render_kw = get_view_render_kwargs(
        view=view, all_item_state=all_item_state, view_state=view_state
    )
    render_pages(
        client=client, page_to_children={view: view_children},
        render_kw=render_kw, parent=root_page, space=SPACE,
        state=view_state,
    )
