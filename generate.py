import copy
import getpass
import logging
import json
import os
import inspect
import pathlib
import sys
import functools
import pcdsutils.utils
import requests.exceptions

import jinja2
import lxml
import lxml.etree
import requests
from atlassian import Confluence

logging.basicConfig(level="INFO")

logger = logging.getLogger(__name__)
logger.setLevel("DEBUG")

# space = "PCDS"
space = "~klauer"
documentation_root_title = "Typhos Documentation Root"
page_title_prefix = "Typhos - "
user_page_suffix = " - User"
related_title_skips = (
    lambda title: title.startswith(page_title_prefix),
    lambda title: "checkout" in title.lower(),
)

# autogen label


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
    s.headers['Authorization'] = f"Bearer {os.environ['CONFLUENCE_TOKEN']}"

    client = Confluence(
        (os.environ.get("CONFLUENCE_URL", "") or
         "https://confluence-test02.slac.stanford.edu"),
        session=s,
    )
    return client


class NamedTemplate:
    filename: str
    title: jinja2.Template
    template: jinja2.Template

    def __init__(self, fn: str):
        self.filename = fn
        with open(fn, "rt") as fp:
            self.title = jinja2.Template(fp.readline().strip("# "))
            self.template = jinja2.Template(fp.read())

    def render(self, **kwargs):
        return self.title.render(**kwargs), self.template.render(**kwargs)


def render_pages(
    happi_item_name, happi_item, page_to_children, parent_id, state=None
):
    if not page_to_children:
        return

    state = state or {"related_pages": {}}
    try:
        cls = pcdsutils.utils.import_helper(device_class_name)
    except Exception:
        device_class_doc = "None"
    else:
        device_class_doc = inspect.getdoc(cls)

    if happi_item_name in state["related_pages"]:
        related_pages = state["related_pages"][happi_item_name]
    else:
        related_query = f"type = page and (title ~ {happi_item_name})"
        related_pages = client.cql(related_query, limit=5).get("results", [])
        related_pages = [
            page for page in related_pages
            if not any(
                should_skip(page["content"]["title"])
                for should_skip in related_title_skips
            )
        ]
        for page in related_pages:
            space = page["content"]["_expandable"]["space"]
            page["space"] = space.split("/")[-1]
        state["related_pages"][happi_item_name] = related_pages
        logger.debug("Found %d related pages for %s", len(related_pages), happi_item_name)

    render_kw = dict(
        device_name=happi_item_name,
        happi_item=happi_item,
        device_class=device_class_name,
        device_class_doc=device_class_doc,
        relevant_pvs=happi_info["metadata"]["item_to_records"].get(happi_item_name, []),
        page_title_prefix=page_title_prefix,
        user_page_suffix=user_page_suffix,
        related_pages=related_pages,
    )

    properties = dict(
        device_name=happi_item_name,
        # happi_item=happi_item,
        device_class=device_class_name,
    )

    for page, children in page_to_children.items():
        if not isinstance(page, NamedTemplate):
            continue
        logger.info("Rendering %s", page.filename)

        options = children.get("_options", {})
        title, source = page.render(**render_kw)

        if options.get("overwrite", True):
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
                logger.info("Page already exists: %s; not overwriting",
                            title)
                page_info = client.get_page_by_title(
                    space=space,
                    title=title
                )

        # for key, value in properties.items():
        #     client.set_page_property(
        #         page_info["id"],
        #         dict(key=key, value=value, version=dict(number=1, minorEdit=True, hidden=True)),
        #     )

        state[page] = page_info
        render_pages(
            happi_item_name, happi_item, children, parent_id=page_info["id"],
            state=state
        )

    return state


with open("happi_info.json", "rt") as fp:
	happi_info = json.load(fp)


hierarchy = {
    NamedTemplate("class.template"): {
        NamedTemplate("device.template"): {
            NamedTemplate("user.template"): {
                "_options": {
                    # "overwrite": False,
                },
            }
        }
    }
}

client = create_client()

root_id = client.get_page_id(space=space, title=documentation_root_title)
if root_id is None:
    raise RuntimeError("No root page")

happi_items = happi_info["metadata"]["item_to_metadata"]

state = {}
for idx, (name, happi_item) in enumerate(happi_items.items()):
    device_class_name = happi_item["device_class"]
    state[name] = render_pages(name, happi_item, hierarchy, parent_id=root_id)
    if idx == 10:
        break
