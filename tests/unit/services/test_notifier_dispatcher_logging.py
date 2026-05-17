"""Logging tests for NotifierDispatcher DEBUG events (gektar_monitor-b9wq).

Covers:
- dispatcher.dispatch.entry (lot_id, region_id, channels_count)
- dispatcher.channel.invoked (lot_id, channel_id, recipients_count)
"""
from __future__ import annotations

import logging

import pytest

from tests.unit.services.conftest import (
    DISPATCHER_LOGGER,
    make_dispatcher,
    make_lot_dto,
)


def test_dispatch_emits_dispatch_entry_debug(caplog: pytest.LogCaptureFixture) -> None:
    """dispatcher.dispatch.entry emitted with lot_id + region_id + channels_count."""
    dispatcher = make_dispatcher()
    lot = make_lot_dto(lot_id=55, region_id=99)

    with caplog.at_level(logging.DEBUG, logger=DISPATCHER_LOGGER):
        dispatcher.dispatch(lot)

    records = [r for r in caplog.records if r.getMessage() == "dispatcher.dispatch.entry"]
    assert records, "expected dispatcher.dispatch.entry debug event"
    rec = records[0]
    assert rec.__dict__.get("lot_id") == 55
    assert rec.__dict__.get("region_id") == 99
    assert "channels_count" in rec.__dict__


def test_dispatch_channels_emits_channel_invoked_debug(caplog: pytest.LogCaptureFixture) -> None:
    """dispatcher.channel.invoked emitted for each registered channel."""
    dispatcher = make_dispatcher(with_browser=True)
    lot = make_lot_dto(lot_id=55)

    with caplog.at_level(logging.DEBUG, logger=DISPATCHER_LOGGER):
        dispatcher._dispatch_all_channels(lot)

    records = [r for r in caplog.records if r.getMessage() == "dispatcher.channel.invoked"]
    assert records, "expected dispatcher.channel.invoked debug event"
    rec = records[0]
    assert rec.__dict__.get("lot_id") == 55
    assert rec.__dict__.get("channel_id") == "browser"
    assert "recipients_count" in rec.__dict__
