"""Unit tests for POST /catchup/dismiss route.

Coverage:
  (a) POST /catchup/dismiss → 204 No Content.
  (b) FakeStateRepo receives set() call with key=catchup_dismissed_until
      and value ≈ now + 24h (± 2 seconds tolerance).
  (c) Fake covers all methods used by the SUT (anti-mock invariant).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.services.catchup_dismiss import CatchupDismissService
from fis_monitor.web.deps import get_catchup_dismiss
from fis_monitor.web.routes.catchup import router

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_DISMISSED_KEY = "catchup_dismissed_until"


class FakeStateRepo:
    """In-memory KV store that records set() calls for assertion."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    # Protocol completeness — not exercised by CatchupDismissService.
    def get_onboarding(self) -> object:  # pragma: no cover
        return None

    def set_onboarding(self, st: object) -> None:  # pragma: no cover
        pass


class FakeClock:
    """Returns a fixed UTC datetime."""

    def __init__(self, fixed: datetime) -> None:
        self._now = fixed

    def now(self) -> datetime:
        return self._now


# ---------------------------------------------------------------------------
# Anti-mock: all methods of FakeStateRepo used by the SUT
# ---------------------------------------------------------------------------


def test_fake_state_repo_all_methods() -> None:
    """Exercise get() and set() to detect runtime API bugs in the fake."""
    repo = FakeStateRepo()
    assert repo.get("x") is None
    repo.set("x", "y")
    assert repo.get("x") == "y"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _build_app(svc: CatchupDismissService) -> FastAPI:
    """Build a minimal FastAPI app with the catchup router and injected fake."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_catchup_dismiss] = lambda: svc
    return app


# ---------------------------------------------------------------------------
# (a) POST /catchup/dismiss → 204
# ---------------------------------------------------------------------------


class TestCatchupDismiss204:
    def test_returns_204(self) -> None:
        repo = FakeStateRepo()
        clock = FakeClock(datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC))
        svc = CatchupDismissService(state_repo=repo, clock=clock)
        app = _build_app(svc)

        with TestClient(app) as client:
            resp = client.post("/catchup/dismiss")

        assert resp.status_code == 204, (
            f"Expected 204, got {resp.status_code}: {resp.text}"
        )

    def test_response_body_is_empty(self) -> None:
        repo = FakeStateRepo()
        clock = FakeClock(datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC))
        svc = CatchupDismissService(state_repo=repo, clock=clock)
        app = _build_app(svc)

        with TestClient(app) as client:
            resp = client.post("/catchup/dismiss")

        assert resp.content == b"", f"Expected empty body, got: {resp.content!r}"


# ---------------------------------------------------------------------------
# (b) FakeStateRepo receives set() with correct timestamp (now + 24h ± 2s)
# ---------------------------------------------------------------------------


class TestCatchupDismissPersistence:
    def test_state_repo_receives_correct_timestamp(self) -> None:
        repo = FakeStateRepo()
        fixed_now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
        clock = FakeClock(fixed_now)
        svc = CatchupDismissService(state_repo=repo, clock=clock)
        app = _build_app(svc)

        before_call = datetime.now(UTC)
        with TestClient(app) as client:
            client.post("/catchup/dismiss")
        after_call = datetime.now(UTC)

        raw = repo.get(_DISMISSED_KEY)
        assert raw is not None, (
            f"Expected '{_DISMISSED_KEY}' to be set in state repo, but got None"
        )

        stored = datetime.fromisoformat(raw)

        # The route calls datetime.now(UTC) internally — the stored value must
        # be approximately now + 24h.  We allow a 2s window for test execution.
        lower = before_call + timedelta(hours=24) - timedelta(seconds=2)
        upper = after_call + timedelta(hours=24) + timedelta(seconds=2)
        assert lower <= stored <= upper, (
            f"Stored timestamp {stored!r} outside expected range "
            f"[{lower!r}, {upper!r}]"
        )

    def test_state_repo_key_name(self) -> None:
        """The KV key must be exactly 'catchup_dismissed_until'."""
        repo = FakeStateRepo()
        clock = FakeClock(datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC))
        svc = CatchupDismissService(state_repo=repo, clock=clock)
        app = _build_app(svc)

        with TestClient(app) as client:
            client.post("/catchup/dismiss")

        assert _DISMISSED_KEY in repo._store, (
            f"Expected key '{_DISMISSED_KEY}' in repo, got keys: {list(repo._store)}"
        )
