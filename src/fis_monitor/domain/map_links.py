"""Pure helper functions for building external map deep-links.

This module is a leaf in the domain layer — importable by both web and
infra layers without introducing layer inversion. It has no dependencies
on models.py or any other domain artefacts.
"""

from __future__ import annotations

_ROSCADASTRES_BASE = "https://ik5map.roscadastres.com/map.html"


def roscadastres_map_url(cadastral_no: str | None) -> str | None:
    """Return a Роскадастр deep-link for the given cadastral number.

    Colons in the cadastral number are passed through as-is (NOT
    URL-encoded) — the roscadastres.com ``?cn=`` parameter requires
    literal colons (e.g. ``77:01:0006004:14``).

    Args:
        cadastral_no: Cadastral number string (e.g. ``"77:01:0006004:14"``).
            Returns ``None`` if the value is ``None`` or an empty string.

    Returns:
        Full URL string, or ``None`` when *cadastral_no* is absent.
    """
    if not cadastral_no:
        return None
    return f"{_ROSCADASTRES_BASE}?cn={cadastral_no}"
