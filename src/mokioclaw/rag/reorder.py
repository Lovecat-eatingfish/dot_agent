"""Lost-in-the-Middle 重排（对齐高级 RAG）

研究（Liu et al. 2023）表明：LLM 对长上下文中间位置的信息利用率低，
即使相关信息在中间也会被忽略（lost in the middle）。

策略：把最相关的片段放头尾，较弱的放中间。
- 输入：按相关度降序的片段列表
- 输出：头放最相关、尾放次相关、中间放其余，规避 LIM 效应

纯规则，零成本，无依赖。
"""
from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def reorder_lost_in_the_middle(items: list[T]) -> list[T]:
    """重排：最相关片段放头尾，弱相关放中间

    输入 items 假定已按相关度降序排好（retrieve/rerank 后）。
    策略：偶数 rank 放尾部（从后往前填），奇数 rank 放头部（从前往后填）。
    结果：[1, 3, 5, ..., 6, 4, 2]（1 最相关放头，2 次相关放尾）。

    >>> reorder_lost_in_the_middle(['a','b','c','d','e'])
    ['a', 'c', 'e', 'd', 'b']
    """
    if len(items) <= 2:
        return list(items)
    head: list[T] = []
    tail: list[T] = []
    # 交替分配：rank 0 → head, rank 1 → tail, rank 2 → head, ...
    # 但要保证最相关(rank0)在头、次相关(rank1)在尾
    for i, item in enumerate(items):
        if i % 2 == 0:
            head.append(item)
        else:
            tail.append(item)
    # tail 是 [rank1, rank3, rank5...]，要倒序让 rank1 最靠近末尾
    tail.reverse()
    return head + tail
