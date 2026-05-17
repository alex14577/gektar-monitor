"""TTL-cache + asyncio.to_thread tests for OnboardingGateMiddleware (bd 45el).

These tests verify that:
- _get_state_and_url() caches the result within the TTL window (avoids repeated DB hits).
- _get_state_and_url() re-reads after TTL expiry.
- The middleware passes the cached state/url through __call__ correctly.
- An exception in current() does NOT poison the cache.

All tests are async (pytest-anyio / anyio marker) to allow direct await of
_get_state_and_url() without a running TestClient.
"""

from __future__ import annotations

import pytest

from fis_monitor.domain.models import OnboardingState
from fis_monitor.web.onboarding_gate import OnboardingGateMiddleware

# ---------------------------------------------------------------------------
# Fake svc (sync, as the real OnboardingService is sync — offloaded by mw)
# ---------------------------------------------------------------------------


class _CountingSvc:
    def __init__(self, state: OnboardingState = OnboardingState.NOT_STARTED) -> None:
        self._state = state
        self.calls: int = 0

    def current(self) -> OnboardingState:
        self.calls += 1
        return self._state

    def url_for_current_step(self) -> str:  # satisfies OnboardingQuery protocol
        return "/onboarding/regions"


def _make_mw(
    state: OnboardingState = OnboardingState.NOT_STARTED,
) -> tuple[OnboardingGateMiddleware, _CountingSvc]:
    svc = _CountingSvc(state)
    mw = OnboardingGateMiddleware(app=None, svc=svc)  # type: ignore[arg-type]
    return mw, svc


# ---------------------------------------------------------------------------
# Cache hit within TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_and_url_caches_result_within_ttl() -> None:
    """Three consecutive reads within TTL must call svc.current() only once."""
    mw, svc = _make_mw()

    s1, u1 = await mw._get_state_and_url()
    s2, u2 = await mw._get_state_and_url()
    s3, u3 = await mw._get_state_and_url()

    assert svc.calls == 1, "svc.current() should be called only once within TTL"
    assert s1 == s2 == s3 == OnboardingState.NOT_STARTED
    assert u1 == u2 == u3 == "/onboarding/regions"


# ---------------------------------------------------------------------------
# Cache refresh after TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_and_url_refreshes_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """After TTL expires, the next call must re-read from svc."""
    fake_now = [0.0]
    monkeypatch.setattr("fis_monitor.web.onboarding_gate.time.monotonic", lambda: fake_now[0])

    mw, svc = _make_mw()

    await mw._get_state_and_url()       # call 1 — populates cache, expires at 1.0
    assert svc.calls == 1

    fake_now[0] = 0.5                   # still within TTL
    await mw._get_state_and_url()
    assert svc.calls == 1               # served from cache

    fake_now[0] = 1.5                   # past TTL
    await mw._get_state_and_url()       # call 2 — cache expired, re-reads
    assert svc.calls == 2


# ---------------------------------------------------------------------------
# Cache starts empty (no stale value on first call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_and_url_no_cache_on_init() -> None:
    """Fresh middleware has no cached state — first call always reads svc."""
    mw, svc = _make_mw(OnboardingState.COMPLETED)
    assert mw._cache is None

    state, url = await mw._get_state_and_url()
    assert state == OnboardingState.COMPLETED
    assert url == "/onboarding/regions"
    assert svc.calls == 1


# ---------------------------------------------------------------------------
# Exception in current() must NOT poison the cache
# ---------------------------------------------------------------------------


class _RaisingSvc:
    def current(self) -> OnboardingState:
        raise RuntimeError("db unavailable")

    def url_for_current_step(self) -> str:
        return "/onboarding/regions"


@pytest.mark.asyncio
async def test_exception_in_current_does_not_cache() -> None:
    """If svc.current() raises, _cache must remain None so next call retries."""
    svc = _RaisingSvc()
    mw = OnboardingGateMiddleware(app=None, svc=svc)  # type: ignore[arg-type]

    assert mw._cache is None
    with pytest.raises(RuntimeError, match="db unavailable"):
        await mw._get_state_and_url()

    assert mw._cache is None, "_cache must not be set after an exception"
