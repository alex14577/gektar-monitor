"""Diagnostics sub-package.

Exposes:
  - DiagnosticsExcludePolicy: PII exclude / redaction rules for diagnostic.zip.
  - DiagnosticsService:       Diagnostic bundle generator (ADR-012, R3-M5, R4-M7, R4-M10).
  - BuildZipResult:           Return type of DiagnosticsService.build_zip().
  - CloudSyncDetector:        Protocol for cloud-sync detection (injectable).
  - DefaultCloudSyncDetector: Default path-heuristic implementation.
  - DIAGNOSTIC_SCHEMA_V1:     Schema snapshot constant (fail-closed guard).
"""

from fis_monitor.services.diagnostics.service import (
    DIAGNOSTIC_SCHEMA_V1,
    BuildZipResult,
    CloudSyncDetector,
    DefaultCloudSyncDetector,
    DiagnosticsService,
)

__all__ = [
    "DIAGNOSTIC_SCHEMA_V1",
    "BuildZipResult",
    "CloudSyncDetector",
    "DefaultCloudSyncDetector",
    "DiagnosticsService",
]
