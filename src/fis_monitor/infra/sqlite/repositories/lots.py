"""SQLite implementation of ``LotRepository``.

Algorithm (upsert):
  1. BEGIN IMMEDIATE — capture writer-lock before first SELECT.
  2. SELECT existing lot by id (inside tx) — old_lot | None.
  3. changes = compute_changes(old_lot, lot, tracked)  ← R3-C2 (INSIDE tx).
  4. INSERT (was_new=True) or UPDATE (was_new=False) into lots.
  5. INSERT lots_history rows for each FieldChange (json-encoded values).
  6. _sync_geo(conn, lot.id, old_coords, new_coords) — R-tree sync.
  7. COMMIT; rollback on any exception.
  8. Return LotUpsertResult(was_new, changes).

See:
  docs/decisions/ADR-016-repository-invariants-begin-immediate.md
  docs/architecture/03-protocols.md §3.1
  docs/data-model/lot.md
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fis_monitor.domain.diff import ALLOWED_TRACKED_FIELDS, compute_changes
from fis_monitor.domain.interfaces import Clock
from fis_monitor.domain.models import (
    FieldChange,
    Lot,
    LotUpsertResult,
    TrackedField,
)
from fis_monitor.infra.sqlite.connection import ConnectionProvider

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    """Serialise a datetime to ISO-8601 string for storage, or None."""
    if dt is None:
        return None
    return dt.isoformat()


def _parse_dt(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 string from DB; restore UTC tzinfo if naive."""
    if raw is None:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def row_to_lot(row: sqlite3.Row | tuple) -> Lot:
    """Convert a DB row (all lots columns in schema order) to a Lot."""
    (
        id_,
        cadastral_no,
        area_sqm,
        region,
        municipality,
        land_category,
        permitted_use,
        ogv,
        status,
        date_create_raw,
        date_update_raw,
        lat,
        lon,
        has_boundaries_raw,
        raw_json_raw,
        parser_version,
        first_seen_raw,
        last_seen_raw,
        detail_fetched_at_raw,
        enrichment_status,
        _enrichment_retries,  # not in Lot model
        _enrichment_last_error,  # not in Lot model
        last_seen_at_raw,
        _last_status,  # not in Lot model
        _last_status_at_raw,  # not in Lot model
        is_active_raw,
        inactive_reason,
        inactive_since_raw,
        inactive_confirmed_at_raw,
        region_id,
    ) = row

    has_boundaries: bool | None = None
    if has_boundaries_raw is not None:
        has_boundaries = bool(has_boundaries_raw)

    raw_json: dict[str, Any] = {}
    if raw_json_raw:
        raw_json = json.loads(raw_json_raw)

    return Lot(
        id=id_,
        cadastral_no=cadastral_no,
        area_sqm=area_sqm,
        region=region,
        municipality=municipality,
        land_category=land_category,
        permitted_use=permitted_use,
        ogv=ogv,
        status=status,
        date_create=_parse_dt(date_create_raw),  # type: ignore[arg-type]
        date_update=_parse_dt(date_update_raw),
        lat=lat,
        lon=lon,
        has_boundaries=has_boundaries,
        raw_json=raw_json,
        parser_version=parser_version,
        first_seen=_parse_dt(first_seen_raw),  # type: ignore[arg-type]
        last_seen=_parse_dt(last_seen_raw),  # type: ignore[arg-type]
        detail_fetched_at=_parse_dt(detail_fetched_at_raw),
        enrichment_status=enrichment_status,
        last_seen_at=_parse_dt(last_seen_at_raw),
        is_active=bool(is_active_raw),
        inactive_reason=inactive_reason,
        inactive_since=_parse_dt(inactive_since_raw),
        inactive_confirmed_at=_parse_dt(inactive_confirmed_at_raw),
        region_id=region_id,
    )


_LOT_SELECT = (
    "id, cadastral_no, area_sqm, region, municipality, land_category, "
    "permitted_use, ogv, status, date_create, date_update, lat, lon, "
    "has_boundaries, raw_json, parser_version, first_seen, last_seen, "
    "detail_fetched_at, enrichment_status, enrichment_retries, "
    "enrichment_last_error, last_seen_at, last_status, last_status_at, "
    "is_active, inactive_reason, inactive_since, inactive_confirmed_at, "
    "region_id"
)


# ---------------------------------------------------------------------------
# R-tree sync (private, called only inside upsert tx)
# ---------------------------------------------------------------------------

