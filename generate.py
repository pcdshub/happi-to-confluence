from __future__ import annotations

import difflib
import inspect
import json
import logging
import os
import pathlib
import sys
from typing import List, Optional, Tuple

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

CONFLUENCE_URL = (
    os.environ.get("CONFLUENCE_URL", "") or
    "https://confluence.slac.stanford.edu"
)
CONFLUENCE_TOKEN = os.environ["CONFLUENCE_TOKEN"]
PAGE_TITLE_MARKER = " (Happi)"
USER_PAGE_SUFFIX = " - Notes"
RELATED_TITLE_SKIPS = (
    lambda title, page: PAGE_TITLE_MARKER in title,
    lambda title, page: "checkout" in title.lower(),
    lambda title, page: HAPPI_TO_CONFLUENCE_LABEL in page["labels"]
)
HAPPI_TO_CONFLUENCE_LABEL = "happi-to-confluence"
NO_OVERWRITE_LABEL = "no-overwrite"
SOURCE_PATH = pathlib.Path("source")
DIFF_IGNORE_CONFLUENCE_TAGS = True

PageHierarchy = dict
# TODO: annotation needs some work
# Union[
#     Dict[NamedTemplate, "PageHierarchy"],
#     Dict[str, Any]
# ]


class NamedTemplate:
    """
    A jinja template that contains a title and some additional information.

    There may be multiple titles provided; the first valid one will be used.
    Labels will be applied to the generated page by name.

    Parameters
    ----------
    fn : str
        The template filename.
    """
    filename: str
    titles: List[jinja2.Template]
    template: jinja2.Template
    labels: List[str]

    def __init__(self, fn: str):
        with open(fn, "rt") as fp:
            contents = fp.read().splitlines()
        info, contents = self._split_title_and_contents(contents)
        self.filename = fn
        self.labels = list(sorted(set(info["labels"]) | {HAPPI_TO_CONFLUENCE_LABEL}))
        self.titles = [jinja2.Template(title) for title in info["title_lines"]]
        self.template = jinja2.Template(contents)

        if not self.titles:
            raise ValueError(f"Template invalid: {fn} has no filename lines")

    @staticmethod
    def _split_title_and_contents(contents) -> Tuple[dict, str]:
        """Parse the template, grabbing header information and contents."""
        info = {
            "title_lines": [],
            "labels": [],
        }
        for idx, line in enumerate(contents):
            if line.startswith("# "):
                line = line.strip("# ")
                directive, data = (item.strip() for item in line.split(":", 1))
                if directive == "title":
                    info["title_lines"].append(data)
                elif directive == "label":
                    info["labels"].append(data)
                else:
                    raise ValueError(f"Unknown directive: {directive} ({data})")
            else:
                contents = "\n".join(contents[idx:])
                break

        return info, contents

    def __repr__(self):
        return f"<NamedTemplate {self.filename}>"

    def render(self, **kwargs) -> Tuple[List[str], str]:
        """
        Render the template with the given kwargs.

        Returns
        -------
        titles : list of str
            List of potential titles, to be checked on confluence.

        rendered : str
            The rendered page.
        """
        return (
            [title.render(**kwargs) for title in self.titles],
            self.template.render(**kwargs)
        )


