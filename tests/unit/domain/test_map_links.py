"""Unit tests for domain.map_links — pure-function invariants.

Covered invariants:
- roscadastres_map_url returns correct URL for a valid cadastral number.
- Colons in the cadastral number are NOT URL-encoded.
- Returns None for None input.
- Returns None for an empty string.
"""

from __future__ import annotations

import pytest

from fis_monitor.domain.map_links import roscadastres_map_url


class TestRoscadastresMapUrl:
    """Contract tests for roscadastres_map_url."""

    def test_valid_cadastral_no_returns_url(self) -> None:
        """Standard cadastral number produces the expected deep-link."""
        result = roscadastres_map_url("77:01:0006004:14")
        assert result == "https://ik5map.roscadastres.com/map.html?cn=77:01:0006004:14"

    def test_colons_not_url_encoded(self) -> None:
        """Colons must appear literally in the URL (roscadastres requires unencoded colons)."""
        result = roscadastres_map_url("27:23:0040000:0099")
        assert result is not None
        assert ":" in result
        assert "%3A" not in result

    @pytest.mark.parametrize("empty", [None, ""])
    def test_returns_none_for_absent_value(self, empty: str | None) -> None:
        """None and empty string both produce None — button is suppressed."""
        assert roscadastres_map_url(empty) is None
