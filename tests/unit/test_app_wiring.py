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


class _FakeSseStreamer:
    """Minimal fake SseStreamer — absorbs bind_executor() call from lifespan."""

    def bind_executor(self, executor: Any) -> None: ...


@dataclass
class _FakeInfra:
    conn_provider: _FakeConnProvider = None  # type: ignore[assignment]
    sse_streamer: _FakeSseStreamer = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.conn_provider is None:
            self.conn_provider = _FakeConnProvider()
        if self.sse_streamer is None:
            self.sse_streamer = _FakeSseStreamer()


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
    """main() creates uvicorn.Server with correct host/port defaults."""
    monkeypatch.setattr(sys, "argv", ["fis-monitor"])
    # Redirect data_dir so mkdir doesn't pollute cwd
    monkeypatch.chdir(tmp_path)

    # Provide a valid license key via patching load_license_key so the startup
    # check passes without a real file on disk.
    import base64
    import datetime as _dt

    from fis_monitor.licensing._codec import _canonical_bytes, encode_payload
    from fis_monitor.licensing._hmac import sign
    from fis_monitor.licensing._secret import _assemble_secret

    _iat = _dt.date.today().isoformat()
    _payload: dict = {"v": 1, "iat": _iat, "lic": "test"}
    _encoded = encode_payload(_payload)
    _sig = sign(_canonical_bytes(_payload), _assemble_secret())
    _encoded_sig = base64.urlsafe_b64encode(_sig).rstrip(b"=").decode("ascii")
    _fake_key = f"v1.{_encoded}.{_encoded_sig}"

    # Fake uvicorn.Server: records Config and exposes should_exit.
    captured_config: list = []

    class FakeServer:
        def __init__(self, config: object) -> None:
            captured_config.append(config)
            self.should_exit = False

        def run(self) -> None:
            pass

    fake_app = MagicMock()
    fake_app.state = MagicMock()

    # Patch create_app to return a fake app so lifespan never runs.
    with (
        patch("fis_monitor.app.create_app", return_value=fake_app),
        patch("uvicorn.Server", FakeServer),
        patch("fis_monitor._license_loader.load_license_key", return_value=_fake_key),
        # Provide a stub build_container so the late import inside main() resolves.
        patch.dict(
            "sys.modules",
            {"fis_monitor.composition": MagicMock(build_container=MagicMock())},
        ),
    ):
        from fis_monitor.app import main

        main()

    assert len(captured_config) == 1, "uvicorn.Server must be constructed once"
    cfg = captured_config[0]
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
