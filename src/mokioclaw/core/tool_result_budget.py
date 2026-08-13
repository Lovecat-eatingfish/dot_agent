"""
L1 Tool-Result Budget — 大输出落盘

当工具输出超过阈值时，将完整内容保存到本地文件，
messages 中只保留预览 + 文件路径提示，避免上下文爆炸。

这是五级上下文压缩的第一层，属于 0 成本规则级压缩（不需要 LLM）。

用法：
    budget = ToolResultBudget(max_chars=50000)
    processed = budget.apply(result_dict, "BashTool", workspace_path)
    # 如果输出超大，processed 会包含截断预览 + _full_output_path 字段
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

# 默认阈值：超过此字符数的输出会被落盘
DEFAULT_MAX_CHARS = 50_000

# 预览保留的字符数
PREVIEW_CHARS = 2_000


@dataclass
class ToolResultBudget:
    """工具输出预算管理器

    检查工具输出大小，超大内容落盘，返回截断预览。
    """
    max_chars: int = DEFAULT_MAX_CHARS

    def apply(
        self,
        result: dict[str, Any],
        tool_name: str,
        workspace: Path,
    ) -> dict[str, Any]:
        """检查并处理工具输出

        如果输出内容总字符数超过 max_chars：
        1. 将完整内容保存到 .mokioclaw/tool-outputs/ 目录
        2. 返回截断的预览 + 文件路径提示

        Args:
            result: 工具返回的结果字典
            tool_name: 工具名称（用于生成文件名）
            workspace: 工作区路径

        Returns:
            处理后的结果字典。超大输出会新增 _full_output_path 字段。
        """
        total_chars = self._estimate_chars(result)
        if total_chars <= self.max_chars:
            return result

        # 超大输出，落盘
        output_dir = workspace / ".mokioclaw" / "tool-outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        # 秒级时间戳 + 进程内计数器，避免同一秒并行落盘文件名碰撞覆盖（m3）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        seq = self._next_seq()
        output_file = output_dir / f"{tool_name}_{timestamp}_{seq}.txt"

        try:
            full_text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            output_file.write_text(full_text, encoding="utf-8")
            logger.info(
                "Tool result budget: %s output %d chars > %d, saved to %s",
                tool_name, total_chars, self.max_chars, output_file,
            )
        except OSError as exc:
            logger.warning("Failed to save tool output: %s", exc)
            return result

        # 构造截断预览
        preview = self._make_preview(result, total_chars)
        preview["_full_output_path"] = str(output_file)
        preview["_truncated"] = True
        preview["_original_chars"] = total_chars
        return preview

    def _next_seq(self) -> int:
        """进程内单调递增计数器，区分同一秒内的多次落盘"""
        # dataclass field 默认值无法直接放可变计数器，用 __dict__ 存
        seq = self.__dict__.get("_seq", 0) + 1
        self.__dict__["_seq"] = seq
        return seq

    def _estimate_chars(self, result: dict[str, Any]) -> int:
        """估算结果字典的字符总数（保守上界，避免漏判落盘）"""
        try:
            return len(json.dumps(result, ensure_ascii=False, default=str))
        except Exception:
            return 0

    def _make_preview(self, result: dict[str, Any], total_chars: int) -> dict[str, Any]:
        """生成截断预览

        对 str/list/dict 都做递归截断（M5：原实现 dict 值原样拷贝不截断，
        嵌套大字典经 json.dumps 直接进上下文，预览预算失效）。
        """
        preview = {}
        for key, value in result.items():
            preview[key] = self._truncate_value(value, PREVIEW_CHARS)
        return preview

    def _truncate_value(self, value: Any, budget: int) -> Any:
        """递归截断：str 直接截；list/dict 序列化后超 budget 则摘要"""
        if isinstance(value, str):
            if len(value) > budget:
                return value[:budget] + f"\n... [truncated, {len(value)} chars total]"
            return value
        if isinstance(value, list):
            try:
                serialized = json.dumps(value, default=str)
            except Exception:  # noqa: BLE001
                return f"[list with {len(value)} items]"
            if len(serialized) > budget:
                return f"[list with {len(value)} items, truncated]"
            return value
        if isinstance(value, dict):
            try:
                serialized = json.dumps(value, default=str)
            except Exception:  # noqa: BLE001
                return f"[dict with {len(value)} keys]"
            if len(serialized) > budget:
                return f"[dict with {len(value)} keys, truncated]"
            return value
        return value
