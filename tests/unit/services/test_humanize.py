"""Unit tests for ``humanize_relative_age`` (bd 47uh)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fis_monitor.services.humanize import humanize_relative_age


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(seconds=-5), "только что"),  # negative (clock skew)
        (timedelta(seconds=0), "только что"),
        (timedelta(seconds=59), "только что"),
        (timedelta(seconds=60), "1 мин назад"),
        (timedelta(minutes=30), "30 мин назад"),
        (timedelta(minutes=59), "59 мин назад"),
        (timedelta(minutes=60), "1 ч назад"),
        (timedelta(hours=5), "5 ч назад"),
        (timedelta(hours=23, minutes=59), "23 ч назад"),
        (timedelta(hours=24), "1 дн назад"),
        (timedelta(days=7), "7 дн назад"),
    ],
)
def test_humanize_relative_age_buckets(age, expected) -> None:
    assert humanize_relative_age(age) == expected
