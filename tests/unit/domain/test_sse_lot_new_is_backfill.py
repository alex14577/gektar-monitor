"""Layer 1 — domain: SseLotNew.is_backfill invariants (dr21).

Invariants covered:
  (4) Default is_backfill == False (backward-compatible; live-path never sets it).
  (3) is_backfill=True can be set explicitly (backfill publish path).

docs/architecture/09-test-strategy.md Layer 1:
  Unit: Pydantic defaults, frozen=True.  No network/DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fis_monitor.domain.models import LotPublicDTO, SseLotNew

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _make_public_dto() -> LotPublicDTO:
    from tests.factories import make_lot

    lot = make_lot()
    return LotPublicDTO(
        **lot.model_dump(),
        age_seconds=60,
        tier="match",
        freshness="hot",
    )


@pytest.mark.parametrize(
    "is_backfill_arg,expected",
    [
        (None, False),  # default: not passed → False
        (False, False),  # explicit False
        (True, True),  # backfill path: explicit True
    ],
    ids=["default", "explicit_false", "explicit_true"],
)
def test_sse_lot_new_is_backfill_default_and_explicit(
    is_backfill_arg: bool | None,
    expected: bool,
) -> None:
    """Invariants (3) and (4): is_backfill defaults to False; can be set to True."""
    dto = _make_public_dto()
    kwargs: dict = {"lot": dto, "fragment_template": "poster"}
    if is_backfill_arg is not None:
        kwargs["is_backfill"] = is_backfill_arg

    event = SseLotNew(**kwargs)
    assert event.is_backfill is expected
    assert event.event == "lot.new"  # event name unchanged regardless of is_backfill
