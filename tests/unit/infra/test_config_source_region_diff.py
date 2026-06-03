"""Unit tests for WatchdogConfigSource region-subscription diff logic (ADR-039).

Layer 2 — pure fakes, no DB, no real FS watcher.

Invariants tested:
1. Diff add: old=[1,2], new=[1,2,3] → set_if_absent(3, now), 1 and 2 untouched.
2. Diff remove: old=[1,2], new=[1] → delete(2).
3. Diff re-add: old=[1,2] → [1] → [1,2] — second save gets new subscribed_at.
4. Idempotency: repeated reload with same regions → set_if_absent not called.
5. Cold-start: empty old, new=[1,2] → set_if_absent for both.
6. Audit log INFO subscribed_at.migration_applied for each net-new; no log on skip.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from fis_monitor.domain.models import Settings
from fis_monitor.domain.regions import subjects_for_macros
from fis_monitor.infra.config_source import WatchdogConfigSource

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._dt = start or datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._dt

    def monotonic(self) -> float:
        return 0.0

    def tick(self, seconds: float = 1.0) -> None:
        self._dt = self._dt + timedelta(seconds=seconds)


class _FakeRegionSubRepo:
    """In-memory RegionSubscriptionRepository for testing."""

    def __init__(self) -> None:
        self.set_if_absent_calls: list[tuple[int, datetime]] = []
        self.delete_calls: list[int] = []
        self._store: dict[int, datetime] = {}

    def get_subscribed_at(self, region_id: int) -> datetime | None:
        return self._store.get(region_id)

    def set_if_absent(self, region_id: int, subscribed_at: datetime) -> bool:
        self.set_if_absent_calls.append((region_id, subscribed_at))
        if region_id in self._store:
            return False
        self._store[region_id] = subscribed_at
        return True

    def delete(self, region_id: int) -> None:
        self.delete_calls.append(region_id)
        self._store.pop(region_id, None)

    def list_subscribed_region_ids(self) -> frozenset[int]:
        return frozenset(self._store.keys())


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _make_source(
    path: Path,
    *,
    clock: _FakeClock | None = None,
    repo: _FakeRegionSubRepo | None = None,
    parser: Callable[[bytes], Settings] | None = None,
) -> WatchdogConfigSource:
    if clock is None:
        clock = _FakeClock()
    if repo is None:
        repo = _FakeRegionSubRepo()
    if parser is None:
        parser = lambda raw: Settings.model_validate(json.loads(raw))  # noqa: E731

    with patch("fis_monitor.infra.config_source.Observer") as MockObserver:
        from unittest.mock import MagicMock

        mock_obs = MagicMock()
        MockObserver.return_value = mock_obs
        src = WatchdogConfigSource(
            path=path,
            clock=clock,
            region_subs_repo=repo,
            parser=parser,
        )
        src._observer = mock_obs
    return src


def _write_regions(path: Path, regions: list[int]) -> None:
    settings = Settings(regions=regions)
    path.write_bytes(settings.model_dump_json().encode())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegionDiffOnReload:
    """_do_reload triggers set_if_absent / delete based on old↔new regions diff."""

    def test_diff_add_calls_set_if_absent_for_new_region(self, tmp_path: Path) -> None:
        """old=[1], new=[1,2] → set_if_absent for each subject of macro 2; macro 1 untouched."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1])

        src = _make_source(cfg, clock=clock, repo=repo)
        # First reload: establishes old=[1] baseline (cold-start, not tested here).
        src._do_reload()
        repo.set_if_absent_calls.clear()
        repo.delete_calls.clear()

        # Now write new=[1,2] and reload.
        clock.tick(1)
        _write_regions(cfg, [1, 2])
        with patch.object(src, "_last_content_hash", b""):
            src._last_content_hash = b""
        src._do_reload()

        expected_subjects = subjects_for_macros([2])
        assert {sid for sid, _ in repo.set_if_absent_calls} == set(expected_subjects)
        assert all(ts == clock.now() for _, ts in repo.set_if_absent_calls)
        assert repo.delete_calls == []

    def test_diff_remove_calls_delete_for_removed_region(self, tmp_path: Path) -> None:
        """old=[1,2], new=[1] → delete(2); no set_if_absent."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1, 2])

        src = _make_source(cfg, clock=clock, repo=repo)
        src._do_reload()
        repo.set_if_absent_calls.clear()
        repo.delete_calls.clear()

        _write_regions(cfg, [1])
        with src._lock:
            src._last_content_hash = b""
        src._do_reload()

        # When macro 2 is removed while macro 1 remains, subjects 87/96 (shared) are kept.
        expected_deleted = set(subjects_for_macros([2])) - set(subjects_for_macros([1]))
        assert set(repo.delete_calls) == expected_deleted
        assert repo.set_if_absent_calls == []

    def test_diff_re_add_after_remove_gets_new_timestamp(self, tmp_path: Path) -> None:
        """old=[1,2] → [1] → [1,2]: second set_if_absent for region 2 uses new time."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1, 2])

        src = _make_source(cfg, clock=clock, repo=repo)
        src._do_reload()  # cold-start: set_if_absent for subjects of macros 1 and 2

        # Remove region 2.
        clock.tick(10)
        _write_regions(cfg, [1])
        with src._lock:
            src._last_content_hash = b""
        src._do_reload()  # delete(2)

        # After removing macro 2, subjects unique to macro 2 are deleted (e.g. 27).
        assert 27 not in repo._store

        # Re-add region 2.
        clock.tick(10)
        new_time = clock.now()
        _write_regions(cfg, [1, 2])
        with src._lock:
            src._last_content_hash = b""
        src._do_reload()  # set_if_absent for each subject of macro 2

        # Subject 27 (unique to macro 2) gets new subscribed_at after re-add.
        assert repo._store[27] == new_time

    def test_idempotency_no_diff_no_calls(self, tmp_path: Path) -> None:
        """Repeated reload with same regions → set_if_absent not called after first load."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1, 2])

        src = _make_source(cfg, clock=clock, repo=repo)
        src._do_reload()  # cold-start
        count_after_first = len(repo.set_if_absent_calls)

        # Same content with different hash forced — but regions unchanged.
        settings = Settings(regions=[1, 2], interval_minutes=5)
        cfg.write_bytes(settings.model_dump_json().encode())
        with src._lock:
            src._last_content_hash = b""
        src._do_reload()

        assert len(repo.set_if_absent_calls) == count_after_first
        assert repo.delete_calls == []

    def test_cold_start_all_regions_get_set_if_absent(self, tmp_path: Path) -> None:
        """File at boot with [1]. Bootstrap seeds subjects of macro 1.
        File changes to [1,2] → _do_reload adds subjects of macro 2 only."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1])

        src = _make_source(cfg, clock=clock, repo=repo)
        assert src.current().regions == [1]
        # Bootstrap seeds all subjects of macro 1.
        assert set(subjects_for_macros([1])) <= repo._store.keys()
        bootstrap_calls = list(repo.set_if_absent_calls)

        # Config file changes to regions=[1,2].
        _write_regions(cfg, [1, 2])
        src._do_reload()

        reload_calls = repo.set_if_absent_calls[len(bootstrap_calls) :]
        reload_called_ids = {sid for sid, _ in reload_calls}
        # _do_reload diff: [1] → [1,2] — only subjects of macro 2 are net-new.
        assert reload_called_ids == set(subjects_for_macros([2]))
        all_called_ids = {sid for sid, _ in repo.set_if_absent_calls}
        assert all_called_ids == set(subjects_for_macros([1, 2]))


