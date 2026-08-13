"""Plugin marketplace + slash command suggestion tests"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mokioclaw.interaction.commands import (
    dispatch_slash_command,
    filter_command_suggestions,
    list_available_commands,
)
from mokioclaw.plugins.loader import (
    discover_plugin_skills,
    list_plugin_command_names,
    load_plugin_command,
)
from mokioclaw.plugins.marketplace import (
    disable_plugin,
    enable_plugin,
    install_plugin,
    list_catalog,
    uninstall_plugin,
)


def test_catalog_includes_builtin_code_review_kit():
    catalog = {p.name: p for p in list_catalog()}
    assert "code-review-kit" in catalog
    assert catalog["code-review-kit"].source == "builtin"


def test_install_enable_disable_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    import mokioclaw.plugins.marketplace as mp

    monkeypatch.setattr(mp, "_GLOBAL_ROOT", home / ".mokioclaw")
    ws = tmp_path / "ws"
    ws.mkdir()

    result = install_plugin("code-review-kit", workspace=ws, scope="user")
    assert result["ok"] is True
    dest = Path(result["path"])
    assert (dest / "plugin.json").exists()
    assert (dest / "commands" / "review-diff.md").exists()

    enabled = json.loads((home / ".mokioclaw" / "plugins.json").read_text(encoding="utf-8"))
    assert "code-review-kit" in enabled["enabled"]

    paths = mp.enabled_plugin_paths(ws)
    assert any(p.name == "code-review-kit" for p in paths)

    cmds = list_plugin_command_names(ws)
    assert "review-diff" in cmds
    body = load_plugin_command("review-diff", ws)
    assert body and "git diff" in body.lower()

    skills = discover_plugin_skills(ws)
    assert any(s.name == "plugin-review" for s in skills)

    disable_plugin("code-review-kit", workspace=ws)
    assert mp.enabled_plugin_paths(ws) == []

    enable_plugin("code-review-kit", workspace=ws)
    assert mp.enabled_plugin_paths(ws)


def test_plugin_slash_command_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    import mokioclaw.plugins.marketplace as mp

    monkeypatch.setattr(mp, "_GLOBAL_ROOT", home / ".mokioclaw")
    ws = tmp_path / "ws"
    ws.mkdir()
    assert install_plugin("code-review-kit", workspace=ws)["ok"]

    listed = dispatch_slash_command("/plugin list", workspace=ws)
    assert listed.handled
    assert "code-review-kit" in listed.ui_message

    custom = dispatch_slash_command("/review-diff", workspace=ws)
    assert custom.handled
    assert custom.action == "inject"
    assert "severity" in custom.inject_message.lower() or "diff" in custom.inject_message.lower()


def test_filter_command_suggestions_includes_system_and_prefix():
    names = list_available_commands()
    assert "help" in names
    assert "plugin" in names
    assert "new" in names

    hits = filter_command_suggestions("/he")
    assert "help" in hits
    assert all(h.startswith("he") or "he" in h for h in hits)

    all_hits = filter_command_suggestions("/")
    assert "plugin" in all_hits
    assert len(all_hits) <= 12


def test_uninstall_and_info_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    import mokioclaw.plugins.marketplace as mp

    monkeypatch.setattr(mp, "_GLOBAL_ROOT", home / ".mokioclaw")
    ws = tmp_path / "ws"
    ws.mkdir()

    assert install_plugin("code-review-kit", workspace=ws, scope="user")["ok"]
    dest = home / ".mokioclaw" / "plugins" / "code-review-kit"
    assert dest.exists()
    assert any(p.name == "code-review-kit" for p in mp.list_installed(ws))

    # uninstall 应删除目录并清理 enabled 标记
    result = uninstall_plugin("code-review-kit", workspace=ws)
    assert result["ok"] is True
    assert not dest.exists()
    assert mp.enabled_plugin_paths(ws) == []

    # info 命令：catalog 仍含 builtin
    info = dispatch_slash_command("/plugin info code-review-kit", workspace=ws)
    assert info.handled
    assert "code-review-kit" in info.ui_message
    assert "builtin" in info.ui_message

    # uninstall 不存在的插件 → 失败但不抛
    miss = uninstall_plugin("does-not-exist", workspace=ws)
    assert miss["ok"] is False


def test_uninstall_plugin_project_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """project-scope 安装的插件也应能被卸载"""
    home = tmp_path / "home"
    home.mkdir()
    import mokioclaw.plugins.marketplace as mp

    monkeypatch.setattr(mp, "_GLOBAL_ROOT", home / ".mokioclaw")
    ws = tmp_path / "ws"
    ws.mkdir()

    assert install_plugin("code-review-kit", workspace=ws, scope="project")["ok"]
    project_dest = ws / ".mokioclaw" / "plugins" / "code-review-kit"
    assert project_dest.exists()
    assert any(p.name == "code-review-kit" for p in mp.enabled_plugin_paths(ws))

    result = uninstall_plugin("code-review-kit", workspace=ws)
    assert result["ok"] is True
    assert not project_dest.exists()
    assert mp.enabled_plugin_paths(ws) == []


def test_plugin_name_rejects_path_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """恶意插件名不能越界删除/写入 plugins 目录之外"""
    home = tmp_path / "home"
    home.mkdir()
    import mokioclaw.plugins.marketplace as mp

    monkeypatch.setattr(mp, "_GLOBAL_ROOT", home / ".mokioclaw")
    ws = tmp_path / "ws"
    ws.mkdir()

    # 在父目录放一个诱饵，确认不会被删
    bait = tmp_path / "bait-target"
    bait.mkdir()
    bait_file = bait / "important.txt"
    bait_file.write_text("keep me", encoding="utf-8")

    for bad in ("../../bait-target", "..", "../bait-target", "/etc", "a/b"):
        # install / uninstall 都应拒绝非法名，且不动诱饵
        assert install_plugin(bad, workspace=ws).get("ok") is False
        assert uninstall_plugin(bad, workspace=ws).get("ok") is False

    assert bait_file.exists()
    # plugins 目录下不应出现诱饵的影子
    assert not (home / ".mokioclaw" / "plugins" / "bait-target").exists()


def test_plugin_subcommand_flag_filtering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """带 flag 的子命令参数应正确提取插件名"""
    home = tmp_path / "home"
    home.mkdir()
    import mokioclaw.plugins.marketplace as mp

    monkeypatch.setattr(mp, "_GLOBAL_ROOT", home / ".mokioclaw")
    ws = tmp_path / "ws"
    ws.mkdir()
    assert install_plugin("code-review-kit", workspace=ws)["ok"]

    info = dispatch_slash_command("/plugin info --project code-review-kit", workspace=ws)
    assert info.handled
    assert "code-review-kit" in info.ui_message
    assert "Unknown plugin" not in info.ui_message


def test_slash_suggestion_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """filter_command_suggestions 三类边界：空前缀、精确命中、含空格"""
    # 纯 / → 返回命令全集（上限 12）
    all_hits = filter_command_suggestions("/")
    assert "help" in all_hits and "plugin" in all_hits

    # 精确命中 help → help 仍在结果中（补全面板层在 TUI 处理，这里只验过滤器）
    exact = filter_command_suggestions("/help")
    assert "help" in exact

    # 含空格的输入不应进入前缀过滤（TUI 层会关闭面板）
    spaced = filter_command_suggestions("he ")
    assert spaced == []


def test_plugin_catalog_in_prompt_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """已启用插件的 skill 应进入 system 动态区 skill 目录"""
    home = tmp_path / "home"
    home.mkdir()
    import mokioclaw.plugins.marketplace as mp

    monkeypatch.setattr(mp, "_GLOBAL_ROOT", home / ".mokioclaw")
    ws = tmp_path / "ws"
    ws.mkdir()
    assert install_plugin("code-review-kit", workspace=ws, scope="user")["ok"]

    from mokioclaw.prompts.builder import PromptBuilder

    builder = PromptBuilder(workspace=ws)
    catalog = builder._load_skill_catalog()
    assert "plugin-review" in catalog
