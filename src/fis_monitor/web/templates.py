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

TEMPLATES_DIR: Path = Path(__file__).parent / "templates"
STATIC_DIR: Path = Path(__file__).parent / "static"


def build_templates() -> Jinja2Templates:
    """Return a freshly configured Jinja2Templates pointing at the bundled dir."""
    return Jinja2Templates(directory=str(TEMPLATES_DIR))
