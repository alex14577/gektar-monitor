"""Unit tests for ShutdownRequesterCell — fail-closed late-binding cell.

Covers the security-critical invariants of ShutdownRequesterCell:
- bind/delegate contract
- latch semantics (double-bind raises RuntimeError and preserves first binding)
- unbound fail-closed path (os._exit(1))
- concurrent bind race: exactly one winner
"""
from __future__ import annotations

import threading
from typing import Any


class _FakeRequester:
    """Minimal fake ShutdownRequester for testing."""

    def __init__(self) -> None:
        self.calls: int = 0

    def request_shutdown(self) -> None:
        self.calls += 1


# ---------------------------------------------------------------------------
# Test 1 — bind stores the real requester and delegates request_shutdown
# ---------------------------------------------------------------------------


def test_bind_stores_real_and_delegates() -> None:
    """bind(fake) then request_shutdown() must delegate to fake exactly once."""
    from fis_monitor.infra.shutdown_cell import ShutdownRequesterCell

    cell = ShutdownRequesterCell()
    fake = _FakeRequester()
    cell.bind(fake)
    cell.request_shutdown()

    assert fake.calls == 1, "request_shutdown must delegate to bound requester"


# ---------------------------------------------------------------------------
# Test 2 — double bind raises RuntimeError; first binding is preserved
# ---------------------------------------------------------------------------


def test_double_bind_raises_runtime_error() -> None:
    """bind() called twice must raise RuntimeError on the second call.

    After the error the cell must still delegate to the first binding, not the
    second one (latch semantics — prevents silent rebinding).
    """
    from fis_monitor.infra.shutdown_cell import ShutdownRequesterCell

    cell = ShutdownRequesterCell()
    first = _FakeRequester()
    second = _FakeRequester()

    cell.bind(first)
    import pytest

    with pytest.raises(RuntimeError):
        cell.bind(second)

    # After the failed second bind, delegation must still go to the first.
    cell.request_shutdown()
    assert first.calls == 1, "Delegation must remain on the first binding after a failed re-bind"
    assert second.calls == 0, "Second requester must never be called"


# ---------------------------------------------------------------------------
# Test 3 — unbound request_shutdown calls os._exit(1) and writes to stderr
# ---------------------------------------------------------------------------


def test_unbound_request_shutdown_calls_os_exit(monkeypatch: Any, capsys: Any) -> None:
    """request_shutdown() before bind() must call os._exit(1) (fail-closed).

    We monkeypatch fis_monitor.infra.shutdown_cell.os._exit so pytest is not
    terminated.  The patch raises a sentinel exception that the test catches.
    """
    from fis_monitor.infra import shutdown_cell

    class _HardExit(Exception):
        def __init__(self, code: int) -> None:
            self.code = code

    def _fake_exit(code: int) -> None:
        raise _HardExit(code)

    monkeypatch.setattr(shutdown_cell.os, "_exit", _fake_exit)

    cell = shutdown_cell.ShutdownRequesterCell()

    import pytest

    with pytest.raises(_HardExit) as exc_info:
        cell.request_shutdown()

    assert exc_info.value.code == 1, "os._exit must be called with code 1"

    captured = capsys.readouterr()
    assert "CRITICAL" in captured.err or "emergency" in captured.err or "bind()" in captured.err, (
        "Forensic banner must be written to stderr before os._exit"
    )


# ---------------------------------------------------------------------------
# Test 4 — concurrent bind: exactly one winner, exactly one RuntimeError
# ---------------------------------------------------------------------------


def test_concurrent_bind_only_one_wins() -> None:
    """Two threads calling bind() simultaneously — exactly one succeeds, one raises."""
    from fis_monitor.infra.shutdown_cell import ShutdownRequesterCell

    cell = ShutdownRequesterCell()
    first = _FakeRequester()
    second = _FakeRequester()
    requesters = [first, second]

    errors: list[Exception] = []
    success_count = [0]
    barrier = threading.Barrier(2)

    def _try_bind(r: _FakeRequester) -> None:
        barrier.wait(timeout=2.0)
        try:
            cell.bind(r)
            success_count[0] += 1
        except RuntimeError as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_try_bind, args=(requesters[0],), daemon=True)
    t2 = threading.Thread(target=_try_bind, args=(requesters[1],), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert success_count[0] == 1, "Exactly one bind must succeed"
    assert len(errors) == 1, "Exactly one RuntimeError must be raised"
    assert isinstance(errors[0], RuntimeError)

    # The winning binding must be callable.
    cell.request_shutdown()
    total_calls = first.calls + second.calls
    assert total_calls == 1, "request_shutdown must delegate to exactly one requester"
