happi-to-confluence
===================

Use ``whatrecord``-generated metadata from a happi database to auto-generate
Confluence documentation.

How do I use this?
------------------

First, you should be sure that you've been tasked to update the happi
documentation.  If not, you probably should not be running these steps.
If you would like to prototype a new set of documentation, consider using
the "test" confluence server so that you do not interrupt access to the
production documentation source.

If you are still OK with the above, continue on to the following steps:

1. Configure a confluence token with read/write access.

2. Create a `confluence.sh` setup script:

    ```bash
    #!/bin/bash

    export CONFLUENCE_URL=https://confluence.slac.stanford.edu
    export CONFLUENCE_USER=$USER
    export CONFLUENCE_TOKEN="((read/write token from some user here))"

    source /cds/group/pcds/pyps/conda/pcds_conda
    ```

3. Ensure the above script is only readable by yourself, as you don't want others
   to use your token.  This script is part of `.gitignore` so you don't
   accidentally commit it.

    ```bash
    $ chmod 0700 confluence.sh
    ```

4. Use the provided `Makefile` (or investigate its contents).

5. `make happi_info.json` will use whatrecord to generate a JSON file that
   describes the entire contents of your happi database down to the individual
   EPICS process variable (PV) level.

6. `make pages` will use the above-generated `happi_info.json` and your
   confluence tokens to generate the entire suite of Confluence pages for
   each document.


Pages?
------

Looking at `generate.py`, many high-level settings are configured directly in
code for now.  Eventually this may be a configuration file, if this
method of documentation is determined to be a good enough pattern.

```python
SPACE = "PCDS"
DOCUMENTATION_ROOT_TITLE = "Happi Devices"
PAGE_TITLE_MARKER = " (Happi)"
USER_PAGE_SUFFIX = " - Notes"
HAPPI_TO_CONFLUENCE_LABEL = "happi-to-confluence"
NO_OVERWRITE_LABEL = "no-overwrite"
RELATED_TITLE_SKIPS = (
    lambda title, page: PAGE_TITLE_MARKER in title,
    lambda title, page: "checkout" in title.lower(),
    lambda title, page: HAPPI_TO_CONFLUENCE_LABEL in page["labels"]
)
```

| Variable                  | Default               | Description                                                                                                                                                                                                       |
|---------------------------|-----------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| SPACE                     | "PCDS"                | This is the confluence space where documentation will be generated.                                                                                                                                               |
| DOCUMENTATION_ROOT_TITLE  | "Happi Devices"       | This is the "root" page under which all pages from happi-to-confluence will be created (or updated).                                                                                                              |
| PAGE_TITLE_MARKER         | " (Happi)"            | This is a disambiguation page suffix.  If an existing page for "device_name" exists that happi-to-confluence did not create, it will not be overwritten. Instead, "device_name (Happi)" will be created and used. |
| HAPPI_TO_CONFLUENCE_LABEL | "happi-to-confluence" | This is a label that will be added to every page that happi-to-confluence creates.                                                                                                                                |
| NO_OVERWRITE_LABEL        | "no-overwrite"        | If happi-to-confluence sees this label on a specific page, it will not update or overwrite the page.                                                                                                              |
| RELATED_TITLE_SKIPS       | (see code)            | This is a set of callable functions that - based on a page title and metadata - will determine if a "related" page should be included in the default notes page.                                                  |


Templates
---------

Several template hierarchies are defined in the code.

Most happi items / ophyd devices will render with the following hierarchy of
pages.  The hierarchy is described by way of nested dictionaries, with 
the top-level template being created under ``DOCUMENTATION_ROOT_TITLE`` (i.e.,
the `"Happi Devices"` page as defined by the above settings.)

```python
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
```

So, at the top level you will have "class.template" which gets rendered
to show all of the individual happi items (devices) that share the same
class.  Underneath that page, as child pages, you will have per-device
pages. Underneath each device page, you will have a notes page as a child page.

