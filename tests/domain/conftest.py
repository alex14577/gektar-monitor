"""Shared fixtures for domain-layer tests.

`make_lot()` factory — minimal, realistic defaults. Tests override only the
fields under inspection (high cohesion in tests, low duplication).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from fis_monitor.domain import Lot


@pytest.fixture
def make_lot():
    """Factory-fixture for `Lot` instances with sensible defaults."""

    def _factory(**overrides: Any):
        now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
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
            "date_create": now,
            "date_update": now,
            "lat": 48.48,
            "lon": 135.08,
            "has_boundaries": True,
            "raw_json": {"k": "v"},
            "parser_version": 1,
            "first_seen": now,
            "last_seen": now,
            "detail_fetched_at": now,
            "enrichment_status": "done",
            "last_seen_at": now,
            "is_active": True,
            "inactive_reason": None,
            "inactive_since": None,
            "inactive_confirmed_at": None,
        }
        defaults.update(overrides)
        return Lot(**defaults)

    return _factory
