"""Explicit Notifier registry — composition-root component (ADR-002).

Stores registered ``Notifier`` plugin instances by ``channel_id``.

Not thread-safe by design: the composition root assembles this registry
in a single thread before any workers are started.  No locking is needed
or provided.
"""

from __future__ import annotations

from fis_monitor.domain.errors import RegistrationError
from fis_monitor.domain.interfaces import Notifier


class ExplicitNotifierRegistry:
    """Explicit registry for ``Notifier`` plugin instances.

    Composition-root component (ADR-002).  Callers register concrete
    notifier instances via ``register()``.  The registry stores them
    keyed by ``channel_id`` and exposes ``get()``, ``has()``, and
    ``all()`` for service-layer consumers.

    **Single-thread registration invariant**: this class is assembled in
    the composition root before any workers are started.  It is NOT
    thread-safe.  Do not call ``register()`` from worker threads.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Notifier] = {}

    def register(self, notifier: Notifier) -> None:
        """Register a ``Notifier`` instance.

        Args:
            notifier: An object that satisfies the ``Notifier`` Protocol.

        Raises:
            RegistrationError: If ``notifier`` does not satisfy the
                ``Notifier`` Protocol, or if its ``channel_id`` has
                already been registered.
        """
        if not isinstance(notifier, Notifier):
            raise RegistrationError("object does not satisfy Notifier Protocol")

        channel_id: str = type(notifier).channel_id
        if channel_id in self._by_id:
            raise RegistrationError(f"duplicate channel_id: {channel_id}")

        self._by_id[channel_id] = notifier

    def get(self, channel_id: str) -> Notifier:
        """Return the registered notifier for ``channel_id``.

        Args:
            channel_id: The channel identifier to look up.

        Returns:
            The registered ``Notifier`` instance.

        Raises:
            KeyError: If no notifier is registered for ``channel_id``.
        """
        return self._by_id[channel_id]

    def has(self, channel_id: str) -> bool:
        """Return ``True`` if a notifier is registered for ``channel_id``."""
        return channel_id in self._by_id

    def all(self) -> list[Notifier]:
        """Return all registered notifiers in registration order."""
        return list(self._by_id.values())