# Each device will render with this given hierarchy of pages:
PER_DEVICE_HIERARCHY = {
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

# ... except if the device name and class name match, as in like AT1K4:
MATCHING_NAME_AND_CLASS_HIERARCHY: PageHierarchy = {
    NamedTemplate("device.template"): {
        NamedTemplate("user.template"): {
            "_options": {
                "overwrite": False,
            },
        }
    }
}

# Additionally, "views" of all (or subsets of devices) will be generated
# with the following.  These go at the documentation root.
VIEWS: PageHierarchy = {
    NamedTemplate("all_devices.template"): {
    },
}

docstring_template = NamedTemplate("docstring.template")


def create_client(
    url: str = CONFLUENCE_URL, token: str = CONFLUENCE_TOKEN
) -> Confluence:
    """Create the Confluence client.

    Parameters
    ----------
    url : str
        The confluence URL.

    token : str
        The token with read/write permissions.
    """
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    return Confluence(url, session=s)


def get_page_labels(client: Confluence, page_id: str):
    """
    Get page labels for the given page ID.

    Parameters
    ----------
    client : atlassian.Confluence
        The client.

    page_id : str
        The page identifier.

    Returns
    -------
    dict
        Label name to label information dictionary.
    """
    return {
        label["name"]: label
        for label in client.get_page_labels(page_id)["results"]
    }


def render_happi_template_arg(template, happi_item):
    """Fill a Jinja2 template using information from a happi item."""
    return jinja2.Template(template).render(**happi_item)


def best_effort_get_args(cls, happi_item):
    """
    Make an attempt at getting instantiation arguments for a device class.

    Attempts to bind to the class signature args/kwargs provided in happi
    metadata, matching up argument name to value.

    Parameters
    ----------
    cls : type
        The device class.

    happi_item : dict
        Happi item metadata.
    """
    sig = inspect.signature(cls)
    kwargs = {
        param.name: param.default
        for param in sig.parameters.values()
        if param.default is not param.empty
    }
    try:
        happi_args = [
            render_happi_template_arg(value, happi_item)
            for value in happi_item.get("args", [])
        ]
        happi_kwargs = {
            name: render_happi_template_arg(value, happi_item)
            for name, value in happi_item.get("kwargs", {}).items()
        }
        bound = sig.bind(*happi_args, **happi_kwargs)
    except TypeError:
        kwargs.update(**happi_item.get("kwargs"))
    else:
        for name, value in bound.arguments.items():
            kwargs.setdefault(name, value)

    for name, value in happi_item.get("kwargs").items():
        kwargs.setdefault(name, value)

    return kwargs


def get_per_item_render_kwargs(client, happi_item_name, happi_item, state):
    """
    For a given happi item, return render kwargs for a template.

    Parameters
    ----------
    client : atlassian.Confluence
        The confluence client.

    happi_item_name : str
        The happi item name.

    happi_item : dict
        The happi item metadata dictionary.

    state : dict
        The current happi-to-confluence render state.

    Returns
    -------
    render_kw : dict
        Render keyword arguments for a jinja template.
        identifier: the happi item name
        device_name: the ophyd device name
        happi_item: the happi item metadata dictionary
        device_class: the device class name
        device_class_doc: the device class docstring
        relevant_pvs_by_kind: relevant PVs for the device
        page_title_marker: optional suffix for generated page titles
        user_page_suffix: the user-editable notes pages
        related_pages: related pages to the given device based on a search
        state: the overall happi-to-confluence state dictionary
        item_state: this device's state from happi-to-confluence
        confluence_url: the base confluence URL (``CONFLUENCE_URL``)
    """
    device_class_name = happi_item["device_class"]
    try:
        cls = pcdsutils.utils.import_helper(device_class_name)
    except Exception:
        device_class_doc = "None"
        device_class_name = device_class_name.split(".")[-1]
        cls = None
        kwargs = {}
    else:
        device_class_doc = inspect.getdoc(cls) or "None"
        device_class_name = cls.__name__
        kwargs = best_effort_get_args(cls, happi_item)

    _, rendered_docstring = docstring_template.render(
        sections=dict(numpydoc.docscrape.NumpyDocString(device_class_doc)),
        kwargs=kwargs,
        happi_item=happi_item,
    )

    if happi_item_name in state.setdefault("_related_pages", {}):
        related_pages = state["_related_pages"][happi_item_name]
    else:
        related_query = (
            f"type = page and ( "
            f"title ~ {happi_item_name} "
            f"OR title ~ {device_class_name} "
            f")"
        )
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
        root_page=DOCUMENTATION_ROOT_TITLE,
        related_pages=related_pages,
        state=state,
        item_state=state.setdefault(happi_item_name, {}),
        confluence_url=client.url,
    )


def get_view_render_kwargs(view, view_state, all_item_state):
    """
    Get aggregate view render keyword arguments.

    Returns
    -------
    render_kw : dict
        Render keyword arguments for a jinja template.
        identifier: the page identifier is just the view name itself
        all_item_state: the state after generating all device pages
        view_state: the state information while generating aggregate views
    """
    return dict(
        identifier=view.filename,
        all_item_state=all_item_state,
        view_state=view_state,
        root_page=DOCUMENTATION_ROOT_TITLE,
        page_title_marker=PAGE_TITLE_MARKER,
        user_page_suffix=USER_PAGE_SUFFIX,
    )


def check_diff(
    existing_source: str,
    new_source: str,
    page_diff: str,
) -> bool:
    if existing_source == new_source:
        return True

    for line in page_diff.splitlines():
        if line.startswith("! "):
            line = line.lstrip("!").strip()
            # <ac:..> confluence tags may be ignored
            ac_like = any(
                (
                    line.startswith("<ac:"),
                    line.startswith("</ac:"),
                )
            )
            if ac_like:
                if not DIFF_IGNORE_CONFLUENCE_TAGS:
                    return False
            else:
                return False
        elif line.startswith("+ ") or line.startswith("- "):
            line = line.lstrip("+- \t").strip()
            # White-space changes are bad
            if line.strip():
                return False

    return True


