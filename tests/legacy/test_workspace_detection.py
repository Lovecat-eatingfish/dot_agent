"""
测试智能工作区检测

验证类似 Claude Code 的工作区自动检测逻辑
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mokioclaw.core.workspace_detection import (
    detect_workspace_from_file,
    detect_workspace_from_project,
    resolve_workspace,
)


class TestWorkspaceFromFile:
    """测试从打开的文件检测工作区"""

    def test_file_path_returns_parent(self, tmp_path: Path) -> None:
        """文件路径应返回其父目录"""
        test_file = tmp_path / "subdir" / "test.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)

        result = detect_workspace_from_file(test_file)
        assert result == test_file.parent

    def test_directory_path_returns_itself(self, tmp_path: Path) -> None:
        """目录路径应返回自身"""
        result = detect_workspace_from_file(tmp_path)
        assert result == tmp_path

    def test_nonexistent_file_returns_parent(self, tmp_path: Path) -> None:
        """不存在的文件路径返回父目录"""
        result = detect_workspace_from_file(tmp_path / "nonexistent.txt")
        # 返回父目录（tmp_path）
        assert result == tmp_path


class TestWorkspaceFromProject:
    """测试从项目根目录检测工作区"""

    def test_detect_pyproject_toml(self, tmp_path: Path) -> None:
        """检测包含 pyproject.toml 的项目"""
        project_root = tmp_path / "my_project"
        project_root.mkdir()
        (project_root / "pyproject.toml").write_text("[project]\n")

        result = detect_workspace_from_project(project_root)
        assert result == project_root

    def test_detect_git_directory(self, tmp_path: Path) -> None:
        """检测包含 .git 目录的项目"""
        project_root = tmp_path / "my_project"
        project_root.mkdir()
        (project_root / ".git").mkdir()

        result = detect_workspace_from_project(project_root)
        assert result == project_root

    def test_traverse_up_to_find_project(self, tmp_path: Path) -> None:
        """向上遍历查找项目根目录"""
        project_root = tmp_path / "my_project"
        project_root.mkdir()
        (project_root / "pyproject.toml").write_text("[project]\n")

        # 从子目录开始
        subdir = project_root / "src" / "mypackage"
        subdir.mkdir(parents=True)

        result = detect_workspace_from_project(subdir)
        assert result == project_root

    def test_no_project_marker_returns_cwd(self, tmp_path: Path) -> None:
        """没有项目标记时返回当前目录"""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        result = detect_workspace_from_project(empty_dir)
        assert result == empty_dir


class TestResolveWorkspace:
    """测试工作区解析优先级"""

    def test_user_specified_has_highest_priority(self, tmp_path: Path) -> None:
        """用户指定的工作区优先级最高"""
        user_workspace = tmp_path / "custom"
        user_workspace.mkdir()

        file_path = tmp_path / "other_project" / "test.py"
        file_path.parent.mkdir()

        result = resolve_workspace(
            user_specified=user_workspace,
            opened_file=file_path,
        )

        assert result == user_workspace

    def test_opened_file_second_priority(self, tmp_path: Path) -> None:
        """打开的文件路径优先级第二"""
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("[project]\n")

        file_path = project_dir / "src" / "test.py"
        file_path.parent.mkdir()

        result = resolve_workspace(opened_file=file_path)

        # 应该返回文件所在目录（src），而不是项目根目录
        # Claude Code 会以文件所在目录为工作区
        assert result == file_path.parent

    def test_project_root_third_priority(self, tmp_path: Path) -> None:
        """项目根目录优先级第三"""
        # 使用临时目录作为 cwd
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("[project]\n")

        # 从项目内的一个子目录开始
        subdir = project_dir / "src"
        subdir.mkdir()

        # Mock cwd 为子目录
        with patch("mokioclaw.core.workspace_detection.Path.cwd", return_value=subdir):
            result = resolve_workspace()

            # 应该找到项目根目录
            assert result == project_dir

    def test_cwd_fallback(self, tmp_path: Path) -> None:
        """当前工作目录作为 fallback"""
        with patch("mokioclaw.core.workspace_detection.Path.cwd", return_value=tmp_path):
            result = resolve_workspace()
            assert result == tmp_path

    def test_creates_directory_if_not_exists(self, tmp_path: Path) -> None:
        """如果工作区不存在，自动创建"""
        new_workspace = tmp_path / "new_workspace"
        assert not new_workspace.exists()

        result = resolve_workspace(user_specified=new_workspace)

        assert result.exists()
        assert result == new_workspace


class TestWorkspaceIntegration:
    """集成测试：验证工作区检测的完整流程"""

    def test_claude_code_like_scenario(self, tmp_path: Path) -> None:
        """模拟 Claude Code 场景：用户打开文件"""
        # 创建一个项目结构
        project = tmp_path / "projects" / "myapp"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='myapp'\n")
        (project / "src").mkdir()
        (project / "src" / "main.py").write_text("print('hello')\n")

        # 用户打开了 src/main.py
        opened_file = project / "src" / "main.py"

        # 解析工作区（不提供 cwd mock，使用真实 cwd）
        # 由于我们真实的 cwd 是 d:/development/code/py_code/dot_agent（也是一个项目）
        # 它会检测到真实项目而不是 tmp_path 中的项目
        # 但我们应该验证文件路径解析是否正确
        workspace = detect_workspace_from_file(opened_file)

        # 应该返回文件所在目录（src）
        assert workspace == project / "src"

    def test_no_file_no_project_uses_cwd(self, tmp_path: Path) -> None:
        """没有打开文件且不在项目内，使用当前目录"""
        with patch("mokioclaw.core.workspace_detection.Path.cwd", return_value=tmp_path):
            workspace = resolve_workspace()
            assert workspace == tmp_path


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
