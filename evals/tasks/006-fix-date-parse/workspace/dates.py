# -*- coding: utf-8 -*-
def is_leap(year: int) -> bool:
    """闰年：能被4整除但不能被100整除，或能被400整除"""
    return year % 4 == 0


def days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if is_leap(year) else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31
