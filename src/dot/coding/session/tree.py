"""
dot.coding.session.tree — SessionTree 会话分支管理

通过 parent_id 链接实现树形分支。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TreeNode:
    """树节点"""
    session_id: str
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)
    is_leaf: bool = True


class SessionTree:
    """会话分支树"""

    def __init__(self) -> None:
        self._nodes: dict[str, TreeNode] = {}

    def add(self, session_id: str, parent_id: str | None = None) -> TreeNode:
        """添加节点"""
        node = TreeNode(session_id=session_id, parent_id=parent_id)
        self._nodes[session_id] = node

        if parent_id and parent_id in self._nodes:
            parent = self._nodes[parent_id]
            parent.children.append(session_id)
            parent.is_leaf = False

        return node

    def get(self, session_id: str) -> TreeNode | None:
        return self._nodes.get(session_id)

    def leaves(self) -> list[str]:
        """获取所有叶子节点"""
        return [nid for nid, node in self._nodes.items() if node.is_leaf]

    def path_to_root(self, session_id: str) -> list[str]:
        """获取从节点到根的路径"""
        path = []
        current = session_id
        while current and current in self._nodes:
            path.append(current)
            current = self._nodes[current].parent_id or ""
        return path
