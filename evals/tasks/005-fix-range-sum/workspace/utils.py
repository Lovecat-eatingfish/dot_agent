# -*- coding: utf-8 -*-
def sum_range(a: int, b: int) -> int:
    """返回 a..b（含两端）的所有整数之和"""
    total = 0
    for i in range(a, b):
        total += i
    return total
