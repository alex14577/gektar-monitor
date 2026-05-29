"""Unit tests for LicenseExpirySupervisor.

Layer 2 (application services) — pure fakes, no real I/O, no real time.sleep.

Invariants verified (spec §Тесты, инварианты 1-7):
  1. next_check_at cadence — boundary cases.
  2. VALID → EXPIRED → shutdown exactly once.
  3. stop_event mid-wait → clean exit without shutdown.
  4. verify raises RuntimeError → supervisor.crash + shutdown (fail-closed).
  6. FileNotFoundError on load_key → check.error + shutdown_requested once.
  7. Watchdog Timer.start called; _handle_expiry idempotent (no duplicate fire).

Note: v1 keys and perpetual (no-exp) keys removed in v2 migration (ADR-058).
"""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fis_monitor.domain.models import SseLicenseExpired
from fis_monitor.licensing._codec import _canonical_bytes, encode_payload
from fis_monitor.licensing._hmac import sign
from fis_monitor.licensing._verify import LicenseResult, LicenseStatus
from fis_monitor.services.license_expiry import LicenseExpirySupervisor, next_check_at

# ---------------------------------------------------------------------------
# Helpers — canonical key builder (mirrors tests/licensing/conftest.py)
# ---------------------------------------------------------------------------

_TEST_SECRET: bytes = (
    b"\x01\x02\x03\x04\x05\x06\x07\x08"
    b"\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10"
    b"\x11\x12\x13\x14\x15\x16\x17\x18"
    b"\x19\x1a\x1b\x1c\x1d\x1e\x1f\x20"
)


def _make_v2_key(
    nbf: date,
    exp: date,
    secret: bytes = _TEST_SECRET,
) -> str:
    """Build a v2 license key for tests. exp is always required in v2."""
    payload: dict[str, object] = {
        "v": 2,
        "nbf": nbf.isoformat(),
        "exp": exp.isoformat(),
        "lic": "interactive",
    }
    encoded = encode_payload(payload)
    sig = sign(_canonical_bytes(payload), secret)
    encoded_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"v2.{encoded}.{encoded_sig}"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    """Injected clock — call ``advance(delta)`` to move time forward."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


class FakeEventBus:
    """Records published events."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def publish(self, event: object) -> None:
        self.events.append(event)

    def subscribe(self) -> object:
        raise NotImplementedError("FakeEventBus.subscribe not needed in unit tests")


class FakeSecretProvider:
    def __init__(self, secret: bytes = _TEST_SECRET) -> None:
        self._secret = secret

    def get_secret(self) -> bytes:
        return self._secret


class FakeKeyProvider:
    """Returns a fixed key string or raises a configured exception."""

    def __init__(self, key_str: str = "") -> None:
        self._key_str = key_str
        self._raises: type[Exception] | None = None

    def set_raises(self, exc_type: type[Exception]) -> None:
        self._raises = exc_type

    def load_key(self) -> str:
        if self._raises is not None:
            raise self._raises("FakeKeyProvider: configured to raise")
        return self._key_str


class FakeShutdownRequester:
    """Records request_shutdown calls."""

    def __init__(self) -> None:
        self.calls: int = 0

    def request_shutdown(self) -> None:
        self.calls += 1


class FakeTimer:
    """Records start/cancel; does NOT fire automatically."""

    def __init__(self, interval: float, func: Callable[[], None]) -> None:
        self.interval = interval
        self.func = func
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class FakeTimerFactory:
    """Returns FakeTimer instances; keeps the last one for inspection."""

    def __init__(self) -> None:
        self.last: FakeTimer | None = None
        self.all: list[FakeTimer] = []

    def __call__(self, interval: float, func: Callable[[], None]) -> FakeTimer:
        t = FakeTimer(interval, func)
        self.last = t
        self.all.append(t)
        return t


class FakeVerifier:
    """Returns a fixed LicenseResult or raises a configured exception."""

    def __init__(self, result: LicenseResult | None = None) -> None:
        self._result = result or LicenseResult(
            status=LicenseStatus.VALID, expires_at=None, licensee="test"
        )
        self._raises: type[Exception] | None = None

    def set_result(self, result: LicenseResult) -> None:
        self._result = result

    def set_raises(self, exc_type: type[Exception]) -> None:
        self._raises = exc_type

    def verify(self, key_str: str, secret: bytes, now: datetime) -> LicenseResult:
        if self._raises is not None:
            raise self._raises("FakeVerifier: configured to raise")
        return self._result


