"""Unit tests for /onboarding routes.

Tests use TestClient + app.dependency_overrides with a FakeOnboardingService.
Anti-mock pattern: all fake methods are called across the test suite, with a
dedicated all-methods test (orchestrator-playbook §6).

Coverage:
  1. GET  /onboarding/state → {state, url} from fake service.
  2. POST /onboarding/advance happy path → 204.
  3. POST /onboarding/advance with guard unsatisfied → 409 + body shape.
  4. POST /onboarding/advance with invalid enum string → 422.
  5. POST /onboarding/skip-email happy → 204.
  6. POST /onboarding/skip-email in wrong state → 409.
  7. AC#4 parametrised: every FSM transition → 409 when guard flag unset.
  8. All fake methods exercised (anti-mock §6).
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fis_monitor.domain.errors import InvalidTransitionError
from fis_monitor.domain.models import OnboardingState
from fis_monitor.web.deps import get_onboarding
from fis_monitor.web.routes.onboarding import router

# ---------------------------------------------------------------------------
# Fake service
# ---------------------------------------------------------------------------


class FakeOnboardingService:
    """Fake OnboardingService — implements ALL public methods.

    ``advance_allowed``: when False, ``advance()`` raises ``InvalidTransitionError``
    (simulates any guard check failure — used in AC#4 parametrised test).
    ``skip_email_allowed``: when False, ``skip_email()`` raises.
    ``state``: current onboarding state returned by ``current()``.
    """

    def __init__(
        self,
        *,
        state: OnboardingState = OnboardingState.NOT_STARTED,
        advance_allowed: bool = True,
        skip_email_allowed: bool = True,
    ) -> None:
        self._state = state
        self._advance_allowed = advance_allowed
        self._skip_email_allowed = skip_email_allowed
        # Call tracking
        self.current_calls: int = 0
        self.can_advance_calls: list[tuple[OnboardingState, OnboardingState]] = []
        self.advance_calls: list[tuple[OnboardingState, OnboardingState]] = []
        self.skip_email_calls: int = 0
        self.url_for_current_step_calls: int = 0

    # Map: state → URL (mirrors OnboardingService._STATE_URL)
    _STATE_URL: ClassVar[dict[OnboardingState, str]] = {
        OnboardingState.NOT_STARTED: "/onboarding/regions",
        OnboardingState.REGIONS_SET: "/onboarding/smtp",
        OnboardingState.SMTP_CONFIGURED: "/onboarding/recipients",
        OnboardingState.RECIPIENTS_SET: "/onboarding/test-email",
        OnboardingState.COMPLETED: "/",
    }

    def current(self) -> OnboardingState:
        self.current_calls += 1
        return self._state

    def can_advance(
        self,
        from_state: OnboardingState,
        to_state: OnboardingState,
    ) -> bool:
        self.can_advance_calls.append((from_state, to_state))
        return self._advance_allowed

    def advance(
        self,
        from_state: OnboardingState,
        to_state: OnboardingState,
    ) -> None:
        self.advance_calls.append((from_state, to_state))
        if not self._advance_allowed:
            raise InvalidTransitionError(
                self._state.value,
                from_state.value,
                to_state.value,
            )

    def skip_email(self) -> None:
        self.skip_email_calls += 1
        if not self._skip_email_allowed:
            raise InvalidTransitionError(
                self._state.value,
                "smtp_configured|recipients_set",
                "skip_email",
            )

    def url_for_current_step(self) -> str:
        self.url_for_current_step_calls += 1
        return self._STATE_URL[self._state]


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _make_app(fake: FakeOnboardingService | None = None) -> tuple[FastAPI, FakeOnboardingService]:
    """Build a minimal FastAPI app with onboarding router and injected fake."""
    if fake is None:
        fake = FakeOnboardingService()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_onboarding] = lambda: fake
    return app, fake


# ---------------------------------------------------------------------------
# Anti-mock: exercise ALL fake methods in one test
# ---------------------------------------------------------------------------


def test_all_fake_methods_are_called() -> None:
    """Invoke ALL methods of FakeOnboardingService to catch API mismatches (§6)."""
    fake = FakeOnboardingService(state=OnboardingState.SMTP_CONFIGURED)

    state = fake.current()
    assert state is OnboardingState.SMTP_CONFIGURED
    assert fake.current_calls == 1

    ok = fake.can_advance(OnboardingState.NOT_STARTED, OnboardingState.REGIONS_SET)
    assert ok is True
    assert len(fake.can_advance_calls) == 1

    fake.advance(OnboardingState.NOT_STARTED, OnboardingState.REGIONS_SET)
    assert len(fake.advance_calls) == 1

    fake.skip_email()
    assert fake.skip_email_calls == 1

    url = fake.url_for_current_step()
    assert url == "/onboarding/recipients"
    assert fake.url_for_current_step_calls == 1


# ---------------------------------------------------------------------------
# GET /onboarding/state
# ---------------------------------------------------------------------------


def test_get_onboarding_state_not_started() -> None:
    """GET /onboarding/state returns state and URL for NOT_STARTED."""
    fake = FakeOnboardingService(state=OnboardingState.NOT_STARTED)
    app, f = _make_app(fake)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/onboarding/state")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "not_started"
    assert body["url"] == "/onboarding/regions"
    assert f.current_calls == 1
    assert f.url_for_current_step_calls == 1


def test_get_onboarding_state_completed() -> None:
    """GET /onboarding/state returns '/' when COMPLETED."""
    fake = FakeOnboardingService(state=OnboardingState.COMPLETED)
    app, _ = _make_app(fake)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/onboarding/state")
    assert resp.status_code == 200
    assert resp.json()["url"] == "/"


def test_get_onboarding_state_body_shape() -> None:
    """GET /onboarding/state response contains exactly 'state' and 'url' keys."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.get("/onboarding/state")
    body = resp.json()
    assert set(body.keys()) == {"state", "url"}


# ---------------------------------------------------------------------------
# POST /onboarding/advance — happy path
# ---------------------------------------------------------------------------


def test_advance_happy_path_204() -> None:
    """POST /onboarding/advance happy path returns 204."""
    fake = FakeOnboardingService(
        state=OnboardingState.NOT_STARTED,
        advance_allowed=True,
    )
    app, f = _make_app(fake)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post(
            "/onboarding/advance",
            json={"from_state": "not_started", "to_state": "regions_set"},
        )
    assert resp.status_code == 204
    assert len(f.advance_calls) == 1
    assert f.advance_calls[0] == (OnboardingState.NOT_STARTED, OnboardingState.REGIONS_SET)


# ---------------------------------------------------------------------------
# POST /onboarding/advance — guard unsatisfied → 409
# ---------------------------------------------------------------------------


def test_advance_guard_unsatisfied_409() -> None:
    """POST /onboarding/advance when guard fails → 409 with correct body shape."""
    fake = FakeOnboardingService(
        state=OnboardingState.NOT_STARTED,
        advance_allowed=False,
    )
    app, _ = _make_app(fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/onboarding/advance",
            json={"from_state": "not_started", "to_state": "regions_set"},
        )
    assert resp.status_code == 409
    # docs/onboarding.md 409 body shape:
    # {"error": "invalid_transition", "current_state": "<curr>", "redirect_to": "/onboarding"}
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_transition"
    assert detail["current_state"] == "not_started"
    assert detail["redirect_to"] == "/onboarding"


# ---------------------------------------------------------------------------
# POST /onboarding/advance — invalid enum string → 422
# ---------------------------------------------------------------------------


def test_advance_invalid_from_state_422() -> None:
    """POST /onboarding/advance with unknown from_state string → 422."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/onboarding/advance",
            json={"from_state": "bogus_state", "to_state": "regions_set"},
        )
    assert resp.status_code == 422


def test_advance_invalid_to_state_422() -> None:
    """POST /onboarding/advance with unknown to_state string → 422."""
    app, _ = _make_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/onboarding/advance",
            json={"from_state": "not_started", "to_state": "bogus_state"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /onboarding/skip-email
# ---------------------------------------------------------------------------


def test_skip_email_happy_204() -> None:
    """POST /onboarding/skip-email in valid state → 204."""
    fake = FakeOnboardingService(
        state=OnboardingState.SMTP_CONFIGURED,
        skip_email_allowed=True,
    )
    app, f = _make_app(fake)
    with TestClient(app, raise_server_exceptions=True) as client:
        resp = client.post("/onboarding/skip-email")
    assert resp.status_code == 204
    assert f.skip_email_calls == 1


def test_skip_email_wrong_state_409() -> None:
    """POST /onboarding/skip-email in wrong state → 409."""
    fake = FakeOnboardingService(
        state=OnboardingState.NOT_STARTED,
        skip_email_allowed=False,
    )
    app, _ = _make_app(fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/onboarding/skip-email")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_transition"
    assert detail["redirect_to"] == "/onboarding"


# ---------------------------------------------------------------------------
# Acceptance criterion #4 — parametrised guard test
# Every legal (from, to) transition → 409 when guard is unsatisfied
# ---------------------------------------------------------------------------

_ALL_TRANSITIONS: list[tuple[OnboardingState, OnboardingState]] = [
    (OnboardingState.NOT_STARTED, OnboardingState.REGIONS_SET),
    (OnboardingState.REGIONS_SET, OnboardingState.SMTP_CONFIGURED),
    (OnboardingState.SMTP_CONFIGURED, OnboardingState.RECIPIENTS_SET),
    (OnboardingState.RECIPIENTS_SET, OnboardingState.COMPLETED),
]


@pytest.mark.parametrize("from_state,to_state", _ALL_TRANSITIONS)
def test_every_transition_returns_409_when_guard_unsatisfied(
    from_state: OnboardingState,
    to_state: OnboardingState,
) -> None:
    """AC#4: for every legal FSM transition, route returns 409 when guard flag unset.

    FakeOnboardingService with advance_allowed=False simulates an unsatisfied guard
    for any (from, to) pair.  The parametrised iteration covers all 4 transitions.
    """
    fake = FakeOnboardingService(
        state=from_state,
        advance_allowed=False,
    )
    app, _ = _make_app(fake)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/onboarding/advance",
            json={"from_state": from_state.value, "to_state": to_state.value},
        )
    assert resp.status_code == 409, (
        f"Expected 409 for {from_state.value} → {to_state.value} with guard unsatisfied, "
        f"got {resp.status_code}"
    )
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_transition"
    assert detail["current_state"] == from_state.value
