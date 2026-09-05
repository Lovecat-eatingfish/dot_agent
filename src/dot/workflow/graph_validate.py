# dot.workflow.graph_validate — GraphValidator（静态图校验）
#
# 只依赖图的纯结构数据（节点名集合 / 入口 / 边 / 路由），
# 与图的定义和执行解耦：WorkflowGraph.validate() 委托到这里。
from __future__ import annotations

from typing import Mapping

from .graph_types import END, Router, WorkflowValidationError


class GraphValidator:
    """静态图校验：入口、不可达节点、到 END 的路径、环路"""

    def __init__(
        self,
        *,
        nodes: Mapping[str, object],
        entry: str | None,
        edges: Mapping[str, str],
        routers: Mapping[str, Router],
    ) -> None:
        self._node_names = list(nodes)
        self._entry = entry
        self._edges = edges
        self._routers = routers

    def validate(self) -> None:
        """完整图校验：入口、无效边、不可达节点、环路"""
        errors: list[str] = []

        # 1. 入口检查
        if self._entry is None:
            raise WorkflowValidationError("entry node is not set")

        # 2. 检查孤立节点（无入边且非入口）
        #    条件路由的目标在运行时才确定，无法静态分析，
        #    因此有 router 时跳过不可达检查。
        if not self._routers:
            all_targets: set[str] = set()
            for target in self._edges.values():
                if target != END:
                    all_targets.add(target)

            unreachable = [
                name for name in self._node_names
                if name != self._entry and name not in all_targets
            ]
            if unreachable:
                errors.append(f"unreachable nodes: {unreachable}")

        # 3. 出口检查：所有节点都必须可达 END
        #    条件分支的节点需要在 router 中显式处理 END
        exit_nodes = [
            name for name in self._node_names
            if name not in self._edges and name not in self._routers
        ]
        # 排除入口（可能一步就到 END）
        exit_without_path = [
            n for n in exit_nodes
            if n != self._entry and not self._has_path_to_end(n)
        ]
        if exit_without_path:
            errors.append(f"nodes without path to END: {exit_without_path}")

        # 4. 环路检测（DFS）
        cycles = self._detect_cycles()
        if cycles:
            errors.append(f"cycles detected: {cycles}")

        if errors:
            raise WorkflowValidationError("; ".join(errors))

    def _successors(self, name: str) -> list[str]:
        """节点的所有静态后继（条件边不确定后继）"""
        if name in self._edges:
            return [self._edges[name]] if self._edges[name] != END else []
        return []

    def _has_path_to_end(self, start: str, visited: set[str] | None = None) -> bool:
        """检查从 start 是否有路径到达 END"""
        if visited is None:
            visited = set()
        if start in visited:
            return False
        visited.add(start)

        if start in self._edges:
            return self._edges[start] == END or self._has_path_to_end(
                self._edges[start], visited
            )
        if start in self._routers:
            return True
        # 无出边的终端节点隐式结束工作流
        return True

    def _detect_cycles(self) -> list[list[str]]:
        """DFS 检测所有环路"""
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in self._node_names}
        parent: dict[str, str | None] = {n: None for n in self._node_names}
        cycles: list[list[str]] = []

        def dfs(node: str) -> None:
            color[node] = GREY
            for succ in self._successors(node):
                if color[succ] == GREY:
                    # 找到环路
                    cycle = [succ]
                    cur = node
                    while cur != succ:
                        cycle.append(cur)
                        cur = parent[cur]  # type: ignore
                    cycle.reverse()
                    cycles.append(cycle)
                elif color[succ] == WHITE:
                    parent[succ] = node
                    dfs(succ)
            color[node] = BLACK

        for node in self._node_names:
            if color[node] == WHITE:
                dfs(node)

        return cycles
