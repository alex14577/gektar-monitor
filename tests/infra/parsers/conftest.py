"""Shared fixtures for parser tests."""

from pathlib import Path

_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures"


def load_fixture(name: str) -> str:
    """Load an HTML fixture by filename."""
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")
