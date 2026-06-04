"""MonitorVM — shared header-status view-model factory (bd 47uh).

Single source of truth for the ``#header-status`` partial context. Replaces
the ad-hoc ``SimpleNamespace`` builders that were sprinkled across
``main.py``, ``notifications.py``, and ``settings.py`` with hardcoded
placeholders. The factory reads real data from ``LotRepository``
(``latest_new_first_seen``) and the session view-model so the widget
shows live state on every page load.

Live updates flow via the ``SseStatus`` event published by
``MonitorCycleService._publish_cycle_done`` and rendered by
``web/sse_encoder._encode_status``. Initial render uses this same VM
to avoid the spinner-vs-real-data discrepancy on first paint.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fis_monitor.domain.interfaces import LotRepository
from fis_monitor.domain.models import Settings
from fis_monitor.services.humanize import format_local_time


def _state_from_session(session: SimpleNamespace, *, awaiting_backfill: bool = False) -> str:
    """Map session flags → header traffic-light state.

    Order matters: awaiting_backfill beats session states; expired beats expiring.
    """
    if awaiting_backfill:
        return "awaiting_backfill"
    if getattr(session, "expired", False):
        return "error"
    if getattr(session, "expires_soon", False):
        return "warning"
    return "active"


def build_monitor_vm(
    *,
    settings: Settings,
    session: SimpleNamespace,
    lot_repo: LotRepository,
    now: datetime,
    awaiting_backfill: bool = False,
) -> SimpleNamespace:
    """Build the header-status view-model for an initial template render.

    Args:
        settings: live ``Settings`` snapshot — supplies ``interval_minutes``
            and ``timezone`` (used to convert UTC lot timestamps to local time).
        session: the same ``session`` namespace that the layout uses for
            expiry rendering. Provides ``expires_at_hhmm`` (best-effort,
            may be empty until ``HttpSessionProbe`` is wired).
        lot_repo: source for ``MAX(first_seen)`` — drives ``last_new_human``.
        now: injected wallclock (UTC-aware) so tests don't drift.

    Countdown fields (``next_cycle_mmss``, ``next_fire_at_iso``) are removed
    by hiq3 — superseded by binary cycle.started / cycle.done events
    (ADR-050; UI pulse-dot consumer further removed in lw5s).
    """
    tz = ZoneInfo(settings.timezone)
    now_local = now.astimezone(tz)
    last_new = lot_repo.latest_new_first_seen()
    last_new_human = (
        "—" if last_new is None else format_local_time(last_new, tz, now_local)
    )

    return SimpleNamespace(
        state=_state_from_session(session, awaiting_backfill=awaiting_backfill),
        interval_minutes=settings.interval_minutes,
        last_new_human=last_new_human,
        expires_at_hhmm=getattr(session, "expires_at_hhmm", ""),
    )
