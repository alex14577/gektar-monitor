"""Unit tests for Settings domain model (Layer 1 — Pydantic validation + migration).

Layer 1 doctrine (docs/architecture/09-test-strategy.md):
  - Unit: Pydantic validation — boundaries, defaults, frozen=True.
  - No network / DB.

Coverage: ADR-035 migration shim (_migrate_subject_site_ids).
  Invariants tested:
  I1. Legacy key absent → model unchanged.
  I2. Legacy key present, rf_subjects empty → values copied to rf_subjects.
  I3. Legacy key present, rf_subjects non-empty → existing rf_subjects wins.
  I4. Legacy key is stripped from the validated model in all cases.
"""

from __future__ import annotations

from fis_monitor.domain.models import Settings


def test_settings_migrates_subject_site_ids_to_filters_rf_subjects() -> None:
    """Raw dict with subject_site_ids and no filters → rf_subjects receives the values."""
    s = Settings.model_validate({"subject_site_ids": [77, 16]})
    assert s.filters.rf_subjects == [77, 16]


def test_settings_migrates_subject_site_ids_when_filters_rf_subjects_empty() -> None:
    """Raw dict with subject_site_ids and explicit empty rf_subjects → migration copies."""
    s = Settings.model_validate({"subject_site_ids": [77], "filters": {"rf_subjects": []}})
    assert s.filters.rf_subjects == [77]


def test_settings_does_not_overwrite_existing_filters_rf_subjects() -> None:
    """User's explicit rf_subjects must win over the legacy subject_site_ids value."""
    s = Settings.model_validate({"subject_site_ids": [77], "filters": {"rf_subjects": [16]}})
    assert s.filters.rf_subjects == [16]


def test_settings_strips_subject_site_ids_key_after_migration() -> None:
    """subject_site_ids must not leak as an attribute after validation."""
    s = Settings.model_validate({"subject_site_ids": [77, 16]})
    assert not hasattr(s, "subject_site_ids")


def test_settings_no_subject_site_ids_key_no_change() -> None:
    """Fresh config without legacy key passes through cleanly with default rf_subjects."""
    s = Settings()
    assert s.filters.rf_subjects == []
    assert not hasattr(s, "subject_site_ids")


def test_settings_migrates_subject_site_ids_explicit_empty() -> None:
    """Explicit empty legacy list → rf_subjects is [] and key is stripped."""
    s = Settings.model_validate({"subject_site_ids": []})
    assert s.filters.rf_subjects == []
    assert not hasattr(s, "subject_site_ids")
