"""Jinja2 templates factory for the web layer.

Responsibility (SRP): construct a single, well-configured ``Jinja2Templates``
instance pointing at ``src/fis_monitor/web/templates``. No globals beyond the
factory return — callers (composition root / lifespan) own the instance and
inject it where needed.

Templates were copied from ``claude-design/templates/`` (per oxy.7).
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import select_autoescape

from fis_monitor.web.filters import format_date_ru

TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
STATIC_DIR: Path = Path(__file__).parent / "static"


def build_templates() -> Jinja2Templates:
    """Return a freshly configured Jinja2Templates pointing at the bundled dir.

    Autoescape: project uses compound `.html.jinja` extension which is NOT
    recognised by Jinja2's default `select_autoescape()` (it checks only
    `html`/`htm`/`xml`). Without explicit configuration `{{ user_input }}`
    in our templates is rendered raw — XSS-class vulnerability (Security F-01).
    Explicit enabled_extensions list closes the gap.

    Custom filters (hiq3):
    - ``dateformat``: formats a ``date`` / ``datetime`` as «D месяца YYYY»
      in Russian (no babel, no locale.setlocale — ADR-026 bundle budget).
    """
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.autoescape = select_autoescape(
        enabled_extensions=("html", "htm", "xml", "jinja", "html.jinja"),
        default_for_string=True,
    )
    templates.env.filters["dateformat"] = format_date_ru
    return templates
