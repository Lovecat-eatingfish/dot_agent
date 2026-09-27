"""Tests for dot.agent.loop_detector — 工具循环检测器"""
import pytest
from dot.agent.loop_detector import ToolCallTracker, ToolLoopViolation


class TestToolCallTracker:
    """测试工具循环检测器"""

    def test_consecutive_same_tool(self):
        """测试连续相同工具检测"""
        tracker = ToolCallTracker(max_consecutive_same_tool=3)

        # 调用3次相同工具
        tracker.record("bash", {"command": "ls"})
        assert tracker.check_violation() is None

        tracker.record("bash", {"command": "pwd"})
        assert tracker.check_violation() is None

        tracker.record("bash", {"command": "whoami"})
        violation = tracker.check_violation()
        assert violation is not None
        assert violation.violation_type == "consecutive"
        assert violation.tool_name == "bash"
        assert violation.count == 3

    def test_consecutive_different_tools(self):
        """测试不同工具不触发连续检测"""
        tracker = ToolCallTracker(max_consecutive_same_tool=3)

        tracker.record("bash", {"command": "ls"})
        tracker.record("read_file", {"path": "a.txt"})
        tracker.record("bash", {"command": "pwd"})
        assert tracker.check_violation() is None

    def test_same_args_repeat(self):
        """测试相同参数重复检测（非连续调用）"""
        tracker = ToolCallTracker(max_same_args_repeat=2, max_consecutive_same_tool=5)

        # 调用相同工具+相同参数，但中间插入其他工具
        tracker.record("bash", {"command": "ls -la"})
        assert tracker.check_violation() is None

        tracker.record("read_file", {"path": "a.txt"})  # 插入其他工具
        assert tracker.check_violation() is None

        tracker.record("bash", {"command": "ls -la"})  # 相同参数重复
        violation = tracker.check_violation()
        assert violation is not None
        assert violation.violation_type == "same_args"
        assert violation.tool_name == "bash"
        assert violation.same_args_count == 2

    def test_same_args_different_values(self):
        """测试不同参数值不触发"""
        tracker = ToolCallTracker(max_same_args_repeat=2)

        tracker.record("bash", {"command": "ls -la"})
        tracker.record("bash", {"command": "ls -lh"})
        assert tracker.check_violation() is None

    def test_window_frequency(self):
        """测试窗口频率检测（非连续调用）"""
        tracker = ToolCallTracker(
            window_size=5, max_same_tool_in_window=3, max_consecutive_same_tool=5
        )

        # 5轮内调用3次相同工具，但中间插入其他工具
        tracker.record("grep", {"pattern": "a"})
        assert tracker.check_violation() is None

        tracker.record("bash", {"command": "ls"})  # 插入其他工具
        assert tracker.check_violation() is None

        tracker.record("grep", {"pattern": "b"})
        assert tracker.check_violation() is None

        tracker.record("read_file", {"path": "a.txt"})  # 插入其他工具
        assert tracker.check_violation() is None

        tracker.record("grep", {"pattern": "c"})
        violation = tracker.check_violation()
        assert violation is not None
        assert violation.violation_type == "window_freq"
        assert violation.tool_name == "grep"
        assert violation.window == 5

    def test_window_frequency_different_tools(self):
        """测试窗口内不同工具不触发"""
        tracker = ToolCallTracker(window_size=5, max_same_tool_in_window=3)

        tracker.record("grep", {"pattern": "a"})
        tracker.record("bash", {"command": "ls"})
        tracker.record("grep", {"pattern": "b"})
        assert tracker.check_violation() is None

    def test_pair_cycle(self):
        """测试 ABAB 循环检测"""
        tracker = ToolCallTracker(
            max_pair_cycle=3,  # 需要3次循环才触发
            max_same_args_repeat=5,
            max_consecutive_same_tool=5,
            window_size=10,
            max_same_tool_in_window=10,
        )

        # ABABAB 模式（3次循环），使用不同参数避免触发 same_args
        tracker.record("read_file", {"path": "a.txt"})
        assert tracker.check_violation() is None

        tracker.record("write_file", {"path": "b.txt"})
        assert tracker.check_violation() is None  # 1次循环

        tracker.record("read_file", {"path": "c.txt"})
        assert tracker.check_violation() is None

        tracker.record("write_file", {"path": "d.txt"})
        assert tracker.check_violation() is None  # 2次循环

        tracker.record("read_file", {"path": "e.txt"})
        assert tracker.check_violation() is None

        tracker.record("write_file", {"path": "f.txt"})
        violation = tracker.check_violation()
        assert violation is not None
        assert violation.violation_type == "pair_cycle"
        assert "read_file" in violation.tool_name
        assert "write_file" in violation.tool_name
        assert violation.count == 3

    def test_pair_cycle_different_tools(self):
        """测试非 ABAB 模式不触发"""
        tracker = ToolCallTracker(max_pair_cycle=2)

        # ABC 模式
        tracker.record("read_file", {"path": "a.txt"})
        tracker.record("write_file", {"path": "a.txt"})
        tracker.record("bash", {"command": "ls"})
        assert tracker.check_violation() is None

    def test_reset(self):
        """测试重置追踪器"""
        tracker = ToolCallTracker(max_consecutive_same_tool=2)

        tracker.record("bash", {"command": "ls"})
        tracker.record("bash", {"command": "ls"})
        assert tracker.check_violation() is not None

        tracker.reset()
        assert tracker.check_violation() is None

    def test_history(self):
        """测试历史记录"""
        tracker = ToolCallTracker()

        tracker.record("bash", {"command": "ls"})
        tracker.record("read_file", {"path": "a.txt"})

        history = tracker.history
        assert len(history) == 2
        assert history[0].tool_name == "bash"
        assert history[1].tool_name == "read_file"

    def test_from_preset_default(self):
        """测试默认预设"""
        tracker = ToolCallTracker.from_preset("default")
        assert tracker._max_consecutive == 3
        assert tracker._max_same_args == 2
        assert tracker._window_size == 5
        assert tracker._max_in_window == 3
        assert tracker._max_pair_cycle == 2

    def test_from_preset_strict(self):
        """测试严格预设"""
        tracker = ToolCallTracker.from_preset("strict")
        assert tracker._max_consecutive == 2
        assert tracker._max_same_args == 1
        assert tracker._window_size == 3
        assert tracker._max_in_window == 2
        assert tracker._max_pair_cycle == 1

    def test_from_preset_lenient(self):
        """测试宽松预设"""
        tracker = ToolCallTracker.from_preset("lenient")
        assert tracker._max_consecutive == 5
        assert tracker._max_same_args == 3
        assert tracker._window_size == 10
        assert tracker._max_in_window == 5
        assert tracker._max_pair_cycle == 3

    def test_invalid_config(self):
        """测试无效配置"""
        with pytest.raises(ValueError, match="max_consecutive_same_tool must be >= 1"):
            ToolCallTracker(max_consecutive_same_tool=0)

        with pytest.raises(ValueError, match="max_same_args_repeat must be >= 1"):
            ToolCallTracker(max_same_args_repeat=0)

        with pytest.raises(ValueError, match="window_size must be >= 2"):
            ToolCallTracker(window_size=1)

    def test_no_violation_normal_usage(self):
        """测试正常使用不触发"""
        tracker = ToolCallTracker()

        # 正常使用模式
        tracker.record("read_file", {"path": "a.txt"})
        tracker.record("write_file", {"path": "b.txt"})
        tracker.record("bash", {"command": "ls"})
        tracker.record("grep", {"pattern": "test"})
        tracker.record("glob_search", {"pattern": "*.py"})

        assert tracker.check_violation() is None

    def test_consecutive_then_different(self):
        """测试连续调用后换工具重置计数"""
        tracker = ToolCallTracker(max_consecutive_same_tool=3, max_same_args_repeat=5)

        tracker.record("bash", {"command": "ls"})
        tracker.record("bash", {"command": "pwd"})  # 不同参数
        assert tracker.check_violation() is None

        # 换工具，连续计数重置
        tracker.record("read_file", {"path": "a.txt"})
        assert tracker.check_violation() is None

        # 再次调用 bash，连续计数从1开始
        tracker.record("bash", {"command": "whoami"})
        assert tracker.check_violation() is None

    def test_multiple_violations_priority(self):
        """测试多个违规同时存在时返回第一个"""
        tracker = ToolCallTracker(
            max_consecutive_same_tool=2,
            max_same_args_repeat=2,
        )

        # 同时满足连续和相同参数
        tracker.record("bash", {"command": "ls"})
        tracker.record("bash", {"command": "ls"})

        violation = tracker.check_violation()
        # 应该返回第一个检测到的（连续）
        assert violation is not None
        assert violation.violation_type == "consecutive"


class TestToolLoopViolation:
    """测试 ToolLoopViolation 数据结构"""

    def test_violation_attributes(self):
        """测试违规信息属性"""
        violation = ToolLoopViolation(
            violation_type="consecutive",
            tool_name="bash",
            description="工具 bash 连续调用 3 次",
            count=3,
            suggestion="尝试不同的方法",
        )

        assert violation.violation_type == "consecutive"
        assert violation.tool_name == "bash"
        assert violation.count == 3
        assert violation.window is None
        assert violation.same_args_count == 0

    def test_violation_with_window(self):
        """测试带窗口的违规信息"""
        violation = ToolLoopViolation(
            violation_type="window_freq",
            tool_name="grep",
            description="工具 grep 在最近 5 轮中调用 3 次",
            count=3,
            window=5,
        )

        assert violation.window == 5