def diff_pages(
    dest_path: pathlib.Path,
    title: str,
    existing_source: str,
    new_source: str,
) -> str:
    """Write status diff information to ``dest_path``."""
    existing_path = dest_path / "existing"
    new_path = dest_path / "new"
    diff_path = dest_path / "diff"
    for path in [existing_path, new_path, diff_path]:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.warning("Failed to create %s", path, exc_info=True)

    with open(existing_path / f"{title}.html", "wt") as fp:
        print(existing_source, file=fp)

    with open(new_path / f"{title}.html", "wt") as fp:
        print(new_source, file=fp)

    diff = difflib.context_diff(
        existing_source.splitlines(True),
        new_source.splitlines(True),
        fromfile=title,
        tofile=f"new-{title}"
    )
    html_formatted_diff = difflib.HtmlDiff().make_file(
        existing_source.splitlines(True),
        new_source.splitlines(True),
        title,
        f"new-{title}"
    )

    diff_string = "".join(diff)
    with open(diff_path / f"{title}.html.diff", "wt") as fp:
        print(diff_string, file=fp)
    with open(diff_path / f"{title}.html", "wt") as fp:
        print(html_formatted_diff, file=fp)
    return diff_string


def render_pages(
    client: Confluence,
    page_to_children: PageHierarchy,
    parent: dict,
    space: str,
    render_kw: dict,
    state: dict,
    properties: dict,
    minor_edit: bool = True,
):
    """
    Render confluence pages.

    Parameters
    ----------
    client : atlassian.Confluence
        The pre-configured Confluence client.

    page_to_children : PageHierarchy
        Nested dictionary of NamedTemplate to child/descendent pages.

    parent : dict
        Parent page information from ``get_page_by_title``.

    space : str
        The Confluence space to publish to.

    render_kw : dict[]
    """
    if not page_to_children:
        return

    parent_id = parent["id"]

    for page_template, children in page_to_children.items():
        if not isinstance(page_template, NamedTemplate):
            continue
        logger.info("Rendering %s", page_template.filename)

        options = children.get("_options", {})
        titles, new_source = page_template.render(**render_kw)
        existing_page = None
        for title in titles:
            existing_page: Optional[dict] = client.get_page_by_title(
                title=title,
                space=space,
                expand="body.storage",
            )
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

        do_not_overwrite = not (
            options.get("overwrite", True) and
            NO_OVERWRITE_LABEL not in labels
        )
        if do_not_overwrite and existing_page:
            page_info = existing_page
        else:
            existing_source = (
                existing_page["body"]["storage"]["value"]
                if existing_page else ""
            )
            try:
                page_diff = diff_pages(
                    dest_path=SOURCE_PATH,
                    title=title,
                    existing_source=existing_source,
                    new_source=new_source,
                )
            except Exception as ex:
                page_diff = "! diff failure"
                logger.error(
                    "Failed to diff existing pages: %s",
                    ex, exc_info=True
                )

            if existing_page and check_diff(existing_source, new_source, page_diff):
                print("Existing page is up-to-date. Great!")
                page_info = existing_page
            else:
                if existing_page and existing_page["id"] == parent_id:
                    # Special-case for updating the root document;
                    # parent_id is set to DOC_ROOT and we may want to update
                    # that automatically
                    parent_id = client.get_parent_content_id(parent_id)

                try:
                    page_info = client.update_or_create(
                        parent_id=parent_id,
                        title=title,
                        body=new_source,
                        minor_edit=minor_edit,
                        version_comment="happi-to-confluence update",
                    )
                except Exception as ex:
                    logger.error("Failed to update page: %s", ex, exc_info=True)
                    with open(f"failed_update_{title}.txt", "wt") as fp:
                        fp.write(new_source)

                    continue

        for label in page_template.labels:
            client.set_page_label(page_info["id"], label)

        # for key, value in properties.items():
        #     # client.set_page_property(
        #     client.update_page_property(
        #         page_info["id"],
        #         dict(
        #             key=key,
        #             value=value,
        #             # version=dict(minorEdit=True, hidden=True),
        #         ),
        #     )

        # state[title] = (page_template, page_info)

        identifier = render_kw["identifier"]
        identifier_state = state.setdefault(identifier, {})
        page_info["_template_"] = page_template
        identifier_state[page_template.filename] = page_info
        # -> state[identifier][page_template.filename] = page_info
        # -> state[identifier][page_template.filename]["_template_"]

        render_pages(
            client,
            children,
            render_kw=render_kw,
            parent=page_info,
            state=state,
            space=space,
            properties={},
        )

    return state


