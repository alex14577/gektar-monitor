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

from fis_monitor.domain.models import (
    Lot,
    LotPublicDTO,
    SseCycleDone,
    SseLoginSucceeded,
    SseLotNew,
    SseStatus,
)
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


class TestLotViewModelUrlCadMap:
    """url_cad_map property invariants on LotViewModel."""

    def test_url_cad_map_format_with_colons(self) -> None:
        """url_cad_map returns ?cn= URL with literal colons for a valid cadastral_no."""
        dto = _make_lot_dto()
        vm = LotViewModel(dto)
        assert vm.url_cad_map == "https://ik5map.roscadastres.com/map.html?cn=01:02:000000:1"
        assert "%3A" not in (vm.url_cad_map or ""), "Colons must not be URL-encoded"

    def test_url_cad_map_none_when_empty_cadastral_no(self) -> None:
        """url_cad_map is None when cadastral_no is empty — button must be suppressed."""
        lot = Lot(
            id=2,
            cadastral_no="",
            area_sqm=None,
            region="R",
            municipality=None,
            land_category=None,
            permitted_use=None,
            ogv=None,
            status="active",
            date_create=_TS,
            date_update=None,
            lat=55.7,
            lon=37.6,
            has_boundaries=None,
            raw_json={},
            first_seen=_TS,
            last_seen=_TS,
            detail_fetched_at=None,
            enrichment_status=None,
            last_seen_at=None,
        )
        dto = LotPublicDTO(**lot.model_dump(), age_seconds=0, tier="match", freshness="hot")
        vm = LotViewModel(dto)
        assert vm.url_cad_map is None

    def test_url_cad_map_ignores_lat_lon(self) -> None:
        """url_cad_map must not depend on lat/lon — only on cadastral_no."""
        lot = Lot(
            id=3,
            cadastral_no="77:01:0006004:14",
            area_sqm=None,
            region="R",
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
        dto = LotPublicDTO(**lot.model_dump(), age_seconds=0, tier="match", freshness="hot")
        vm = LotViewModel(dto)
        assert vm.url_cad_map == "https://ik5map.roscadastres.com/map.html?cn=77:01:0006004:14"

    def test_rendered_html_contains_cad_map_link(self) -> None:
        """SSE-rendered poster must include the Роскадастр deep-link and label."""
        templates = build_templates()
        encoder = make_html_sse_encoder(templates.env)

        dto = _make_lot_dto()
        event = SseLotNew(lot=dto, fragment_template="poster")
        payload = encoder(event).decode()

        assert "ik5map.roscadastres.com" in payload, (
            f"Expected roscadastres deep-link in rendered HTML.\nGot:\n{payload}"
        )
        assert "На карту Роскадастр" in payload, (
            f"Expected label 'На карту Роскадастр' in rendered HTML.\nGot:\n{payload}"
        )
        assert "pkk.rosreestr.ru" not in payload, (
            f"Old PKK URL must be gone from rendered HTML.\nGot:\n{payload}"
        )
        assert 'class="copy"' not in payload, f"Icon copy button must be removed.\nGot:\n{payload}"
        assert 'class="lot__cad-copy"' in payload, (
            f"Inline copy button with class 'lot__cad-copy' must be present.\nGot:\n{payload}"
        )
        assert 'data-copy="01:02:000000:1"' in payload, (
            f"Button must carry data-copy with the cadastral number.\nGot:\n{payload}"
        )
        assert ">Скопировать<" not in payload, (
            f"Standalone 'Скопировать' button label must not appear"
            f" (old icon button gone).\nGot:\n{payload}"
        )


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


class TestSseStatusAbsoluteTime:
    """SseStatus absolute-time invariant: last_new_human is rendered verbatim in the chip."""

    def test_status_event_renders_last_new_human(self) -> None:
        """SseStatus.last_new_human must appear verbatim in the rendered header chip.

        Invariant: the absolute local-time string (e.g. '17:35') must survive
        SSE status updates, not only the initial page render.
        """
        templates = build_templates()
        encoder = make_html_sse_encoder(templates.env)

        event = SseStatus(
            timestamp=_TS,
            state="active",
            interval_minutes=5,
            last_new_human="17:35",
            expires_at_hhmm="",
        )

        payload = encoder(event).decode()

        assert "17:35" in payload, (
            f"Expected '17:35' (last_new_human) in SSE-rendered header-status HTML.\n"
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
            f"Expected 'hx-swap-oob' in login.succeeded SSE fragment.\nGot payload:\n{payload}"
        )
        assert "cycle-result" in payload, (
            f"Expected '#cycle-result' target in login.succeeded fragment.\nGot payload:\n{payload}"
        )


class TestSseCycleDoneRenderedHtml:
    """cycle.done ok-branch renders 'Проверка завершена в HH:MM', not counters (bd nq5g)."""

    def test_ok_fragment_renders_finished_at_hhmm_and_no_counters(self) -> None:
        """Invariant: ok-branch shows local time; lots/duration counters must NOT appear."""
        templates = build_templates()
        encoder = make_html_sse_encoder(templates.env)

        event = SseCycleDone(
            timestamp=_TS,
            cycle_id=1,
            status="ok",
            lots_fetched=12,
            new_lots=3,
            duration_ms=1400,
            finished_at_hhmm="14:05",
        )

        payload = encoder(event).decode()

        assert "Проверка завершена в 14:05" in payload, (
            f"Expected 'Проверка завершена в 14:05' in ok-branch fragment.\nGot:\n{payload}"
        )
        assert "лотов" not in payload, (
            f"Counter text ('лотов') must not appear in ok-branch fragment.\nGot:\n{payload}"
        )

    def test_ok_fragment_drops_dangling_preposition_when_hhmm_empty(self) -> None:
        """Regression: empty finished_at_hhmm (error-branch fallback / old payload)
        must render 'Проверка завершена', never a dangling 'Проверка завершена в '."""
        templates = build_templates()
        encoder = make_html_sse_encoder(templates.env)

        event = SseCycleDone(
            timestamp=_TS,
            cycle_id=1,
            status="ok",
            lots_fetched=0,
            new_lots=0,
            duration_ms=0,
            finished_at_hhmm="",
        )

        payload = encoder(event).decode()

        assert "Проверка завершена в" not in payload, (
            f"Dangling preposition 'в' must not render when HH:MM is empty.\nGot:\n{payload}"
        )
        assert "Проверка завершена" in payload, (
            f"Expected 'Проверка завершена' fallback in ok-branch fragment.\nGot:\n{payload}"
        )