# ---------------------------------------------------------------------------
# Helper: build supervisor with all fakes
# ---------------------------------------------------------------------------

_BASE_NOW = datetime(2026, 5, 28, 22, 0, 0, tzinfo=UTC)


def _build_supervisor(
    *,
    clock: FakeClock | None = None,
    key_provider: FakeKeyProvider | None = None,
    verifier: FakeVerifier | None = None,
    timer_factory: FakeTimerFactory | None = None,
    shutdown_requester: FakeShutdownRequester | None = None,
    event_bus: FakeEventBus | None = None,
    watchdog_grace: float = 45.0,
) -> tuple[
    LicenseExpirySupervisor,
    FakeClock,
    FakeKeyProvider,
    FakeVerifier,
    FakeTimerFactory,
    FakeShutdownRequester,
    FakeEventBus,
]:
    clk = clock or FakeClock(_BASE_NOW)
    kp = key_provider or FakeKeyProvider("dummy")
    ver = verifier or FakeVerifier()
    tf = timer_factory or FakeTimerFactory()
    sr = shutdown_requester or FakeShutdownRequester()
    eb = event_bus or FakeEventBus()

    svc = LicenseExpirySupervisor(
        secret_provider=FakeSecretProvider(),
        key_provider=kp,
        clock=clk,
        event_bus=eb,
        shutdown_requester=sr,
        watchdog_grace_seconds=watchdog_grace,
        watchdog_factory=tf,
        verifier=ver,
    )
    return svc, clk, kp, ver, tf, sr, eb


# ===========================================================================
# Invariant 1 — next_check_at cadence
# ===========================================================================


@pytest.mark.parametrize(
    "now_str, expected_str",
    [
        # 23:00 → tomorrow 00:01
        ("2026-05-28T23:00:00+00:00", "2026-05-29T00:01:00+00:00"),
        # 00:00:30 → same day 00:01:00
        ("2026-05-28T00:00:30+00:00", "2026-05-28T00:01:00+00:00"),
        # exactly 00:01:00 → tomorrow 00:01:00 (boundary: candidate <= now)
        ("2026-05-28T00:01:00+00:00", "2026-05-29T00:01:00+00:00"),
        # 12:00:00 → tomorrow 00:01:00
        ("2026-05-28T12:00:00+00:00", "2026-05-29T00:01:00+00:00"),
    ],
)
def test_next_check_at_cadence(now_str: str, expected_str: str) -> None:
    now = datetime.fromisoformat(now_str)
    expected = datetime.fromisoformat(expected_str)
    result = next_check_at(now, ZoneInfo("UTC"))
    assert result == expected, f"now={now_str} → got {result}, expected {expected}"


# ===========================================================================
# Invariant 2 — VALID → EXPIRED → shutdown exactly once
# ===========================================================================


def test_valid_then_expired_shutdown_once() -> None:
    """VALID on first check; EXPIRED on second check → shutdown called exactly once."""
    nbf = date(2026, 1, 1)
    exp = date(2026, 5, 28)  # expired by base_now (2026-05-28T22:00)
    key = _make_v2_key(nbf, exp)

    # Clock starts before expiry on first check (T1 = 2026-05-27), expired on second (T1 = base_now)
    t1 = datetime(2026, 5, 27, 22, 0, 0, tzinfo=UTC)  # VALID: today=2026-05-27 <= exp=2026-05-28
    t2 = datetime(2026, 5, 29, 0, 1, 0, tzinfo=UTC)   # EXPIRED: today=2026-05-29 > exp=2026-05-28

    calls_seq: list[datetime] = [t1, t2]
    call_idx = [0]

    class _SequencedClock:
        def now(self) -> datetime:
            idx = min(call_idx[0], len(calls_seq) - 1)
            return calls_seq[idx]

        def monotonic(self) -> float:
            return 0.0

    kp = FakeKeyProvider(key)
    sr = FakeShutdownRequester()
    eb = FakeEventBus()
    tf = FakeTimerFactory()

    # Real verifier (integration-style: use actual verify_license)
    from fis_monitor.licensing import verify_license

    class _RealVerifierWithClock:
        def verify(self, key_str: str, secret: bytes, now: datetime) -> LicenseResult:
            return verify_license(key_str, secret, now)

    clk = _SequencedClock()
    svc = LicenseExpirySupervisor(
        secret_provider=FakeSecretProvider(_TEST_SECRET),
        key_provider=kp,
        clock=clk,
        event_bus=eb,
        shutdown_requester=sr,
        watchdog_grace_seconds=45.0,
        watchdog_factory=tf,
        verifier=_RealVerifierWithClock(),
    )

    stop_event = threading.Event()

    def _run() -> None:
        # First check (VALID) — advance clock to trigger second check
        # Override: we'll call _check_once manually to avoid timing complexity
        svc._check_once()
        # Advance to T2
        call_idx[0] = 1
        svc._check_once()
        stop_event.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), "test thread did not finish in time"

    assert sr.calls == 1, f"Expected 1 shutdown call, got {sr.calls}"
    license_expired_events = [e for e in eb.events if isinstance(e, SseLicenseExpired)]
    assert len(license_expired_events) == 1, "Expected exactly 1 SseLicenseExpired event"
    assert tf.last is not None and tf.last.started, "Watchdog timer must have been started"


