# -*- coding: utf-8 -*-
import pytest
from stats import average


def test_normal():
    assert average([2, 4, 6]) == 4


def test_empty_raises():
    with pytest.raises(ValueError, match="empty list"):
        average([])