An exception to the above hierarchy is if the device name and class name match,
as in for example ``AT1K4``, the following will be used instead.  This is
because pages in confluence must have globally unique title names.

```python
MATCHING_NAME_AND_CLASS_HIERARCHY: PageHierarchy = {
    NamedTemplate("device.template"): {
        NamedTemplate("user.template"): {
            "_options": {
                "overwrite": False,
            },
        }
    }
}
```

Additionally, "views" of all (or subsets of devices) will be generated
with the following.  These go at the documentation root.

```python
VIEWS: PageHierarchy = {
    NamedTemplate("all_devices.template"): {
    },
}
```

Finally, Python class docstrings will be handled specially.

```python
docstring_template = NamedTemplate("docstring.template")
```

This will be available as the ``device_class_doc`` template variable when
generating per-device or per-class pages.


Templates and Template Variables
--------------------------------

Templates are Jinja2-format and support variable expansion, loops, and many
other neat things. See https://jinja2docs.readthedocs.io/en/stable/ for more
information on Jinja itself.

For each happi item (individual device instance), the following variables
are available:

| Variable                 | Description                                         |
|--------------------------|-----------------------------------------------------|
| ``identifier``           | The happi item name                                 |
| ``device_name``          | The ophyd device name                               |
| ``happi_item``           | The happi item metadata dictionary                  |
| ``device_class``         | The device class name                               |
| ``device_class_doc``     | The device class docstring                          |
| ``relevant_pvs_by_kind`` | Relevant PVs for the device                         |
| ``page_title_marker``    | Optional suffix for generated page titles           |
| ``user_page_suffix``     | The user-editable notes pages                       |
| ``related_pages``        | Related pages to the given device based on a search |
| ``state``                | The overall happi-to-confluence state dictionary    |
| ``item_state``           | This device's state from happi-to-confluence        |
| ``confluence_url``       | The base confluence URL (``CONFLUENCE_URL``)        |


For each "view" page, the following will be available:

| Variable                 | Description                                         |
|--------------------------|-----------------------------------------------------|
| ``all_item_state``       | Confluence API state for each generated item page.  |
| ``identifier``           | The view page identifier name                       |
| ``page_title_marker``    | Optional suffix for generated page titles           |
| ``user_page_suffix``     | The user-editable notes pages                       |
| ``view_state``           | State information used while generating the view.   |


For more information on how whatrecord presents happi metadata, take a look
into the ``happi_info.json`` file. Consider formatting it with a tool like
``jq`` prior to viewing.

Page titles?
------------

The ``NamedTemplate`` class is used to read and render each of the template
files in this repository. A single ``.template`` file contains the following
information:

1. Page title
2. Page labels
3. Page contents

The header section contains (1) and (2), and the remainder is considered (3)
the page contents.

The page titles can use the previously-described variables and have fallbacks.

```
# title: {{ device_name }}
# title: {{ device_name }}{{ page_title_marker}}
# label: auto-generated
```

In the above example, for a device named "mydevice", happi-to-confluence would
first try to find a page called ``mydevice``.  It would fall back to
``mydevice (Happi)`` in the case that the ``mydevice`` page lacked a
``HAPPI_TO_CONFLUENCE_LABEL`` label.  As described above, this is to avoid
overwriting user-created pages of the same name.

Multiple labels may be specified on individual lines using the following format:

```
# label: label1
# label: label2
```

Labels may also include any supported variables.


HTML/XML?
---------

Each template should be written in the Confluence-supported markup language.

* What about Confluence macros?

Macros may be included as needed.  Avoid using UUIDs in the template files
for the macros.

* Have any easier way to make a template?

Consider using Confluence itself to sketch out your page.  In the editing
interface, click "view source" to see Confluence's interpretation of the page
source.  Copy that into your template, remove UUIDs from macros, add it to
the page hierarchy, and give it a try.
