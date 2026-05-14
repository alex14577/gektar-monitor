"""Tests for create_app() wiring: routes, middleware, static, main().

Verifies that the assembly in create_app() is correct without starting a
real server or loading the database.  All Container/Locker dependencies are
faked via DI seams.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from fis_monitor.app import (
    _LazyOnboardingProxy,
    create_app,
)

# ---------------------------------------------------------------------------
# Minimal fakes — just enough to satisfy lifespan's attribute accesses
# ---------------------------------------------------------------------------


class _FakeStopEvent:
    def set(self) -> None: ...
    def wait(self) -> None: ...


class _FakeDispatcher:
    stop_event = _FakeStopEvent()

    def consumer_loop(self) -> None:
        pass


class _FakeFullScan:
    def run_forever(self, stop_event: Any) -> None:
        pass


class _FakeLogin:
    def bind_executor(self, executor: Any) -> None: ...
    def cancel_active_job(self) -> None: ...


class _FakeConnProvider:
    def close_all(self) -> None: ...


@dataclass
class _FakeInfra:
    conn_provider: _FakeConnProvider = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.conn_provider is None:
            self.conn_provider = _FakeConnProvider()


@dataclass
class _FakeServices:
    notifier_dispatcher: _FakeDispatcher = None  # type: ignore[assignment]
    full_scan: _FakeFullScan = None  # type: ignore[assignment]
    login: _FakeLogin = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notifier_dispatcher is None:
            self.notifier_dispatcher = _FakeDispatcher()
        if self.full_scan is None:
            self.full_scan = _FakeFullScan()
        if self.login is None:
            self.login = _FakeLogin()


@dataclass
class _FakeContainer:
    infra: _FakeInfra = None  # type: ignore[assignment]
    services: _FakeServices = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.infra is None:
            self.infra = _FakeInfra()
        if self.services is None:
            self.services = _FakeServices()


class _FakeLockHandle:
    pass


class _FakeLocker:
    def acquire(self) -> _FakeLockHandle:
        return _FakeLockHandle()

    def release(self, handle: Any) -> None:
        pass


def _fake_container_factory(settings: Any, data_dir: Path) -> _FakeContainer:
    return _FakeContainer()


def _fake_locker_factory(data_dir: Path) -> _FakeLocker:
    return _FakeLocker()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_app_mounts_routers(tmp_path: Path) -> None:
    """All seven routers and /static are mounted after create_app()."""
    app = create_app(
        tmp_path,
        container_factory=_fake_container_factory,
        locker_factory=_fake_locker_factory,
    )

    paths = [r.path for r in app.routes if hasattr(r, "path")]

    # Routers with explicit prefixes
    assert any(p.startswith("/lots") for p in paths), f"/lots not found in {paths}"
    assert any(p.startswith("/notifications") for p in paths), (
        f"/notifications not found in {paths}"
    )
    assert any(p.startswith("/settings") for p in paths), f"/settings not found in {paths}"
    assert any(p.startswith("/diagnostics") for p in paths), (
        f"/diagnostics not found in {paths}"
    )
    assert any(p.startswith("/onboarding") for p in paths), f"/onboarding not found in {paths}"
    assert any(p.startswith("/auth") for p in paths), f"/auth not found in {paths}"

    # Events router has no prefix — route is /events
    assert any(p.startswith("/events") for p in paths), f"/events not found in {paths}"

    # StaticFiles mount
    assert "/static" in paths, f"/static mount not found in {paths}"


def test_lazy_onboarding_proxy_resolves_via_app_state() -> None:
    """_LazyOnboardingProxy delegates to app.state.container.services.onboarding."""

    class FakeOnboarding:
        def current(self) -> object:
            return "completed"

        def url_for_current_step(self) -> str:
            return "/"

    app = FastAPI()
    app.state.container = SimpleNamespace(
        services=SimpleNamespace(onboarding=FakeOnboarding())
    )

    proxy = _LazyOnboardingProxy(app)

    assert proxy.current() == "completed"
    assert proxy.url_for_current_step() == "/"


def test_main_argparse_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main() passes host/port defaults to uvicorn.run when called with no args."""
    monkeypatch.setattr(sys, "argv", ["fis-monitor"])
    # Redirect data_dir so mkdir doesn't pollute cwd
    monkeypatch.chdir(tmp_path)

    fake_app = MagicMock()
    fake_run = MagicMock()

    # Patch create_app to return a fake app so lifespan never runs.
    with (
        patch("fis_monitor.app.create_app", return_value=fake_app),
        patch("uvicorn.run", fake_run),
        # Provide a stub build_container so the late import inside main() resolves.
        patch.dict(
            "sys.modules",
            {"fis_monitor.composition": MagicMock(build_container=MagicMock())},
        ),
    ):
        from fis_monitor.app import main

        main()

    fake_run.assert_called_once()
    _, kwargs = fake_run.call_args
    assert kwargs.get("host") == "127.0.0.1" or fake_run.call_args[0][1] == "127.0.0.1"
    # port may be positional or keyword
    call_args = fake_run.call_args
    port_value = call_args.kwargs.get("port") or (
        call_args.args[2] if len(call_args.args) > 2 else None
    )
    assert port_value == 8000
