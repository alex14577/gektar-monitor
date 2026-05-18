"""Tests for LotViewModel (Layer 3 — web/template).

Invariants:
- has_registry_date is True when date_registry is set, False when None
"""
from __future__ import annotations

from datetime import UTC, datetime

from fis_monitor.web.sse_encoder import LotViewModel
from tests.factories import make_lot

# --- has_registry_date -------------------------------------------------------


def test_has_registry_date_true_when_date_set() -> None:
    lot = make_lot(date_registry=datetime(2024, 3, 15, tzinfo=UTC))
    vm = LotViewModel(lot)
    assert vm.has_registry_date is True


def test_has_registry_date_false_when_none() -> None:
    lot = make_lot(date_registry=None)
    vm = LotViewModel(lot)
    assert vm.has_registry_date is False
