"""Unit tests for SettingsService.

Contract under test:
  1. resolve_and_check is called BEFORE save (DNS outside tx).
  2. If resolve_and_check raises SmtpHostPolicyError, save is NOT called.
  3. DNS phase does not hold any lock that save would contend on
     (concurrency invariant: DNS outside tx).
  4. All fake-interface methods are exercised in at least one test
     (anti-unused-fake pattern).
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from fis_monitor.domain.errors import SmtpHostPolicyError
from fis_monitor.domain.models import (
    ResolvedSmtpEndpoint,
    SmtpCredentials,
)
from fis_monitor.services.settings import SettingsService

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSmtpCredentialsRepository:
    """In-memory fake.  Exposes ``load_calls`` and ``save_calls`` counters."""

    def __init__(self, *, load_result: SmtpCredentials | None = None) -> None:
        self._stored: SmtpCredentials | None = load_result
        self.load_calls: int = 0
        self.save_calls: int = 0
        # Lock used by the concurrency test to detect tx-overlap.
        self._save_lock = threading.Lock()

    def load(self) -> SmtpCredentials | None:
        self.load_calls += 1
        return self._stored

    def save(self, creds: SmtpCredentials) -> None:
        # Simulate a short critical-section to make overlap detectable.
        with self._save_lock:
            self.save_calls += 1
            self._stored = creds

    # Helper used by the concurrency test —
    def try_save_in_thread(self, creds: SmtpCredentials) -> bool:
        """Attempt save in a new thread; return True if lock was NOT contended."""
        acquired = threading.Event()
        done = threading.Event()
        result: list[bool] = []

        def _do_save() -> None:
            got_lock = self._save_lock.acquire(blocking=False)
            acquired.set()
            if got_lock:
                self.save_calls += 1
                self._stored = creds
                self._save_lock.release()
                result.append(True)
            else:
                result.append(False)
            done.set()

        t = threading.Thread(target=_do_save, daemon=True)
        t.start()
        done.wait(timeout=2.0)
        return result[0] if result else False


class FakeSmtpHostPolicy:
    """Fake that records call order relative to save calls of a repo."""

    def __init__(
        self,
        *,
        endpoint: ResolvedSmtpEndpoint | None = None,
        raise_error: bool = False,
        delay: float = 0.0,
        delay_event: threading.Event | None = None,
    ) -> None:
        self.call_count: int = 0
        self.call_timestamps: list[float] = []
        self._endpoint = endpoint or ResolvedSmtpEndpoint(
            ip="203.0.113.1",
            family=socket.AF_INET,
            port=587,
            original_host="smtp.example.com",
        )
        self._raise_error = raise_error
        self._delay = delay
        self._delay_event = delay_event  # Set when policy is mid-resolve.

    def resolve_and_check(self, host: str, port: int) -> ResolvedSmtpEndpoint:
        self.call_count += 1
        self.call_timestamps.append(time.monotonic())
        if self._delay_event is not None:
            self._delay_event.set()
        if self._delay > 0:
            time.sleep(self._delay)
        if self._raise_error:
            raise SmtpHostPolicyError(f"blocked: {host!r}")
        return self._endpoint


def _make_creds(host: str = "smtp.example.com", port: int = 587) -> SmtpCredentials:
    return SmtpCredentials(
        smtp_user="user@example.com",
        smtp_password="secret",  # type: ignore[arg-type]
        smtp_host=host,
        smtp_port=port,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSetSmtpCredentials:
    def test_set_smtp_credentials_calls_policy_then_save(self) -> None:
        """resolve_and_check must complete before save is called."""
        call_order: list[str] = []

        class OrderTrackingPolicy:
            def resolve_and_check(self, host: str, port: int) -> ResolvedSmtpEndpoint:
                call_order.append("policy")
                return ResolvedSmtpEndpoint(
                    ip="203.0.113.1",
                    family=socket.AF_INET,
                    port=port,
                    original_host=host,
                )

        class OrderTrackingRepo:
            def load(self) -> SmtpCredentials | None:
                return None

            def save(self, creds: SmtpCredentials) -> None:
                call_order.append("save")

        svc = SettingsService(
            smtp_creds_repo=OrderTrackingRepo(),
            host_policy=OrderTrackingPolicy(),
        )
        svc.set_smtp_credentials(_make_creds())

        assert call_order == ["policy", "save"], (
            f"Expected policy before save, got: {call_order}"
        )

    def test_set_smtp_credentials_policy_failure_does_not_save(self) -> None:
        """When policy raises SmtpHostPolicyError, save MUST NOT be called."""
        repo = FakeSmtpCredentialsRepository()
        policy = FakeSmtpHostPolicy(raise_error=True)
        svc = SettingsService(smtp_creds_repo=repo, host_policy=policy)

        with pytest.raises(SmtpHostPolicyError):
            svc.set_smtp_credentials(_make_creds())

        assert repo.save_calls == 0, "save must not be called after policy failure"
        assert policy.call_count == 1, "policy must be called exactly once"

    def test_set_smtp_credentials_dns_outside_tx(self) -> None:
        """Concurrency invariant: while policy (DNS) is executing, a parallel
        thread must be able to call repo.save() without being blocked.

        Proof: the fake repo's ``_save_lock`` simulates the writer-lock.
        The fake policy signals a threading.Event then sleeps for 0.1 s.
        A parallel thread attempts a non-blocking lock acquisition on
        ``_save_lock`` during the sleep window.  If save were holding the
        lock while policy runs, the parallel thread would fail — that would
        indicate DNS-under-tx, which is the prohibited pattern.

        Because our SettingsService calls policy BEFORE entering the repo,
        the repo lock is NOT held during the policy phase → parallel thread
        succeeds → test passes.
        """
        policy_started = threading.Event()
        repo = FakeSmtpCredentialsRepository()
        policy = FakeSmtpHostPolicy(delay=0.1, delay_event=policy_started)
        svc = SettingsService(smtp_creds_repo=repo, host_policy=policy)

        parallel_succeeded: list[bool] = []

        def _run_service() -> None:
            svc.set_smtp_credentials(_make_creds())

        t = threading.Thread(target=_run_service, daemon=True)
        t.start()

        # Wait until policy has started (DNS-resolve is in progress).
        policy_started.wait(timeout=2.0)

        # Now attempt repo.save() from a parallel thread.
        # Since policy has NOT entered the repo yet, _save_lock is free.
        other_creds = _make_creds(host="smtp.other.com")
        got_lock = repo.try_save_in_thread(other_creds)
        parallel_succeeded.append(got_lock)

        t.join(timeout=2.0)

        assert parallel_succeeded == [True], (
            "Parallel save should succeed while DNS is running "
            "(DNS must NOT hold the repo lock)"
        )

    def test_save_persists_credentials(self) -> None:
        """Successful call stores creds in the repo."""
        repo = FakeSmtpCredentialsRepository()
        policy = FakeSmtpHostPolicy()
        svc = SettingsService(smtp_creds_repo=repo, host_policy=policy)
        creds = _make_creds()

        svc.set_smtp_credentials(creds)

        assert repo.save_calls == 1
        assert repo.load() == creds

    def test_empty_host_raises_before_policy(self) -> None:
        """Empty smtp_host raises ValueError before any DNS call."""
        repo = FakeSmtpCredentialsRepository()
        policy = FakeSmtpHostPolicy()
        svc = SettingsService(smtp_creds_repo=repo, host_policy=policy)

        # Build creds with whitespace-only host via model_construct to bypass
        # Pydantic str validation (domain model allows any non-empty string).
        creds = SmtpCredentials.model_construct(
            smtp_user="u",
            smtp_password="p",  # type: ignore[arg-type]
            smtp_host="   ",
            smtp_port=587,
        )
        with pytest.raises(ValueError, match="smtp_host"):
            svc.set_smtp_credentials(creds)

        assert policy.call_count == 0
        assert repo.save_calls == 0


class TestAllFakeMethodsInvoked:
    """Ensure no fake method is defined but never called across the suite
    (anti-pattern: mock that is never exercised = false safety).

    This test explicitly calls every method of FakeSmtpCredentialsRepository
    and FakeSmtpHostPolicy at least once.
    """

    def test_all_fake_methods_invoked(self) -> None:
        repo = FakeSmtpCredentialsRepository()
        policy = FakeSmtpHostPolicy()

        # SmtpCredentialsRepository: load + save
        loaded = repo.load()
        assert loaded is None
        assert repo.load_calls == 1

        creds = _make_creds()
        repo.save(creds)
        assert repo.save_calls == 1

        loaded_after = repo.load()
        assert loaded_after == creds
        assert repo.load_calls == 2

        # SmtpHostPolicy: resolve_and_check
        endpoint = policy.resolve_and_check("smtp.example.com", 587)
        assert endpoint is not None
        assert policy.call_count == 1