class TestRegionDiffOnSave:
    """save() also applies region diff (bypasses _do_reload via hash-dedup)."""

    def test_save_new_region_calls_set_if_absent(self, tmp_path: Path) -> None:
        """save(regions=[1,2]) when current=[1] → set_if_absent for each subject of macro 2."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1])

        src = _make_source(cfg, clock=clock, repo=repo)
        src._do_reload()  # load [1] as current
        repo.set_if_absent_calls.clear()
        repo.delete_calls.clear()

        clock.tick(1)
        new_settings = Settings(regions=[1, 2])
        src.save(new_settings)

        expected_subjects = subjects_for_macros([2])
        assert {sid for sid, _ in repo.set_if_absent_calls} == set(expected_subjects)
        assert all(ts == clock.now() for _, ts in repo.set_if_absent_calls)
        assert repo.delete_calls == []

    def test_save_remove_region_calls_delete(self, tmp_path: Path) -> None:
        """save(regions=[1]) when current=[1,2] → delete(2)."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1, 2])

        src = _make_source(cfg, clock=clock, repo=repo)
        src._do_reload()
        repo.set_if_absent_calls.clear()
        repo.delete_calls.clear()

        src.save(Settings(regions=[1]))

        expected_deleted = set(subjects_for_macros([2])) - set(subjects_for_macros([1]))
        assert set(repo.delete_calls) == expected_deleted
        assert repo.set_if_absent_calls == []