# ===========================================================================
# Invariant 3 — stop_event mid-wait → clean exit, no shutdown
# ===========================================================================


def test_stop_event_exits_without_shutdown() -> None:
    """If stop_event is set immediately and license is VALID, no shutdown is triggered."""
    svc, _clk, _kp, ver, _tf, sr, _eb = _build_supervisor()
    # Verifier returns VALID
    ver.set_result(LicenseResult(status=LicenseStatus.VALID, expires_at=None, licensee="t"))

    stop_event = threading.Event()
    stop_event.set()  # signal before supervisor even starts its loop

    t = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True)
    t.start()
    t.join(timeout=2.0)

    assert not t.is_alive(), "run_forever did not exit promptly after stop_event"
    assert sr.calls == 0, "No shutdown should be requested when license is VALID"


# ===========================================================================
# Invariant 4 — verifier raises → supervisor.crash + shutdown (fail-closed)
# ===========================================================================


def test_verifier_raises_triggers_shutdown() -> None:
    """If LicenseVerifier.verify raises, supervisor fail-closes: calls shutdown."""
    svc, _clk, _kp, ver, _tf, sr, _eb = _build_supervisor()
    ver.set_raises(RuntimeError)

    stop_event = threading.Event()

    t = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True)
    t.start()
    t.join(timeout=2.0)

    assert not t.is_alive(), "run_forever did not exit after crash"
    assert sr.calls == 1, "Shutdown must be called on verifier crash (fail-closed)"


# ===========================================================================
# Invariant 5 — VALID key → supervisor loops without shutdown
# ===========================================================================


def test_valid_license_loops_without_shutdown() -> None:
    """VALID license (v2, with exp in future) never triggers shutdown over multiple checks."""
    svc, clk, _kp, ver, _tf, sr, _eb = _build_supervisor()
    # v2 VALID keys always have an exp date
    future_exp = date(2027, 12, 31)
    ver.set_result(
        LicenseResult(status=LicenseStatus.VALID, expires_at=future_exp, licensee="interactive")
    )

    # Do several _check_once calls manually to simulate N days passing
    for _ in range(5):
        result = svc._check_once()
        assert result is False, "VALID key must return False (no shutdown requested)"
        clk.advance(timedelta(days=1))

    assert sr.calls == 0, "VALID license must never request shutdown"


# ===========================================================================
# Invariant 6 — FileNotFoundError on load_key → check.error + shutdown once
# ===========================================================================


def test_file_not_found_triggers_shutdown() -> None:
    """FileNotFoundError from load_key triggers shutdown exactly once."""
    kp = FakeKeyProvider()
    kp.set_raises(FileNotFoundError)
    svc, _clk, _, _ver, _tf, sr, _eb = _build_supervisor(key_provider=kp)

    stop_event = threading.Event()

    t = threading.Thread(target=svc.run_forever, args=(stop_event,), daemon=True)
    t.start()
    t.join(timeout=2.0)

    assert not t.is_alive(), "run_forever did not exit after FileNotFoundError"
    assert sr.calls == 1, "Shutdown must be called exactly once on missing key file"


# ===========================================================================
# Invariant 7 — watchdog armed; _handle_expiry idempotent
# ===========================================================================