def _bool_to_int(value: bool | None) -> int | None:
    """Convert bool → 0/1 for SQLite INTEGER columns, or None."""
    if value is None:
        return None
    return 1 if value else 0


def _json_encode(value: Any) -> str:
    """Encode a FieldChange value to JSON string for lots_history storage.

    Datetime objects are converted to ISO-8601 strings before encoding so
    that ``json.dumps`` does not raise ``TypeError``.
    """
    if isinstance(value, datetime):
        return json.dumps(value.isoformat(), ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _has_coords(lat: float | None, lon: float | None) -> bool:
    """Both coordinates must be non-NULL for an R-tree entry."""
    return lat is not None and lon is not None


def _sync_geo(
    conn: sqlite3.Connection,
    lot_id: int,
    old_lat: float | None,
    old_lon: float | None,
    new_lat: float | None,
    new_lon: float | None,
) -> None:
    """Synchronise ``lots_rtree`` inside an existing BEGIN IMMEDIATE tx.

    Five cases (ADR-016 R3-M8):
    1. NULL → NULL:   no-op.
    2. NULL → value:  INSERT point row.
    3. value → NULL:  DELETE row.
    4. value → other: UPDATE (INSERT OR REPLACE) row.
    5. value → same:  no-op.

    ``NULL`` means "at least one coordinate is NULL"; a full (lat, lon) pair
    is required for an R-tree entry.
    """
    old_has = _has_coords(old_lat, old_lon)
    new_has = _has_coords(new_lat, new_lon)

    if not old_has and not new_has:
        # Case 1 — nothing to do.
        return

    if not old_has and new_has:
        # Case 2 — INSERT new point (minLat=maxLat=lat, minLon=maxLon=lon).
        conn.execute(
            "INSERT INTO lots_rtree(id, min_lat, max_lat, min_lon, max_lon)"
            " VALUES (?, ?, ?, ?, ?)",
            (lot_id, new_lat, new_lat, new_lon, new_lon),
        )
        return

    if old_has and not new_has:
        # Case 3 — DELETE existing row.
        conn.execute("DELETE FROM lots_rtree WHERE id = ?", (lot_id,))
        return

    # old_has and new_has — check for change (Cases 4 / 5).
    if old_lat == new_lat and old_lon == new_lon:
        # Case 5 — no-op.
        return

    # Case 4 — UPDATE via INSERT OR REPLACE (equivalent, simpler).
    conn.execute(
        "INSERT OR REPLACE INTO lots_rtree(id, min_lat, max_lat, min_lon, max_lon)"
        " VALUES (?, ?, ?, ?, ?)",
        (lot_id, new_lat, new_lat, new_lon, new_lon),
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class SqliteLotRepository:
    """SQLite-backed ``LotRepository``.

    Responsibilities (SRP): CRUD + tx-invariants for ``lots``,
    ``lots_history``, ``lots_rtree`` and the ``state`` k/v table (for
    ``last_known_id``). Business rules (diff-policy, enrichment scheduling)
    live outside this class.

    DI via constructor::

        repo = SqliteLotRepository(
            conn_provider=provider,
            clock=SystemClock(),
        )
    """

    def __init__(self, *, conn_provider: ConnectionProvider, clock: Clock) -> None:
        self._conn_provider = conn_provider
        self._clock = clock

    # ------------------------------------------------------------------
    # upsert
    # ------------------------------------------------------------------

    def upsert(
        self,
        lot: Lot,
        *,
        tracked: Sequence[TrackedField],
    ) -> LotUpsertResult:
        """Insert-or-update ``lot`` atomically.

        ``compute_changes`` is called INSIDE the ``BEGIN IMMEDIATE``
        transaction so there is no TOCTOU window between SELECT-old and
        UPDATE (R3-C2 / ADR-016).

        Notification dispatch is caller-side: ``BackfillService`` does not
        call the dispatcher after upsert; ``MonitorCycleService`` does.
        The ``notify`` parameter has been removed (P1-3 dead parameter).
        """
        # Defence-in-depth: validate tracked fields before acquiring the writer
        # lock. compute_changes performs the same check inside the tx; this
        # guard catches callers that bypass static type checking early.
        for field in tracked:
            if field not in ALLOWED_TRACKED_FIELDS:
                raise ValueError(f"Unknown tracked field {field!r}")

        conn = self._conn_provider.get()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Step 2 — SELECT existing row inside tx.
            cur = conn.execute(
                f"SELECT {_LOT_SELECT} FROM lots WHERE id = ?",
                (lot.id,),
            )
            row = cur.fetchone()
            cur.close()

            old_lot: Lot | None = row_to_lot(row) if row is not None else None

            # Step 3 — compute diff inside tx (R3-C2).
            changes: list[FieldChange] = compute_changes(old_lot, lot, tracked)

            old_lat = old_lot.lat if old_lot is not None else None
            old_lon = old_lot.lon if old_lot is not None else None

            if old_lot is None:
                # Step 4a — INSERT.
                conn.execute(
                    "INSERT INTO lots("
                    "  id, cadastral_no, area_sqm, region, municipality,"
                    "  land_category, permitted_use, ogv, status,"
                    "  date_create, date_update, lat, lon, has_boundaries,"
                    "  raw_json, parser_version, first_seen, last_seen,"
                    "  detail_fetched_at, enrichment_status, enrichment_retries,"
                    "  last_seen_at, last_status, last_status_at,"
                    "  is_active, inactive_reason, inactive_since, inactive_confirmed_at,"
                    "  region_id"
                    ") VALUES ("
                    "  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    "  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
                    ")",
                    (
                        lot.id,
                        lot.cadastral_no,
                        lot.area_sqm,
                        lot.region,
                        lot.municipality,
                        lot.land_category,
                        lot.permitted_use,
                        lot.ogv,
                        lot.status,
                        _iso(lot.date_create),
                        _iso(lot.date_update),
                        lot.lat,
                        lot.lon,
                        _bool_to_int(lot.has_boundaries),
                        json.dumps(lot.raw_json, ensure_ascii=False),
                        lot.parser_version,
                        _iso(lot.first_seen),
                        _iso(lot.last_seen),
                        _iso(lot.detail_fetched_at),
                        lot.enrichment_status,
                        0,  # enrichment_retries default
                        _iso(lot.last_seen_at),
                        None,  # last_status — not in Lot model
                        None,  # last_status_at — not in Lot model
                        1 if lot.is_active else 0,
                        lot.inactive_reason,
                        _iso(lot.inactive_since),
                        _iso(lot.inactive_confirmed_at),
                        lot.region_id,
                    ),
                )
                was_new = True
            else:
                # Step 4b — UPDATE.
                conn.execute(
                    "UPDATE lots SET"
                    "  cadastral_no = ?, area_sqm = ?, region = ?,"
                    "  municipality = ?, land_category = ?, permitted_use = ?,"
                    "  ogv = ?, status = ?, date_create = ?, date_update = ?,"
                    "  lat = ?, lon = ?, has_boundaries = ?, raw_json = ?,"
                    "  parser_version = ?, first_seen = ?, last_seen = ?,"
                    "  detail_fetched_at = ?, enrichment_status = ?,"
                    "  last_seen_at = ?, is_active = ?,"
                    "  inactive_reason = ?, inactive_since = ?,"
                    "  inactive_confirmed_at = ?, region_id = ?"
                    " WHERE id = ?",
                    (
                        lot.cadastral_no,
                        lot.area_sqm,
                        lot.region,
                        lot.municipality,
                        lot.land_category,
                        lot.permitted_use,
                        lot.ogv,
                        lot.status,
                        _iso(lot.date_create),
                        _iso(lot.date_update),
                        lot.lat,
                        lot.lon,
                        _bool_to_int(lot.has_boundaries),
                        json.dumps(lot.raw_json, ensure_ascii=False),
                        lot.parser_version,
                        _iso(lot.first_seen),
                        _iso(lot.last_seen),
                        _iso(lot.detail_fetched_at),
                        lot.enrichment_status,
                        _iso(lot.last_seen_at),
                        1 if lot.is_active else 0,
                        lot.inactive_reason,
                        _iso(lot.inactive_since),
                        _iso(lot.inactive_confirmed_at),
                        lot.region_id,
                        lot.id,
                    ),
                )
                was_new = False

            # Step 5 — INSERT lots_history rows (json-encoded values).
            changed_at = _iso(self._clock.now())
            for change in changes:
                conn.execute(
                    "INSERT INTO lots_history(lot_id, field, old_value, new_value, changed_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        lot.id,
                        change.field,
                        _json_encode(change.old_value),
                        _json_encode(change.new_value),
                        changed_at,
                    ),
                )

            # Step 6 — sync R-tree (private, inside tx).
            _sync_geo(conn, lot.id, old_lat, old_lon, lot.lat, lot.lon)

            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return LotUpsertResult(was_new=was_new, changes=changes)

    # ------------------------------------------------------------------
    # get / list_active
    # ------------------------------------------------------------------

    def get(self, lot_id: int) -> Lot | None:
        """Return the lot by primary key, or ``None`` if not found."""
        conn = self._conn_provider.get()
        cur = conn.execute(
            f"SELECT {_LOT_SELECT} FROM lots WHERE id = ?",
            (lot_id,),
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        return row_to_lot(row)

    def list_active(self, *, limit: int, offset: int) -> list[Lot]:
        """Return active lots ordered by ``date_create DESC``."""
        conn = self._conn_provider.get()
        cur = conn.execute(
            f"SELECT {_LOT_SELECT} FROM lots"
            " WHERE is_active = 1"
            " ORDER BY date_create DESC"
            " LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = cur.fetchall()
        cur.close()
        return [row_to_lot(r) for r in rows]

    # ------------------------------------------------------------------
    # last_known_id — stored in state k/v table as "last_known_id_<region>"
    # ------------------------------------------------------------------

    def get_last_known_id(self, region: int) -> int | None:
        """Return the last known lot-id for a region, or ``None``."""
        key = f"last_known_id_{region}"
        conn = self._conn_provider.get()
        cur = conn.execute("SELECT value FROM state WHERE key = ?", (key,))
        row = cur.fetchone()
        cur.close()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def set_last_known_id(self, region: int, value: int) -> None:
        """Persist the last known lot-id for a region.  BEGIN IMMEDIATE."""
        key = f"last_known_id_{region}"
        now_iso = _iso(self._clock.now())
        conn = self._conn_provider.get()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                "   updated_at = excluded.updated_at",
                (key, str(value), now_iso),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------------
    # mark_seen / mark_inactive
    # ------------------------------------------------------------------

    def mark_seen(self, lot_ids: Sequence[int], at: datetime) -> None:
        """Bulk-update ``last_seen`` / ``last_seen_at`` for the given lot-ids.

        Single ``BEGIN IMMEDIATE`` transaction — all rows updated atomically.
        """
        if not lot_ids:
            return
        at_iso = _iso(at)
        placeholders = ",".join("?" for _ in lot_ids)
        conn = self._conn_provider.get()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                f"UPDATE lots SET last_seen = ?, last_seen_at = ?"
                f" WHERE id IN ({placeholders})",
                (at_iso, at_iso, *lot_ids),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def mark_inactive(self, lot_id: int, reason: str, at: datetime) -> None:
        """Mark a lot as inactive.  BEGIN IMMEDIATE."""
        at_iso = _iso(at)
        conn = self._conn_provider.get()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE lots SET"
                "  is_active = 0,"
                "  inactive_reason = ?,"
                "  inactive_since = ?,"
                "  inactive_confirmed_at = ?"
                " WHERE id = ?",
                (reason, at_iso, at_iso, lot_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------------
    # needing_enrichment
    # ------------------------------------------------------------------

    def needing_enrichment(self, limit: int) -> list[int]:
        """Return lot-ids waiting for detail enrichment.

        Selects lots where ``enrichment_status`` is NULL or ``'pending'``
        AND ``detail_fetched_at`` is NULL.  Cursor closed before return.
        """
        conn = self._conn_provider.get()
        cur = conn.execute(
            "SELECT id FROM lots"
            " WHERE (enrichment_status IS NULL OR enrichment_status = 'pending')"
            "   AND detail_fetched_at IS NULL"
            " LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # count_active
    # ------------------------------------------------------------------

    def count_active(self, region_id: int | None = None) -> int:
        """Return the number of active lots.

        When ``region_id`` is given, filters by ``lots.region_id`` too.
        Single read-only ``SELECT COUNT(*)`` — no BEGIN IMMEDIATE needed.
        """
        conn = self._conn_provider.get()
        if region_id is None:
            cur = conn.execute("SELECT COUNT(*) FROM lots WHERE is_active = 1")
        else:
            cur = conn.execute(
                "SELECT COUNT(*) FROM lots WHERE is_active = 1 AND region_id = ?",
                (region_id,),
            )
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0
