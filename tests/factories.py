"""Plain-function factories for domain DTOs.

Importable from any test module (no pytest dependency, no fixture wiring):

    from tests.factories import make_lot, make_notification, make_settings

Each factory returns a fully valid Pydantic instance with sensible defaults;
callers override only the fields under inspection.

Note: a fixture-style `make_lot` already exists in `tests/domain/conftest.py`
(legacy from bd 531.1, scoped to domain-layer tests). That fixture is left
in place for backward compatibility; new tests should prefer these plain
functions because they:
- Avoid pytest-fixture name shadowing across test trees.
- Can be called multiple times in one test without per-call fixture setup.
- Compose freely inside parametrize/data tables.
"""

from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.models import (
    Lot,
    NotificationRecord,
    Settings,
)

# Canonical UTC instant used across factories so tests get reproducible state.
_DEFAULT_NOW = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)


def make_lot(**overrides: Any) -> Lot:
    """Build a valid `Lot` with sensible defaults.

    Overrides applied last-write-wins. Examples:

        make_lot()
        make_lot(id=42, region="Хабаровский край")
    """
    defaults: dict[str, Any] = {
        "id": 12345,
        "cadastral_no": "27:23:0040000:1234",
        "area_sqm": 10_000,
        "region": "Хабаровский край",
        "municipality": "Хабаровск",
        "land_category": "Земли сельхозназначения",
        "permitted_use": "ЛПХ",
        "ogv": "Минимущество ХК",
        "status": "Свободен",
        "date_create": _DEFAULT_NOW,
        "date_update": _DEFAULT_NOW,
        "lat": 48.48,
        "lon": 135.08,
        "has_boundaries": True,
        "raw_json": {"k": "v"},
        "parser_version": 1,
        "first_seen": _DEFAULT_NOW,
        "last_seen": _DEFAULT_NOW,
        "detail_fetched_at": _DEFAULT_NOW,
        "enrichment_status": "done",
        "last_seen_at": _DEFAULT_NOW,
        "is_active": True,
        "inactive_reason": None,
        "inactive_since": None,
        "inactive_confirmed_at": None,
    }
    defaults.update(overrides)
    return Lot(**defaults)


def make_notification(**overrides: Any) -> NotificationRecord:
    """Build a valid `NotificationRecord` in the freshly-reserved state.

    Defaults: status='pending', attempt_no=0, both timestamps None
    (the shape produced by `NotificationsRepository.reserve` — ADR-019).
    """
    defaults: dict[str, Any] = {
        "lot_id": 12345,
        "channel": "email",
        "recipient": "user@example.com",
        "status": "pending",
        "attempt_no": 0,
        "last_attempt_at": None,
        "sent_at": None,
    }
    defaults.update(overrides)
    return NotificationRecord(**defaults)


def make_settings(**overrides: Any) -> Settings:
    """Build a valid `Settings` (root config.json shape).

    All nested config sections have model-level defaults — calling
    `Settings()` already yields a valid instance. Overrides forwarded
    verbatim.
    """
    return Settings(**overrides)
