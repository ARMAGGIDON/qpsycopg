# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

import sys
from pathlib import Path

docs_dir = Path(__file__).resolve().parent
repo_dir = docs_dir.parent
sys.path.insert(0, str(repo_dir / "psycopg"))
sys.path.insert(0, str(repo_dir / "psycopg_pool"))
sys.path.append(str(docs_dir / "lib"))

import psycopg

# -- Project information -----------------------------------------------------

project = "psycopg"
copyright = "2020, Daniele Varrazzo and The Psycopg Team"
author = "Daniele Varrazzo"
release = psycopg.__version__


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, strings. They can be extensions
# coming with Sphinx (named 'sphinx.ext.*') or your custom extensions.
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    # Currently breaking docs link (see #562)
    # "sphinx_autodoc_typehints",
    "sql_role",
    "ticket_role",
    "pg3_docs",
    "libpq_docs",
]

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", ".venv"]

maximum_signature_line_length = 100

# -- Options for HTML output -------------------------------------------------

# The announcement may be in the website but not shipped with the docs
if (ann_file := (docs_dir / "../../templates/docs3-announcement.html")).exists():
    with ann_file.open(encoding="utf-8") as f:
        announcement = f.read()
else:
    announcement = ""

html_css_files = ["psycopg.css"]

# The name of the Pygments (syntax highlighting) style to use.
# Some that I've check don't suck:
# default lovelace tango algol_nu
# list: from pygments.styles import STYLE_MAP; print(sorted(STYLE_MAP.keys()))
pygments_style = "tango"

# The theme to use for HTML output.
html_theme = "furo"
html_show_sphinx = True
html_show_sourcelink = False
html_theme_options = {
    "announcement": announcement,
    "sidebar_hide_name": False,
    "light_logo": "psycopg.png",
    "dark_logo": "psycopg.png",
    "light_css_variables": {"admonition-font-size": "1rem"},
}

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin static file.
html_static_path = ["_static"]

# The reST default role (used for this markup: `text`) to use for all documents.
default_role = "obj"

intersphinx_mapping = {
    "py": ("https://docs.python.org/3", None),
    "pg2": ("https://www.psycopg.org/docs/", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
}

autodoc_member_order = "bysource"

# PostgreSQL docs version to link libpq functions to
libpq_docs_version = "18"

# Where to point on :ticket: role
ticket_url = "https://github.com/psycopg/psycopg/issues/%s"
