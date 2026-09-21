"""Sphinx configuration for the semiconductor-intel design docs."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# Sphinx (8.2+) patches typing internals when autodoc loads; pydantic model
# classes created *after* that hit `PydanticSchemaGenerationError` on
# `__pydantic_extra__` (any nooa import constructs such classes). Build all
# nooa/pydantic-dependent modules now, in a clean interpreter — autodoc then
# just re-uses sys.modules and docs keep full fidelity.
import importlib  # noqa: E402

_PREIMPORT = [
    "nooa",
    "intel.api.app",
    "intel.api.deps",
    "intel.api.errors",
    "intel.api.pagination",
    "intel.api.idempotency",
    "intel.api.sse",
    "intel.nooa_adapter.agents",
    "intel.nooa_adapter.factory",
    "intel.nooa_adapter.middleware",
    "intel.nooa_adapter.gateway",
    "intel.nooa_adapter.tracing",
    "intel.workers.composition",
    "intel.workers.runner",
    "intel.workers.leases",
    "intel.workers.scheduler",
    "intel.workers.stores",
]
for _mod in _PREIMPORT:
    importlib.import_module(_mod)

project = "semiconductor-intel"
author = "semiconductor-intel contributors"
copyright = "2026, semiconductor-intel contributors"  # noqa: A001

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx.ext.todo",
    "myst_parser",
    "sphinxcontrib.mermaid",
]

# Autodoc imports the real package; Settings reads env with safe defaults.
autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented"
autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
}
nitpicky = False

napoleon_google_docstring = True
napoleon_numpy_docstring = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
}
intersphinx_timeout = 10

myst_enable_extensions = ["colon_fence", "deflist", "tasklist"]
myst_heading_anchors = 3

mermaid_d3_zoom = True

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "superpowers"]

# Keep the build strict: docs rot fast when warnings are allowed to pile up.
# (CI runs `sphinx-build -W --keep-going`.)
suppress_warnings = ["myst.header"]

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_css_files = ["custom.css"]

html_theme_options = {
    "navigation_with_keys": True,
    "style_external_links": True,
}

latex_engine = "xelatex"
language = "zh_CN"

todo_include_todos = False