def initialize_client(space: str, root_title: str) -> Tuple[Confluence, dict]:
    """
    Initialize the Confluence client.

    Expects that ``root_title`` page exists.  Fails with RuntimeError if not.

    Parameters
    ----------
    space : str
        The space in which to store documentation.

    root_title : str
        The title of the root page.
    """
    client = create_client()

    root_page = client.get_page_by_title(
        space=space, title=root_title
    )
    if root_page is None:
        raise RuntimeError("No root page")
    return client, root_page


def render_device_pages(
    space: str,
    client: Confluence,
    root_page,
    happi_info_filename: str = "happi_info.json",
    testing: bool = False,
) -> dict:
    """
    Render all individual device pages.

    Parameters
    ----------
    space : str
        The Confluence space to publish to.

    client : atlassian.Confluence
        The confluence client.

    root_page : dict
        The documentation root page information.

    happi_info_filename : str
        The happi info JSON filename, generated from
        ``whatrecord.plugins.happi``.

    Returns
    -------
    state : dict
        Returns aggregated information about all generated pages.
        Includes per-device "happi_item" information and page information.
        state["_related_pages"][happi_name]
        state[happi_name][page_template_filename] -> page_info
        state[happi_name][page_template_filename]["_template_"]
        state[happi_name]["happi_item"]
    """
    state = {}

    with open(happi_info_filename, "rt") as fp:
        happi_info = json.load(fp)

    # Keys for the happi plugin are the happi item names
    md_by_key = happi_info["metadata_by_key"]
    for idx, (happi_name, happi_item) in enumerate(md_by_key.items(), 1):
        logger.info("")
        logger.info(f"Working on device {idx} of {len(md_by_key)}: {happi_name}...")
        if not happi_item.get("device_class", None):
            continue

        render_kw = get_per_item_render_kwargs(
            client, happi_name, happi_item, state=state
        )

        if happi_name.lower() == str(render_kw["device_class"]).lower():
            # In cases of devices like AT1K4, its class and happi name are
            # the same; so we can't make it a subpage.. hmm
            to_render = MATCHING_NAME_AND_CLASS_HIERARCHY
            state[happi_name]["has_class_page"] = False
        else:
            to_render = PER_DEVICE_HIERARCHY
            state[happi_name]["has_class_page"] = True

        render_pages(
            client=client,
            page_to_children=to_render,
            render_kw=render_kw,
            parent=root_page,
            space=SPACE,
            state=state,
            properties=dict(
                device_name=happi_name,
                # happi_item=happi_item,
                device_class=render_kw["device_class"],
            ),
        )
        state[happi_name]["happi_item"] = happi_item

        if testing and idx > 10:
            break

    return state


def render_view_pages(space: str, client: Confluence, root_page, state):
    """
    Views are not for individual devices, but rather pages that aggregate
    all devices together in some sensible way.

    For example, see the "all_devices" template.

    Parameters
    ----------
    state : dict
        The dictionary of all happi items by name, along with their metadata.

    Returns
    -------
    state : dict
        State information about generated overall view pages.
    """
    view_state = {}
    for view, view_children in VIEWS.items():
        render_kw = get_view_render_kwargs(
            view=view, all_item_state=state, view_state=view_state
        )
        render_pages(
            client=client,
            page_to_children={view: view_children},
            render_kw=render_kw,
            parent=root_page,
            space=space,
            state=view_state,
            properties={},
        )
    return view_state


def main(space: str, root_title: str, testing: bool = False):
    client, root_page = initialize_client(space=space, root_title=root_title)
    all_item_state = render_device_pages(space=space, client=client, root_page=root_page, testing=testing)
    view_state = render_view_pages(space=space, client=client, root_page=root_page, state=all_item_state)
    return all_item_state, view_state


if __name__ == "__main__":
    testing = "--test" in sys.argv
    if "--production" in sys.argv:
        SPACE = "PCDS"
        DOCUMENTATION_ROOT_TITLE = "Happi Devices"
    else:
        SPACE = "~klauer"
        DOCUMENTATION_ROOT_TITLE = "Typhos Documentation Root"

    print(
        f"Writing to space '{SPACE}' page '{DOCUMENTATION_ROOT_TITLE}'.\n"
        f"Single page test mode enable status: {testing}.\n"
        f"Ctrl-C now to cancel, or press enter to continue\n"
    )
    try:
        # input()   # TODO
        ...
    except KeyboardInterrupt:
        sys.exit(0)

    all_item_state, view_state = main(
        space=SPACE,
        root_title=DOCUMENTATION_ROOT_TITLE,
        testing=testing,
    )  # noqa: F401
