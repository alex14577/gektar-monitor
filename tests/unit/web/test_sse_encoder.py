"""Unit tests for ``web.sse_encoder`` — HTML rendering correctness (hiq3).

Layer 3 (Web) — tests the encoder factory and view-model wrappers that
convert domain SSE events into rendered HTML fragments.

Covered invariants:
- ``SseLotNew`` events render the ``lot-card--new`` CSS class (was_new=True).
- Base ``LotViewModel`` returns was_new=False.
- ``_SseLotNewViewModel`` returns was_new=True.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fis_monitor.domain.models import Lot, LotPublicDTO, SseLoginSucceeded, SseLotNew, SseStatus
from fis_monitor.web.sse_encoder import LotViewModel, _SseLotNewViewModel, make_html_sse_encoder
from fis_monitor.web.templates import build_templates

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_lot_dto(lot_id: int = 1) -> LotPublicDTO:
    lot = Lot(
        id=lot_id,
        cadastral_no="01:02:000000:1",
        area_sqm=None,
        region="TestRegion",
        municipality=None,
        land_category=None,
        permitted_use=None,
        ogv=None,
        status="active",
        date_create=_TS,
        date_update=None,
        lat=None,
        lon=None,
        has_boundaries=None,
        raw_json={},
        first_seen=_TS,
        last_seen=_TS,
        detail_fetched_at=None,
        enrichment_status=None,
        last_seen_at=None,
    )
    return LotPublicDTO(**lot.model_dump(), age_seconds=0, tier="match", freshness="hot")


class TestLotViewModelWasNew:
    """was_new contract for base and SSE-specific view-model variants."""

    def test_base_vm_was_new_is_false(self) -> None:
        """Base LotViewModel.was_new is always False (SSE fan-out path default)."""
        dto = _make_lot_dto()
        vm = LotViewModel(dto)
        assert vm.was_new is False

    def test_sse_lot_new_vm_was_new_is_true(self) -> None:
        """_SseLotNewViewModel.was_new is always True: lot.new = first-seen by definition."""
        dto = _make_lot_dto()
        vm = _SseLotNewViewModel(dto)
        assert vm.was_new is True


class TestSseLotNewRenderedHtml:
    """make_html_sse_encoder renders lot-card--new class for SseLotNew events."""

    def test_lot_new_event_renders_lot_card_new_class(self) -> None:
        """SseLotNew rendered HTML must contain 'lot-card--new' CSS class.

        Invariant: live SSE-inserted new lots must receive the orange accent
        border, same as server-rendered lots with was_new=True.
        """
        templates = build_templates()
        encoder = make_html_sse_encoder(templates.env)

        dto = _make_lot_dto()
        event = SseLotNew(lot=dto, fragment_template="poster")

        payload = encoder(event).decode()

        assert "lot-card--new" in payload, (
            f"Expected 'lot-card--new' class in SSE-rendered HTML for SseLotNew.\n"
            f"Got payload:\n{payload}"
        )


class TestSseStatusTooltip:
    """SseStatus tooltip invariant: last_new_at_hhmm must appear in rendered title."""

    def test_status_event_renders_hhmm_tooltip(self) -> None:
        """SseStatus with last_new_at_hhmm='14:23' must render title containing '(14:23)'.

        Invariant: the absolute-time tooltip must survive SSE status updates,
        not only the initial page render (regression for hiq3 fix).
        """
        templates = build_templates()
        encoder = make_html_sse_encoder(templates.env)

        event = SseStatus(
            timestamp=_TS,
            state="active",
            interval_minutes=5,
            last_new_human="47 мин назад",
            last_new_at_hhmm="14:23",
            expires_at_hhmm="",
        )

        payload = encoder(event).decode()

        assert "(14:23)" in payload, (
            f"Expected '(14:23)' in SSE-rendered header-status HTML for SseStatus.\n"
            f"Got payload:\n{payload}"
        )


class TestSseLoginSucceededEncoder:
    """SseLoginSucceeded encoder contract (fplb).

    Invariants:
    - Encoded frame starts with 'event: login.succeeded'.
    - Fragment includes hx-swap-oob (OOB mechanism for clearing stale state).
    - Fragment contains '#cycle-result' span (drops stale cycle-error message).
    """

    def test_encode_login_succeeded_renders_fragment(self) -> None:
        """Encoder returns a valid SSE frame for SseLoginSucceeded events."""
        templates = build_templates()
        encoder = make_html_sse_encoder(templates.env)

        event = SseLoginSucceeded(timestamp=_TS)
        payload = encoder(event).decode()

        assert payload.startswith("event: login.succeeded\n"), (
            f"Expected SSE frame to start with 'event: login.succeeded\\n'.\n"
            f"Got payload:\n{payload}"
        )
        assert payload.endswith("\n\n"), "SSE frame must end with double newline"

    def test_encode_login_succeeded_includes_oob_hint(self) -> None:
        """Encoded fragment must include hx-swap-oob to clear stale containers."""
        templates = build_templates()
        encoder = make_html_sse_encoder(templates.env)

        event = SseLoginSucceeded(timestamp=_TS)
        payload = encoder(event).decode()

        assert "hx-swap-oob" in payload, (
            f"Expected 'hx-swap-oob' in login.succeeded SSE fragment.\n"
            f"Got payload:\n{payload}"
        )
        assert "cycle-result" in payload, (
            f"Expected '#cycle-result' target in login.succeeded fragment.\n"
            f"Got payload:\n{payload}"
        )
