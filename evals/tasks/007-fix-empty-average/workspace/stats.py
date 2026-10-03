# -*- coding: utf-8 -*-
def average(numbers: list) -> float:
    """平均值；空列表必须抛 ValueError("empty list")"""
    return sum(numbers) / len(numbers)
