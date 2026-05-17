"""Shared fixtures for tests/unit/infra/."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fis_monitor.domain.models import Settings
from fis_monitor.infra.config_source import WatchdogConfigSource


class FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return 0.0


class FakeRegionSubRepo:
    def get_subscribed_at(self, region_id: int) -> None:
        return None

    def set_if_absent(self, region_id: int, at: datetime) -> None:
        pass

    def delete(self, region_id: int) -> None:
        pass


def make_config_source(
    path: Path,
    *,
    clock: FakeClock | None = None,
    region_subs_repo: FakeRegionSubRepo | None = None,
) -> WatchdogConfigSource:
    if clock is None:
        clock = FakeClock()
    parser = lambda raw: Settings.model_validate(json.loads(raw))  # noqa: E731
    with patch("fis_monitor.infra.config_source.Observer") as MockObs:
        mock_obs = MagicMock()
        MockObs.return_value = mock_obs
        src = WatchdogConfigSource(
            path=path,
            clock=clock,
            parser=parser,
            region_subs_repo=region_subs_repo,
        )
        src._observer = mock_obs
    return src


def write_settings(path: Path) -> bytes:
    raw = json.dumps(Settings().model_dump(mode="json")).encode()
    path.write_bytes(raw)
    return raw


@pytest.fixture()
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def fake_region_sub_repo() -> FakeRegionSubRepo:
    return FakeRegionSubRepo()
