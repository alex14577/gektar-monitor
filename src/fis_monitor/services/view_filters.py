"""View-filter service — session-scoped, cookie-persisted filters.

No database, no config.json.  State lives exclusively in a signed-ish
JSON cookie ``view_filters`` (localhost-only app per ADR-011; no HMAC in MVP).

Public API:
  ViewFilters   — Pydantic model for filter state.
  serialize()   — ViewFilters → URL-percent-encoded JSON string (cookie-safe).
  deserialize() — cookie string → ViewFilters | None (returns None on parse error).

Encoding: JSON is percent-encoded with urllib.parse.quote so that non-ASCII
subject names (Cyrillic) survive the latin-1 cookie encoding constraint imposed
by the HTTP spec and Starlette's response layer.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote, unquote

from pydantic import BaseModel, Field, ValidationError

__all__ = ["ViewFilters", "ViewFiltersService", "deserialize", "serialize"]

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

# TODO(future-bd): add HMAC signing if the app ever binds on a non-loopback
# interface.  Currently localhost-only per ADR-011, so plain JSON is fine.

# MVP placeholder list — real subjects come from SettingsService.list_subjects()
# once the corresponding bd task lands (separate bd).
PLACEHOLDER_SUBJECTS: list[str] = [
    "Московская область",
    "Краснодарский край",
    "Республика Татарстан",
    "Свердловская область",
    "Новосибирская область",
]


class ViewFilters(BaseModel):
    """Ephemeral view-filter state stored in a signed cookie.

    All fields are optional / nullable so that a partial cookie value
    (e.g. after a schema bump) degrades gracefully rather than erroring.

    Model is intentionally *not* frozen — callers may construct via keyword
    args and the serialisation roundtrip preserves None semantics.
    """

    model_config = {"extra": "ignore"}  # forward-compat: ignore unknown keys

    subjects: list[str] = Field(default_factory=list)
    area_min: int | None = Field(default=None, ge=0)
    area_max: int | None = Field(default=None, ge=0)
    only_new: bool = False
    only_stars: bool = False


# ---------------------------------------------------------------------------
# Cookie helpers (pure functions — easy to test)
# ---------------------------------------------------------------------------


def serialize(filters: ViewFilters) -> str:
    """Serialise *filters* to a percent-encoded JSON string suitable for a cookie value.

    Starlette (and the HTTP spec) require cookie values to be encodable in latin-1.
    Non-ASCII subject names (e.g. Cyrillic) are handled by URL-percent-encoding the
    entire JSON blob so only ASCII bytes appear in the Set-Cookie header.
    """
    return quote(filters.model_dump_json(exclude_none=False), safe="")


def deserialize(cookie_value: str) -> ViewFilters | None:
    """Parse a percent-encoded *cookie_value* into a ViewFilters instance.

    Returns ``None`` on any parse or validation error so callers can treat a
    missing or corrupted cookie as "no filters applied" without crashing.
    """
    if not cookie_value or not cookie_value.strip():
        return None
    try:
        raw = json.loads(unquote(cookie_value))
        return ViewFilters.model_validate(raw)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        _log.debug("view_filters cookie parse error (treating as empty): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Service facade (stateless — no constructor dependencies)
# ---------------------------------------------------------------------------


class ViewFiltersService:
    """Thin stateless facade over the module-level helpers.

    Allows the route layer to depend on a class (DI-friendly, easy to stub)
    while keeping the actual logic in pure functions above.
    """

    def serialize(self, filters: ViewFilters) -> str:
        return serialize(filters)

    def deserialize(self, cookie_value: str) -> ViewFilters | None:
        return deserialize(cookie_value)
