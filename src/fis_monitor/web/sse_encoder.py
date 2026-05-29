"""HTML-rendering SSE encoder for the web layer.

Responsibility (SRP): convert ``SseLotNew`` events into Jinja2-rendered HTML
fragments that htmx's sse-extension can swap directly into the feed DOM.
All other event types fall back to the default JSON encoder.

Design:
  - ``make_html_sse_encoder(env)`` is a factory that captures the Jinja2
    ``Environment`` and returns a ``Callable[[SseEvent], bytes]`` compatible
    with ``SseStreamer(event_encoder=...)``.
  - ``LotViewModel`` wraps ``LotPublicDTO`` and provides the presentation fields
    that ``_lot_poster.html.jinja`` expects but that do not exist on the domain
    DTO (computed fields, user-state defaults).
  - The encoder is registered in ``app.py`` lifespan via
    ``SseStreamer.bind_event_encoder(make_html_sse_encoder(templates.env))``.

SSE line discipline (RFC 8895):
  HTML may contain newlines — each line is emitted as a separate ``data:``
  field so the browser's event-source parser reassembles them correctly.
  This mirrors the same multi-line handling in the JSON encoder
  (``infra/sse/sse_stream.py::encode_sse_event``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

from jinja2 import Environment

from fis_monitor.domain.map_links import roscadastres_map_url
from fis_monitor.domain.models import (
    LotPublicDTO,
    LotUserDTO,
    SseCycleDone,
    SseEvent,
    SseLoginSucceeded,
    SseLotNew,
    SseStatus,
)
from fis_monitor.infra.sse.sse_stream import encode_sse_event

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)

# Map fragment_template → Jinja2 partial filename.
#
# Only ``"poster"`` is supported.  Adding a new fragment_template requires:
#   (a) adding its entry here,
#   (b) ensuring ``LotViewModel`` provides all fields the partial references,
#   (c) a matching producer-side change in ``infra/sse/browser_sse_notifier.py``.
#
# "list" is intentionally absent: ``_lot_list.html.jinja`` references fields
# (``lot.category_short``, ``lot.vri_short``, ``lot.gone_at_human``,
# ``lot.new_status_human``) that ``LotViewModel`` does NOT define, and the
# producer never publishes fragment_template="list".  Keeping the entry would
# cause a silent Jinja2 Undefined rendering bug.
_TEMPLATE_MAP: dict[str, str] = {
    "poster": "partials/_lot_poster.html.jinja",
}

# Fallback session context for SSE rendering: no per-user session state is
# available in the fan-out path (SSE is broadcast, not per-user).
_SSE_SESSION_CTX = SimpleNamespace(expired=False)


class LotViewModel:
    """Presentation wrapper around ``LotPublicDTO``.

    Provides computed and default fields that the lot-partial templates expect
    but that are not present on the domain DTO (user-state, human-readable
    derived values, external URL, etc.).

    All user-state fields default to "not seen / no note" —
    correct for brand-new lots arriving via SSE.
    """

    __slots__ = ("_dto",)

    def __init__(self, dto: LotPublicDTO) -> None:
        self._dto = dto

    # --- Pass-through domain fields ----------------------------------------

    @property
    def id(self) -> int:
        return self._dto.id

    @property
    def cadastral_no(self) -> str:
        return self._dto.cadastral_no

    @property
    def region(self) -> str:
        return self._dto.region

    @property
    def status(self) -> str:
        return self._dto.status

    @property
    def ogv(self) -> str | None:
        return self._dto.ogv

    @property
    def raw_json(self) -> dict:  # type: ignore[type-arg]
        return {}  # stripped from public DTO; omit from rendered HTML

    # --- Computed / derived presentation fields ----------------------------

    @property
    def area_ha(self) -> str:
        """Area in hectares, formatted to 2 decimal places.

        ``area_sqm`` may be None (detail not yet fetched).  Returns "—" in
        that case so the template renders a placeholder rather than crashing.
        """
        if self._dto.area_sqm is None:
            return "—"
        ha = self._dto.area_sqm / 10_000
        return f"{ha:.2f}"

    @property
    def category(self) -> str:
        """Human-readable land category (falls back to «—»)."""
        return self._dto.land_category or "—"

    @property
    def district(self) -> str | None:
        """Municipality / district (None → template omits the district chip)."""
        return self._dto.municipality

    @property
    def vri(self) -> str | None:
        """Permitted use (вид разрешённого использования)."""
        return self._dto.permitted_use

    @property
    def external_id(self) -> str | None:
        """External lot ID on the upstream site (from cadastral_no suffix)."""
        return None  # not yet parsed into a dedicated field

    @property
    def url(self) -> str:
        """Direct link to the lot on the upstream site (placeholder)."""
        # TorgiUrlBuilder requires Settings.base_url — not available here.
        # Return a stable relative URL so the button is rendered but harmless.
        return f"/lots/{self._dto.id}/redirect"

    @property
    def url_cad_map(self) -> str | None:
        """Роскадастр deep-link — requires only cadastral_no (no lat/lon needed)."""
        return roscadastres_map_url(self._dto.cadastral_no)

    @property
    def coords_decimal(self) -> str:
        """Decimal degree coordinates (e.g. «55.7558° N, 37.6176° E»)."""
        lat = self._dto.lat
        lon = self._dto.lon
        if lat is None or lon is None:
            return "—"
        return f"{lat:.4f}° N, {lon:.4f}° E"

    @property
    def coords_dms(self) -> str:
        """DMS coordinate string for the title attribute."""
        lat = self._dto.lat
        lon = self._dto.lon
        if lat is None or lon is None:
            return "—"
        return f"{_decimal_to_dms(lat, 'N', 'S')}, {_decimal_to_dms(lon, 'E', 'W')}"

    @property
    def first_seen_at_human(self) -> str:
        """Human-readable first-seen date (locale-agnostic short format)."""
        return self._dto.first_seen.strftime("%d.%m.%Y %H:%M")

    @property
    def date_create(self):  # type: ignore[return]
        """FIS site publication date as datetime — for the ``dateformat`` Jinja2 filter (hiq3)."""
        return self._dto.date_create

    @property
    def date_registry(self):  # type: ignore[return]
        """EGRN registration date as datetime | None — for ``dateformat`` Jinja2 filter (hiq3)."""
        return self._dto.date_registry

    @property
    def published_at_human(self) -> str:
        """FIS site publication date — DATE_CREATE, when lot was added to FIS DB (NOT EGRN)."""
        return self._dto.date_create.strftime("%d.%m.%Y %H:%M")

    @property
    def has_registry_date(self) -> bool:
        """True when EGRN registration date has been fetched (enrichment complete)."""
        return self._dto.date_registry is not None

    @property
    def registry_date_human(self) -> str:
        """EGRN registration date — «Дата постановки на учет» from detail page.

        Returns «—» when date_registry is not yet fetched (enrichment pending).
        """
        if self._dto.date_registry is None:
            return "—"
        return self._dto.date_registry.strftime("%d.%m.%Y")

    @property
    def temp(self) -> str:
        """Freshness tier for CSS class (maps freshness → temp value)."""
        return self._dto.freshness  # "hot" / "warm" / "cool" / "cold"

    @property
    def event(self) -> str | None:
        """Lot lifecycle event for data-event attribute (None for new lots)."""
        return None  # new lots have no lifecycle event

    @property
    def category_short(self) -> str:
        """Short category label for the dense list card (falls back to «с/х»)."""
        cat = self._dto.land_category
        if not cat:
            return ""
        return cat.split(",")[0].strip()

    @property
    def vri_short(self) -> str | None:
        """Short permitted-use label for the dense list card."""
        vri = self._dto.permitted_use
        if not vri:
            return None
        return vri.split(",")[0].strip()

    @property
    def gone_at_human(self) -> str:
        """Time since the lot left "Свободен" — empty for active lots."""
        return ""

    @property
    def new_status_human(self) -> str:
        """New status label for gone-event lots — empty for active lots."""
        return ""

    # --- User-state fields (defaults for SSE fan-out path) -----------------

    @property
    def is_seen(self) -> bool:
        """Always False for brand-new lots arriving via SSE."""
        return False

    @property
    def note(self) -> str | None:
        return None

    @property
    def was_new(self) -> bool:
        """hiq3: always False for SSE fan-out path; LotUserViewModel overrides from DTO."""
        return False

    @property
    def is_backfill(self) -> bool:
        """False for server-rendered feed and base SSE path; overridden by _SseLotNewViewModel."""
        return False


class _SseLotNewViewModel(LotViewModel):
    """``LotViewModel`` variant for ``SseLotNew`` events.

    Overrides ``was_new`` to ``True`` because ``lot.new`` semantically means
    first-seen: the encoder already knows the event type, so the override lives
    here rather than widening the domain DTO contract.

    Also carries ``is_backfill`` so ``_lot_poster.html.jinja`` can stamp
    ``data-backfill="1"`` — the JS layer reads this attribute to reposition
    the card to the bottom of the feed without sound/notification (dr21).
    """

    __slots__ = ("_is_backfill",)

    def __init__(self, dto: LotPublicDTO, *, is_backfill: bool = False) -> None:
        super().__init__(dto)
        self._is_backfill = is_backfill

    @property
    def was_new(self) -> bool:
        """Always True: a lot.new event is, by definition, first-seen."""
        return True

    @property
    def is_backfill(self) -> bool:
        """True when the originating SseLotNew event came from BackfillService."""
        return self._is_backfill


class LotUserViewModel(LotViewModel):
    """``LotViewModel`` variant that surfaces per-user state from a ``LotUserDTO``.

    Used by the server-rendered feed (``GET /``) where ``seen_at`` / ``note``
    are loaded from ``UserStateRepository``.  The SSE fan-out path keeps
    using the base ``LotViewModel`` (no per-user state available).
    """

    def __init__(self, dto: LotUserDTO) -> None:
        super().__init__(dto)

    @property
    def _user_dto(self) -> LotUserDTO:
        return self._dto  # type: ignore[return-value]

    @property
    def is_seen(self) -> bool:
        return self._user_dto.seen_at is not None

    @property
    def note(self) -> str | None:
        return self._user_dto.note

    @property
    def was_new(self) -> bool:
        """hiq3: surface was_new from LotUserDTO for the accent border-left CSS class."""
        return self._user_dto.was_new


def _decimal_to_dms(degrees: float, pos_suffix: str, neg_suffix: str) -> str:
    """Convert decimal degrees to a DMS string (e.g. «55°45'21"N»)."""
    suffix = pos_suffix if degrees >= 0 else neg_suffix
    degrees = abs(degrees)
    d = int(degrees)
    m_float = (degrees - d) * 60
    m = int(m_float)
    s = (m_float - m) * 60
    return f"{d}°{m}'{s:.0f}\"{suffix}"


def make_html_sse_encoder(env: Environment) -> Callable[[SseEvent], bytes]:
    """Return an SSE encoder that renders HTML fragments for lot events.

    For ``SseLotNew`` events: renders the Jinja2 partial specified by
    ``fragment_template`` (currently only ``"poster"`` is supported) and wraps
    the HTML in SSE format (``event: lot.new\\ndata: <line>\\n...\\n\\n``).
    An unknown ``fragment_template`` value triggers a warning and falls back to
    JSON encoding (the same path used for non-SseLotNew events).

    For all other event types: delegates to ``encode_sse_event`` (JSON).

    Args:
        env: Jinja2 ``Environment`` from ``app.state.templates.env``.

    Returns:
        A ``Callable[[SseEvent], bytes]`` suitable for
        ``SseStreamer(event_encoder=...)``.
    """

    def _encode(event: SseEvent) -> bytes:
        if isinstance(event, SseCycleDone):
            return _encode_cycle_done(env, event)
        if isinstance(event, SseStatus):
            return _encode_status(env, event)
        if isinstance(event, SseLoginSucceeded):
            return _encode_login_succeeded(env, event)

        if not isinstance(event, SseLotNew):
            return encode_sse_event(event)

        template_name = _TEMPLATE_MAP.get(event.fragment_template)
        if template_name is None:
            # Unknown template variant — log a warning and fall back to JSON
            # encoding.  This matches the existing non-SseLotNew fallback path
            # and avoids crashing or silently rendering a broken HTML fragment.
            _log.warning(
                "sse_encoder.unknown_fragment_template",
                extra={"fragment_template": event.fragment_template},
            )
            return encode_sse_event(event)

        template = env.get_template(template_name)
        vm = _SseLotNewViewModel(event.lot, is_backfill=event.is_backfill)
        html: str = template.render(lot=vm, session=_SSE_SESSION_CTX)

        # RFC 8895 §9.2.5: split on newlines so each line starts with "data:".
        data_lines = "\n".join(f"data: {line}" for line in html.split("\n"))
        return f"event: {event.event}\n{data_lines}\n\n".encode()

    return _encode


_CYCLE_DONE_TEMPLATE = "partials/_cycle_done.html.jinja"
_HEADER_STATUS_TEMPLATE = "partials/_header_status.html.jinja"


def _encode_status(env: Environment, event: SseStatus) -> bytes:
    """Render ``SseStatus`` → HTML for the ``#header-status`` widget (bd 47uh).

    The partial expects a ``monitor`` namespace with the same field shape
    as the initial-render VM produced by ``web/monitor_vm.build_monitor_vm``;
    SseStatus carries that shape directly so we pass the event through.
    """
    template = env.get_template(_HEADER_STATUS_TEMPLATE)
    html: str = template.render(monitor=event)
    data_lines = "\n".join(f"data: {line}" for line in html.split("\n"))
    return f"event: {event.event}\n{data_lines}\n\n".encode()


def _encode_cycle_done(env: Environment, event: SseCycleDone) -> bytes:
    """Render an ``SseCycleDone`` to an HTML SSE chunk for ``#cycle-result``.

    The partial receives ``event`` as its sole context variable — no PII path
    (counters + cycle_id only). Mirrors the SSE multi-line ``data:`` discipline
    used for ``lot.new`` poster rendering above.
    """
    template = env.get_template(_CYCLE_DONE_TEMPLATE)
    html: str = template.render(event=event)
    data_lines = "\n".join(f"data: {line}" for line in html.split("\n"))
    return f"event: {event.event}\n{data_lines}\n\n".encode()


_LOGIN_SUCCEEDED_TEMPLATE = "partials/_login_succeeded.html.jinja"


def _encode_login_succeeded(env: Environment, event: SseLoginSucceeded) -> bytes:
    """Render an ``SseLoginSucceeded`` to an HTML SSE chunk.

    The fragment uses ``hx-swap-oob`` to clear ``#cycle-result`` (drops the
    stale «Проверка завершена с ошибкой» fragment from the pre-login cycle)
    and to hide ``#session-expired-modal`` if present.

    Targeted by ``#login-succeeded-listener`` (``sse-swap="login.succeeded"``)
    in ``base.html.jinja``.
    """
    template = env.get_template(_LOGIN_SUCCEEDED_TEMPLATE)
    html: str = template.render(event=event)
    data_lines = "\n".join(f"data: {line}" for line in html.split("\n"))
    return f"event: {event.event}\n{data_lines}\n\n".encode()
