"""Tests for the domain exception hierarchy."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# T11 — ParseBugError vs ParserVersionMismatch are siblings under DomainError
# ---------------------------------------------------------------------------
def test_parse_bug_vs_version_mismatch_no_subclass():
    from fis_monitor.domain import (
        DomainError,
        ParseBugError,
        ParserVersionMismatch,
        UpstreamError,
    )

    # Siblings, NOT a subclass relationship.
    assert not issubclass(ParserVersionMismatch, ParseBugError)
    assert not issubclass(ParseBugError, ParserVersionMismatch)

    # Both root in DomainError.
    assert issubclass(ParseBugError, DomainError)
    assert issubclass(ParserVersionMismatch, DomainError)
    assert issubclass(UpstreamError, DomainError)

    # DomainError is an Exception (so raise/except work).
    assert issubclass(DomainError, Exception)


def test_domain_errors_are_raisable():
    import pytest

    from fis_monitor.domain import ParseBugError, ParserVersionMismatch, UpstreamError

    with pytest.raises(ParseBugError):
        raise ParseBugError("boom")
    with pytest.raises(ParserVersionMismatch):
        raise ParserVersionMismatch("v mismatch")
    with pytest.raises(UpstreamError):
        raise UpstreamError("upstream")
