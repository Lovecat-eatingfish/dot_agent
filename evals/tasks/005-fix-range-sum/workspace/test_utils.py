# -*- coding: utf-8 -*-
from utils import sum_range


def test_inclusive_bounds():
    assert sum_range(1, 5) == 15


def test_negative():
    assert sum_range(-2, 2) == 0


def test_single():
    assert sum_range(3, 3) == 3