class TestAuditLog:
    """set_if_absent triggers INFO audit log; no log on skip (no diff)."""

    def test_audit_log_emitted_for_net_new_region(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Net-new region emits one INFO subscribed_at.migration_applied per subject written."""
        import logging

        from fis_monitor.domain.regions import subjects_for_macros

        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        # Start with region 2 only so that region 1 (11 subjects) becomes net-new.
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [2])
        src = _make_source(cfg, clock=clock, repo=repo)
        src._do_reload()
        # Clear bootstrap events emitted during __init__ (ADR-039 _bootstrap_subscriptions).
        caplog.clear()

        # Add region 1 to trigger a net-new diff with subjects.
        _write_regions(cfg, [1, 2])
        with caplog.at_level(logging.INFO, logger="fis_monitor.audit"):
            src._do_reload()

        audit_msgs = [r.message for r in caplog.records if "migration_applied" in r.message]
        expected = len(subjects_for_macros([1]))
        assert len(audit_msgs) == expected, (
            f"Expected {expected} audit events (one per subject of region 1),"
            f" got {len(audit_msgs)}: {audit_msgs}"
        )
        assert all("migration_applied" in m for m in audit_msgs)

    def test_no_audit_log_on_no_diff(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No diff → no audit log emitted."""
        import logging

        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1, 2])

        src = _make_source(cfg, clock=clock, repo=repo)
        src._do_reload()  # cold-start
        caplog.clear()

        # Same regions, different interval — triggers reload but no region diff.
        settings = Settings(regions=[1, 2], interval_minutes=5)
        cfg.write_bytes(settings.model_dump_json().encode())
        with src._lock:
            src._last_content_hash = b""

        with caplog.at_level(logging.INFO, logger="fis_monitor.audit"):
            src._do_reload()

        audit_msgs = [r.message for r in caplog.records if "migration_applied" in r.message]
        assert audit_msgs == []


class TestBootstrapSubscriptions:
    """_bootstrap_subscriptions called in __init__ seeds region_subscriptions (ADR-039)."""

    def test_fresh_start_seeds_all_startup_regions(self, tmp_path: Path) -> None:
        """Fresh start: empty region_subscriptions + config regions=[1,2] → both seeded."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1, 2])

        _make_source(cfg, clock=clock, repo=repo)

        expected_subjects = set(subjects_for_macros([1, 2]))
        assert set(repo._store.keys()) == expected_subjects
        assert all(ts == clock.now() for ts in repo._store.values())

    def test_restart_with_existing_records_is_noop(self, tmp_path: Path) -> None:
        """Restart: region_subscriptions already has 1 and 2 → no overwrite, no audit log."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        existing_time = datetime(2025, 1, 1, tzinfo=UTC)
        # Pre-populate all subjects for macros 1 and 2 as if already seeded.
        for subject_id in subjects_for_macros([1, 2]):
            repo._store[subject_id] = existing_time

        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1, 2])

        _make_source(cfg, clock=clock, repo=repo)

        assert repo.set_if_absent_calls == []
        for subject_id in subjects_for_macros([1, 2]):
            assert repo._store[subject_id] == existing_time

    def test_partial_state_seeds_missing_region_only(self, tmp_path: Path) -> None:
        """Partial state: region_subscriptions has 1 only, config=[1,2] → only 2 seeded."""
        clock = _FakeClock()
        repo = _FakeRegionSubRepo()
        existing_time = datetime(2025, 1, 1, tzinfo=UTC)
        # Pre-populate all subjects for macro 1 only.
        for subject_id in subjects_for_macros([1]):
            repo._store[subject_id] = existing_time

        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1, 2])

        _make_source(cfg, clock=clock, repo=repo)

        # Subjects of macro 1 are untouched.
        for subject_id in subjects_for_macros([1]):
            assert repo._store[subject_id] == existing_time
        # Subjects of macro 2 (not already in store) are newly seeded.
        macro2_only = set(subjects_for_macros([2])) - set(subjects_for_macros([1]))
        for subject_id in macro2_only:
            assert repo._store[subject_id] == clock.now()
        seeded_ids = {rid for rid, _ in repo.set_if_absent_calls}
        assert seeded_ids == macro2_only


class TestNullRepoIsNoOp:
    """Without region_subs_repo injected, _apply_region_diff is a no-op."""

    def test_no_repo_no_error(self, tmp_path: Path) -> None:
        """WatchdogConfigSource without region_subs_repo: reload works without error."""
        clock = _FakeClock()
        cfg = tmp_path / "config.json"
        _write_regions(cfg, [1, 2])

        with patch("fis_monitor.infra.config_source.Observer") as MockObserver:
            from unittest.mock import MagicMock

            mock_obs = MagicMock()
            MockObserver.return_value = mock_obs
            src = WatchdogConfigSource(
                path=cfg,
                clock=clock,
                # no region_subs_repo
            )
            src._observer = mock_obs

        _write_regions(cfg, [1, 2, 3])
        with src._lock:
            src._last_content_hash = b""
        src._do_reload()  # must not raise
        assert src.current().regions == [1, 2, 3]
