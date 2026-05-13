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

    with pytest.raises(ParseBugError) as exc_info:
        raise ParseBugError(selector="intentional-test-raise")
    assert exc_info.value.selector == "intentional-test-raise"
    with pytest.raises(ParserVersionMismatch):
        raise ParserVersionMismatch("v mismatch")
    with pytest.raises(UpstreamError):
        raise UpstreamError("upstream")


def test_parse_bug_error_str_does_not_leak_html_or_pii():
    """str(ParseBugError) and repr(ParseBugError) must not contain raw HTML, URLs or emails.

    PII-safety is a caller-responsibility convention — this test guards the
    well-formed-input path: when caller supplies safe selector+context, the
    serialised exception remains safe.
    """
    from fis_monitor.domain import ParseBugError

    exc = ParseBugError(selector="tbody.lots-list", context="empty rows")
    s = str(exc)
    r = repr(exc)
    # PII-safe characters only — no HTML brackets, no URL schemas, no @ signs
    for bad_char in ("<", ">", "http://", "https://", "@"):
        assert bad_char not in s, f"str(exc) leaked {bad_char!r}: {s}"
        assert bad_char not in r, f"repr(exc) leaked {bad_char!r}: {r}"
    # Positive: safe content present
    assert "tbody.lots-list" in s
    assert "empty rows" in s
