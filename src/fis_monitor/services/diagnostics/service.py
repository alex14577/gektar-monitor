"""DiagnosticsService — diagnostic bundle generator with explicit allow-list.

Architecture context:
  - ADR-012: explicit allow-list + redactor for diagnostic.zip
  - ADR-017: secrets / crash-dump exclusion (SecretStr, *.dmp / core.*)
  - R3-M5:   schema-snapshot fail-closed (SchemaDriftError → DiagnosticUnavailable)
  - R4-M7:   audit.jsonl excluded when cloud-sync detected
  - R4-M10:  generic UI message — no paths/PII in user-facing message

Design:
  - SRP: this module ONLY builds the zip; policy lives in DiagnosticsExcludePolicy.
  - OCP: CloudSyncDetector is an injectable Protocol; default impl uses path heuristics.
  - DIP: depends on ConnectionProvider and Clock abstractions, not concrete impls.
  - Fail-closed: any schema drift stops zip creation before any file I/O.

See: docs/architecture/10-7-diagnostic-zip.md, docs/decisions/ADR-012-*.md
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from fis_monitor.domain.interfaces import Clock, ConnectionProvider
from fis_monitor.services.diagnostics.exclude_policy import DiagnosticsExcludePolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema snapshot (R3-M5 fail-closed)
# ---------------------------------------------------------------------------

# Canonical column sets per table.
# MUST be updated deliberately when adding columns — evaluate PII risk first.
# See: docs/architecture/10-7-diagnostic-zip.md §Schema-snapshot fail-closed
DIAGNOSTIC_SCHEMA_V1: dict[str, frozenset[str]] = {
    "lots": frozenset(
        {
            "id",
            "cadastral_no",
            "area_sqm",
            "region",
            "municipality",
            "land_category",
            "permitted_use",
            "ogv",
            "status",
            "date_create",
            "date_update",
            "lat",
            "lon",
            "has_boundaries",
            "parser_version",
            "first_seen",
            "last_seen",
            "detail_fetched_at",
            "enrichment_status",
            "enrichment_retries",
            "last_seen_at",
            "last_status",
            "last_status_at",
            "is_active",
            "inactive_reason",
            "inactive_since",
            "inactive_confirmed_at",
        }
    ),
    "cycles": frozenset(
        {
            "id",
            "region",
            "started_at",
            "finished_at",
            "status",
            "lots_fetched",
            "new_lots",
            "error",
            "id_schema_check",
        }
    ),
    "notifications": frozenset({"lot_id", "channel", "recipient", "sent_at"}),
    # ATTENTION: status/attempt_no/last_attempt_at — NOT in whitelist
    # (may contain PII via side-channels).
    # NOTE: recipient IS in the snapshot — it is part of the PK (lot_id, channel, recipient)
    # per ADR-019 and must be tracked for schema-drift detection. PII filtering at the
    # row level is handled by DiagnosticsExcludePolicy, NOT by the schema snapshot.
    # The snapshot is a structural guard against unreviewed schema drift, not a data whitelist.
    "state": frozenset({"key", "value", "updated_at"}),
    # state-key filtering is done by DiagnosticsExcludePolicy.filter_state_keys()
}

# Files in data_dir that are allowed in the zip (explicit allow-list, ADR-012).
_ALLOWED_FILE_NAMES: frozenset[str] = frozenset(
    {
        "state.db",
        "app.jsonl",
        "requests.jsonl",
        "schema-snapshot.txt",
    }
)

# Files excluded unconditionally (PII / crash dumps, ADR-017).
_EXCLUDE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r".*\.dmp$", re.IGNORECASE),
    re.compile(r"core\.\d+$"),
    re.compile(r"^audit\.jsonl$"),  # PII — only included when NOT cloud-synced
)

# Generic UI message (R4-M10) — never leaks paths, column names, or PII.
_GENERIC_UI_MESSAGE = "Diagnostics bundle unavailable. Please contact support."


# ---------------------------------------------------------------------------
# Exceptions (internal)
# ---------------------------------------------------------------------------


class SchemaDriftError(Exception):
    """Raised when the live DB schema diverges from DIAGNOSTIC_SCHEMA_V1.

    Details (table name, new column names) go to logger.error ONLY — never
    surfaced to the user UI (R4-M10).
    """


class DiagnosticUnavailable(Exception):
    """User-facing wrapper: generic message, no internal details."""


# ---------------------------------------------------------------------------
# CloudSyncDetector Protocol + default implementation
# ---------------------------------------------------------------------------


@runtime_checkable
class CloudSyncDetector(Protocol):
    """Detect whether *data_dir* lives inside a cloud-sync folder.

    Injected for testability — no mocking required; use a lambda/stub in tests.
    """

    def is_cloud_synced(self, data_dir: Path) -> bool:
        """Return True if *data_dir* is inside a known cloud-sync folder."""
        ...


class DefaultCloudSyncDetector:
    """Detect cloud-sync by path-segment heuristics.

    Covers the most common desktop cloud-sync clients on macOS/Windows/Linux.
    Intentionally simple (O(n) string scan) — this runs once per build_zip()
    call, not in a hot loop.
    """

    _MARKERS: tuple[str, ...] = (
        "Dropbox",
        "iCloud",
        "OneDrive",
        "Google Drive",
        "GoogleDrive",
    )

    def is_cloud_synced(self, data_dir: Path) -> bool:
        path_str = str(data_dir)
        return any(marker in path_str for marker in self._MARKERS)


# ---------------------------------------------------------------------------
# BuildZipResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildZipResult:
    """Outcome of DiagnosticsService.build_zip().

    Attributes:
        ok:               True if the zip was written successfully.
        output_path:      Destination path (always set; zip exists only when ok=True).
        files_included:   Relative paths inside the zip (empty when ok=False).
        schema_ok:        False if schema drift was detected (fail-closed activated).
        audit_included:   False when cloud-sync was detected (R4-M7).
        ui_message:       User-facing message. Generic — never contains paths/PII.
    """

    ok: bool
    output_path: Path
    files_included: tuple[str, ...]
    schema_ok: bool
    audit_included: bool
    ui_message: str = field(default="")


# ---------------------------------------------------------------------------
# DiagnosticsService
# ---------------------------------------------------------------------------


class DiagnosticsService:
    """Diagnostic bundle generator with explicit allow-list (ADR-012).

    Build sequence:
      1. Validate schema via ConnectionProvider; compare live columns against
         DIAGNOSTIC_SCHEMA_V1. On drift → fail-closed: return ok=False with
         generic UI message (R3-M5, R4-M10).
      2. Detect cloud-sync directory (DefaultCloudSyncDetector).  If detected
         → audit.jsonl NOT included (R4-M7), logger.warning emitted.
      3. Walk data_dir; include only allow-listed files.  Exclude *.dmp /
         core.* unconditionally; exclude audit.jsonl when cloud-sync detected.
      4. Write zip to output_path.

    Args:
        data_dir:            Directory that contains state.db, app.jsonl, etc.
        conn_provider:       Per-thread SQLite connection factory.
        clock:               Injected time source (unused in core logic; available
                             for future MANIFEST timestamp).
        exclude_policy:      PII field/key exclusion rules (reused from exclude_policy.py).
        cloud_sync_detector: Injectable detector; defaults to DefaultCloudSyncDetector.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        conn_provider: ConnectionProvider,
        clock: Clock,
        exclude_policy: DiagnosticsExcludePolicy,
        cloud_sync_detector: CloudSyncDetector | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._conn_provider = conn_provider
        self._clock = clock
        self._exclude_policy = exclude_policy
        self._cloud_sync_detector: CloudSyncDetector = (
            cloud_sync_detector
            if cloud_sync_detector is not None
            else DefaultCloudSyncDetector()
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_zip(self, output_path: Path) -> BuildZipResult:
        """Build a diagnostic bundle at *output_path*.

        Returns a :class:`BuildZipResult` describing the outcome.  Never
        raises — all errors are captured in the result (fail-closed for schema
        drift; warning for cloud-sync).

        Args:
            output_path: Path where the zip file should be written.

        Returns:
            BuildZipResult with ok=False and generic ui_message on schema drift.
        """
        # Step 1: schema validation (fail-closed, R3-M5)
        try:
            self._validate_schema()
        except SchemaDriftError as exc:
            logger.error("diagnostic.schema_drift details=%s", exc)
            return BuildZipResult(
                ok=False,
                output_path=output_path,
                files_included=(),
                schema_ok=False,
                audit_included=False,
                ui_message=_GENERIC_UI_MESSAGE,
            )

        # Step 2: cloud-sync detection (R4-M7)
        audit_included = True
        if self._cloud_sync_detector.is_cloud_synced(self._data_dir):
            logger.warning(
                "diagnostic.cloud_sync_detected data_dir=%s — audit.jsonl excluded",
                self._data_dir,
            )
            audit_included = False

        # Step 3 + 4: collect files and write zip
        files_included = self._write_zip(output_path, audit_included=audit_included)

        return BuildZipResult(
            ok=True,
            output_path=output_path,
            files_included=tuple(files_included),
            schema_ok=True,
            audit_included=audit_included,
            ui_message="",
        )

    # ------------------------------------------------------------------
    # Schema validation (R3-M5 fail-closed)
    # ------------------------------------------------------------------

    def _validate_schema(self) -> None:
        """Compare live DB columns against DIAGNOSTIC_SCHEMA_V1.

        Uses PRAGMA table_info(<table>) for each table in the snapshot.
        Raises SchemaDriftError if any table has columns beyond the whitelist.

        Only tables in DIAGNOSTIC_SCHEMA_V1 are inspected; tables like
        smtp_credentials are never touched (ADR-012 / ADR-017).
        """
        conn: sqlite3.Connection = self._conn_provider.get()
        for table, allowed_cols in DIAGNOSTIC_SCHEMA_V1.items():
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                # Table absent from DB — this is unexpected schema drift (fail-closed).
                # A missing expected table may indicate mid-migration corruption or
                # a schema change that bypassed the snapshot update process.
                logger.error(
                    "diagnostic.schema_drift table=%r not found in DB; "
                    "expected per DIAGNOSTIC_SCHEMA_V1",
                    table,
                )
                raise SchemaDriftError(
                    f"table={table!r} missing from DB; "
                    "update DIAGNOSTIC_SCHEMA_V1 after schema review"
                )
            live_cols: frozenset[str] = frozenset(row[1] for row in rows)
            extra = live_cols - allowed_cols
            if extra:
                raise SchemaDriftError(
                    f"table={table!r} new_columns={sorted(extra)!r}; "
                    "update DIAGNOSTIC_SCHEMA_V1 after PII review"
                )

    # ------------------------------------------------------------------
    # File collection + zip writing
    # ------------------------------------------------------------------

    def _write_zip(self, output_path: Path, *, audit_included: bool) -> list[str]:
        """Walk data_dir, apply allow-list + exclude rules, write zip atomically.

        Uses a temp file in the same directory as output_path, then renames it
        into place.  This prevents a partial zip from being visible if the process
        is interrupted mid-write.

        Returns the list of relative paths included in the zip.
        """
        included: list[str] = []

        fd, tmp_name = tempfile.mkstemp(
            dir=output_path.parent, prefix=".diag_tmp_", suffix=".zip"
        )
        try:
            with (
                os.fdopen(fd, "wb") as raw,
                zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as zf,
            ):
                for candidate in sorted(self._data_dir.iterdir()):
                    if not candidate.is_file():
                        continue

                    rel = candidate.name

                    # Unconditional excludes: *.dmp, core.*
                    if self._is_excluded(rel):
                        continue

                    # audit.jsonl: excluded when cloud-sync detected (R4-M7)
                    if rel == "audit.jsonl" and not audit_included:
                        continue

                    # Only allow-listed file names pass through
                    if rel not in _ALLOWED_FILE_NAMES and rel != "audit.jsonl":
                        continue

                    zf.write(candidate, arcname=rel)
                    included.append(rel)

            os.replace(tmp_name, output_path)
        except Exception:
            # Clean up temp file on any failure; propagate the exception.
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

        return included

    @staticmethod
    def _is_excluded(name: str) -> bool:
        """Return True if *name* matches any unconditional exclude pattern."""
        return any(pattern.match(name) for pattern in _EXCLUDE_PATTERNS[:2])
        # Note: _EXCLUDE_PATTERNS[2] is audit.jsonl — handled separately above