def test_watchdog_armed_and_handle_expiry_idempotent() -> None:
    """_handle_expiry arms watchdog; calling it twice does NOT duplicate timer/SSE/shutdown."""
    svc, _clk, _kp, _ver, tf, sr, eb = _build_supervisor()

    expired_result = LicenseResult(
        status=LicenseStatus.EXPIRED,
        expires_at=date(2026, 5, 1),
        licensee="t",
    )

    # First call — should arm watchdog, publish SSE, call shutdown
    svc._handle_expiry(expired_result, reason="expired")

    assert tf.last is not None, "Watchdog timer must be created on first _handle_expiry"
    assert tf.last.started, "Watchdog timer must be started"
    assert tf.last.interval == 45.0, "Watchdog grace_seconds must match constructor arg"
    assert sr.calls == 1, "shutdown_requester.request_shutdown must be called once"
    license_events = [e for e in eb.events if isinstance(e, SseLicenseExpired)]
    assert len(license_events) == 1, "Exactly one SseLicenseExpired must be published"

    # Second call — idempotent: no new timer, no new SSE, no new shutdown
    svc._handle_expiry(expired_result, reason="expired")

    assert len(tf.all) == 1, "Second _handle_expiry must NOT create a second timer"
    assert sr.calls == 1, "Second _handle_expiry must NOT call shutdown again"
    assert len(eb.events) == 1, "Second _handle_expiry must NOT publish a second SSE event"


# ===========================================================================
# Invariant 8 — _handle_expiry concurrency: two simultaneous callers → exactly one fire
# ===========================================================================


@pytest.mark.parametrize("iteration", range(50))
def test_handle_expiry_concurrent_calls_idempotent(iteration: int) -> None:
    """Two threads calling _handle_expiry simultaneously must produce exactly one
    shutdown call, one SSE event, and one watchdog timer start (MAJOR 3 / M3).

    Parametrized over 50 iterations to probabilistically expose races that would
    pass with a single run (e.g., a check-then-act bug using Event instead of Lock).
    """
    svc, _clk, _kp, _ver, tf, sr, eb = _build_supervisor()

    expired_result = LicenseResult(
        status=LicenseStatus.EXPIRED,
        expires_at=date(2026, 5, 1),
        licensee="t",
    )

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def _call() -> None:
        try:
            barrier.wait(timeout=2.0)  # synchronize both threads at entry
            svc._handle_expiry(expired_result, reason="expired")
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_call, daemon=True)
    t2 = threading.Thread(target=_call, daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert not errors, f"Thread errors: {errors}"
    assert sr.calls == 1, f"Expected exactly 1 shutdown call, got {sr.calls}"
    license_events = [e for e in eb.events if isinstance(e, SseLicenseExpired)]
    assert len(license_events) == 1, f"Expected 1 SseLicenseExpired, got {len(license_events)}"
    assert len(tf.all) == 1, f"Expected 1 watchdog timer, got {len(tf.all)}"
    assert tf.all[0].started, "Watchdog timer must have been started"


# ===========================================================================
# Anti-fake: all FakeTimer methods exercised
# ===========================================================================


def test_fake_timer_start_and_cancel() -> None:
    """Ensure FakeTimer satisfies the _TimerLike Protocol contract."""
    factory = FakeTimerFactory()
    timer = factory(10.0, lambda: None)
    assert not timer.started
    timer.start()
    assert timer.started
    assert not timer.cancelled
    timer.cancel()
    assert timer.cancelled


# ===========================================================================
# Anti-fake: all fake methods exercised
# ===========================================================================


def test_all_fakes_exercised() -> None:
    """Ensure every method on test fakes is called at least once."""
    clk = FakeClock(_BASE_NOW)
    assert clk.now() == _BASE_NOW
    assert clk.monotonic() == 0.0
    clk.advance(timedelta(hours=1))
    assert clk.now() == _BASE_NOW + timedelta(hours=1)

    eb = FakeEventBus()
    eb.publish(object())
    assert len(eb.events) == 1
    with pytest.raises(NotImplementedError):
        eb.subscribe()

    sp = FakeSecretProvider()
    assert len(sp.get_secret()) == 32

    kp = FakeKeyProvider("test-key")
    assert kp.load_key() == "test-key"
    kp.set_raises(FileNotFoundError)
    with pytest.raises(FileNotFoundError):
        kp.load_key()

    sr = FakeShutdownRequester()
    assert sr.calls == 0
    sr.request_shutdown()
    assert sr.calls == 1

    ver = FakeVerifier()
    r = ver.verify("k", b"\x00" * 32, _BASE_NOW)
    assert r.status == LicenseStatus.VALID
    ver.set_raises(RuntimeError)
    with pytest.raises(RuntimeError):
        ver.verify("k", b"\x00" * 32, _BASE_NOW)
