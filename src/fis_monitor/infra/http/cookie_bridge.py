"""RequestsCookieStore — CookieStore Protocol implementation.

Bridges Playwright-obtained cookies into a ``requests.Session`` so that
``RequestsHttpClient`` sends authenticated requests without importing
or depending on Playwright directly.

Design (ADR-034):
- Implements the ``CookieStore`` Protocol (``domain/interfaces.py``).
- Translates Playwright cookie dicts into ``requests.cookies.RequestsCookieJar``
  entries via ``requests.cookies.create_cookie``.
- Thread-safe: ``requests.Session.cookies`` is not modified concurrently
  because the login flow (``PlaywrightLoginSession``) holds ``_lock`` for
  the duration of the Playwright session; ``store()`` is called before
  ``_lock`` is released.  No additional locking needed here.

Playwright cookie dict fields:
  name, value, domain, path, expires (float, -1 == session cookie),
  httpOnly, secure, sameSite.

References:
  - ``domain/interfaces.py::CookieStore`` Protocol
  - ``infra/playwright/login.py::PlaywrightLoginSession``
  - ADR-034 (cookie bridge Playwright → requests)
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

import requests
import requests.cookies

__all__ = ["RequestsCookieStore"]

_log = logging.getLogger(__name__)


class RequestsCookieStore:
    """Translates Playwright cookie dicts into a ``requests.Session`` cookie jar.

    Implements ``CookieStore`` Protocol (structural typing — no explicit
    ``Protocol`` import required here).

    Args:
        session: The ``requests.Session`` instance whose cookie jar will be
            populated.  Must be the same session passed to ``RequestsHttpClient``.
    """

    def __init__(self, session: requests.Session) -> None:
        self._session = session

    def store(self, cookies: Sequence[Mapping[str, object]]) -> None:
        """Load Playwright cookie dicts into the session cookie jar.

        Each dict is converted via ``requests.cookies.create_cookie`` and
        set on the session jar.  Existing cookies for the same name/domain
        are overwritten.

        Args:
            cookies: Sequence of Playwright cookie dicts.  Expected keys:
                ``name`` (str), ``value`` (str), ``domain`` (str),
                ``path`` (str), ``expires`` (float, -1 for session cookies),
                ``httpOnly`` (bool), ``secure`` (bool), ``sameSite`` (str).
                Unknown keys are silently ignored.
        """
        if not cookies:
            _log.debug("RequestsCookieStore.store: empty cookie list — no-op")
            return

        loaded = 0
        for raw in cookies:
            name = str(raw.get("name", ""))
            value = str(raw.get("value", ""))
            domain = str(raw.get("domain", ""))
            path = str(raw.get("path", "/"))
            secure = bool(raw.get("secure", False))
            http_only = bool(raw.get("httpOnly", False))

            # Playwright uses -1 for session cookies (no expiry).
            # requests.cookies.create_cookie expects int | None; None = session.
            raw_expires = raw.get("expires", -1)
            try:
                expires_float = float(raw_expires)  # type: ignore[arg-type]  # object narrowed at runtime
            except (TypeError, ValueError):
                expires_float = -1.0
            expires: int | None = int(expires_float) if expires_float > 0 else None

            # rest dict carries additional cookie attributes that
            # requests.cookies.create_cookie passes to http.cookiejar.Cookie.
            rest: dict[str, str] = {}
            if http_only:
                rest["HttpOnly"] = ""

            try:
                cookie = requests.cookies.create_cookie(
                    name=name,
                    value=value,
                    domain=domain,
                    path=path,
                    expires=expires,
                    secure=secure,
                    rest=rest,
                )
                self._session.cookies.set_cookie(cookie)
                loaded += 1
            except Exception:
                _log.warning(
                    "RequestsCookieStore.store: failed to create cookie name=%r domain=%r",
                    name,
                    domain,
                    exc_info=True,
                )

        _log.debug(
            "RequestsCookieStore.store: loaded %d/%d cookies into session",
            loaded,
            len(cookies),
        )
