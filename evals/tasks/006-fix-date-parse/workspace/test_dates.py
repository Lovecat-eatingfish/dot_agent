# -*- coding: utf-8 -*-
from dates import is_leap, days_in_month


def test_leap_years():
    assert is_leap(2024) is True
    assert is_leap(2000) is True
    assert is_leap(1900) is False
    assert is_leap(2023) is False


def test_feb_days():
    assert days_in_month(2024, 2) == 29
    assert days_in_month(2000, 2) == 29
    assert days_in_month(1900, 2) == 28
    assert days_in_month(2023, 2) == 28
