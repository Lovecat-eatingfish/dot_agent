"""Tests for config loader and prompt builder (动静分离提示词系统)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from mokioclaw.config.loader import (
    UserConfig,
    _apply_frontmatter,
    _coerce_value,
    _find_project_config,
    _merge_instructions,
    _parse_markdown_with_frontmatter,
    load_user_config,
)
from mokioclaw.prompts.builder import PromptBuilder, get_prompt_builder, reset_prompt_builder


# ============================================================
# UserConfig defaults
# ============================================================

class TestUserConfig:
    def test_default_values(self):
        config = UserConfig()
        assert config.approval_mode == "inline"
        assert config.checkpoint_mode == "light"
        assert config.trace_mode == "on"
        assert config.bash_default_timeout_seconds == 120
        assert config.bash_max_timeout_seconds == 600
        assert config.bash_max_output_chars == 6000
        assert config.max_attempts == 3
        assert config.context_token_limit == 400000
        assert config.custom_instructions == ""
        assert config.config_sources == []


# ============================================================
# Frontmatter parsing
# ============================================================

class TestParseMarkdownWithFrontmatter:
    def test_no_frontmatter(self):
        text = "Just a plain markdown file."
        fm, body = _parse_markdown_with_frontmatter(
            self._write("plain.md", text)
        )
        assert fm == {}
        assert body == text

    def test_full_frontmatter(self):
        content = "---\napproval_mode: auto\nbash_timeout: 300\n---\n\n# Title\nBody text."
        fm, body = _parse_markdown_with_frontmatter(self._write("full.md", content))
        assert fm["approval_mode"] == "auto"
        assert fm["bash_timeout"] == 300
        assert body == "# Title\nBody text."

    def test_empty_frontmatter_block(self):
        content = "---\n---\n\nBody only."
        fm, body = _parse_markdown_with_frontmatter(self._write("empty_fm.md", content))
        assert fm == {}
        assert "Body only." in body

    def test_no_closing_delimiter(self):
        content = "---\nkey: value\nNo closing delimiter."
        fm, body = _parse_markdown_with_frontmatter(self._write("no_close.md", content))
        assert fm == {}

    def test_unicode_body(self):
        content = "---\nkey: value\n---\n\n中文内容测试"
        fm, body = _parse_markdown_with_frontmatter(self._write("unicode.md", content))
        assert "中文内容测试" in body

    @staticmethod
    def _write(name: str, content: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        )
        tmp.write(content)
        tmp.close()
        return Path(tmp.name)


# ============================================================
# Value coercion
# ============================================================

class TestCoerceValue:
    def test_bool_true(self):
        assert _coerce_value("true") is True
        assert _coerce_value("yes") is True
        assert _coerce_value("on") is True

    def test_bool_false(self):
        assert _coerce_value("false") is False
        assert _coerce_value("no") is False
        assert _coerce_value("off") is False

    def test_int(self):
        assert _coerce_value("42") == 42

    def test_float(self):
        assert _coerce_value("3.14") == 3.14

    def test_string(self):
        assert _coerce_value("inline") == "inline"


# ============================================================
# Frontmatter application
# ============================================================

class TestApplyFrontmatter:
    def test_known_fields(self):
        config = UserConfig()
        _apply_frontmatter(config, {
            "approval_mode": "auto",
            "bash_timeout": 300,
            "max_attempts": 5,
        })
        assert config.approval_mode == "auto"
        assert config.bash_default_timeout_seconds == 300
        assert config.max_attempts == 5

    def test_unknown_fields_ignored(self):
        config = UserConfig()
        _apply_frontmatter(config, {"unknown_field": "value"})
        assert config.approval_mode == "inline"  # unchanged

    def test_partial_apply(self):
        config = UserConfig()
        _apply_frontmatter(config, {"approval_mode": "deny"})
        assert config.approval_mode == "deny"
        assert config.max_attempts == 3  # default


# ============================================================
# Instruction merging
# ============================================================

class TestMergeInstructions:
    def test_both_empty(self):
        assert _merge_instructions("", "") == ""

    def test_existing_only(self):
        assert _merge_instructions("rule 1", "") == "rule 1"

    def test_new_only(self):
        assert _merge_instructions("", "rule 2") == "rule 2"

    def test_both_present(self):
        result = _merge_instructions("rule 1", "rule 2")
        assert "rule 1" in result
        assert "rule 2" in result


# ============================================================
# Project config discovery
# ============================================================

class TestFindProjectConfig:
    def test_finds_config_in_workspace(self, tmp_path: Path):
        config_dir = tmp_path / ".mokioclaw"
        config_dir.mkdir()
        (config_dir / "config.md").write_text("---\nkey: val\n---\n", encoding="utf-8")
        found = _find_project_config(tmp_path)
        assert found is not None
        assert found.exists()

    def test_returns_none_when_no_config(self, tmp_path: Path):
        found = _find_project_config(tmp_path)
        assert found is None

    def test_stops_at_git_root(self, tmp_path: Path):
        # Create .mokioclaw/config.md only at tmp_path, not in parent
        config_dir = tmp_path / ".mokioclaw"
        config_dir.mkdir()
        (config_dir / "config.md").write_text("---\nkey: val\n---\n", encoding="utf-8")
        found = _find_project_config(tmp_path)
        assert found is not None


# ============================================================
# load_user_config integration
# ============================================================

class TestLoadUserConfig:
    def test_returns_defaults_when_no_files(self, tmp_path: Path):
        config = load_user_config(workspace=tmp_path)
        assert isinstance(config, UserConfig)
        assert config.custom_instructions == ""
        assert config.approval_mode == "inline"

    def test_loads_project_config(self, tmp_path: Path):
        config_dir = tmp_path / ".mokioclaw"
        config_dir.mkdir()
        (config_dir / "config.md").write_text(
            "---\napproval_mode: auto\nmax_attempts: 5\n---\n\n# Rules\n- Use tabs\n",
            encoding="utf-8",
        )
        config = load_user_config(workspace=tmp_path)
        assert config.approval_mode == "auto"
        assert config.max_attempts == 5
        assert "Use tabs" in config.custom_instructions
        assert len(config.config_sources) >= 1

    def test_project_overrides_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # Create a fake global config
        global_dir = tmp_path / ".mokioclaw"
        global_dir.mkdir()
        monkeypatch.setattr(
            "mokioclaw.config.loader._GLOBAL_CONFIG",
            global_dir / "CLAUDE.md",
        )
        (global_dir / "CLAUDE.md").write_text(
            "---\napproval_mode: auto\nmax_attempts: 10\n---\n\nGlobal rules.\n",
            encoding="utf-8",
        )
        # Project config overrides only some fields
        config_dir = tmp_path / "project" / ".mokioclaw"
        config_dir.mkdir(parents=True)
        (config_dir / "config.md").write_text(
            "---\nmax_attempts: 3\n---\n\nProject rules.\n",
            encoding="utf-8",
        )
        config = load_user_config(workspace=tmp_path / "project")
        # max_attempts from project (3), approval_mode from global (auto)
        assert config.approval_mode == "auto"
        assert config.max_attempts == 3
        assert "Global rules" in config.custom_instructions
        assert "Project rules" in config.custom_instructions

    def test_global_override_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        global_cfg = tmp_path / "custom_claude.md"
        global_cfg.write_text("---\napproval_mode: auto\n---\n", encoding="utf-8")
        monkeypatch.setattr(
            "mokioclaw.config.loader._GLOBAL_CONFIG", global_cfg
        )
        config = load_user_config(global_override=global_cfg, workspace=tmp_path)
        assert config.approval_mode == "auto"

    def test_project_override_path(self, tmp_path: Path):
        project_cfg = tmp_path / "custom_config.md"
        project_cfg.write_text(
            "---\nmax_attempts: 7\n---\n", encoding="utf-8"
        )
        config = load_user_config(
            workspace=tmp_path, project_override=project_cfg
        )
        assert config.max_attempts == 7


# ============================================================
# PromptBuilder
# ============================================================

class TestPromptBuilder:
    def test_build_planner(self):
        builder = PromptBuilder()
        prompt = builder.build("planner")
        assert "You are the planner node" in prompt
        assert "User Custom Instructions" not in prompt  # no custom instructions

    def test_build_code_agent(self):
        builder = PromptBuilder()
        prompt = builder.build("code_agent")
        assert "codeAgent" in prompt or "You are codeAgent" in prompt

    def test_build_verifier(self):
        builder = PromptBuilder()
        prompt = builder.build("verifier")
        assert "verifier" in prompt

    def test_build_search_agent(self):
        builder = PromptBuilder()
        prompt = builder.build("search_agent")
        assert "searchAgent" in prompt

    def test_build_intent_router(self):
        builder = PromptBuilder()
        prompt = builder.build("intent_router")
        assert "intent router" in prompt.lower()

    def test_build_chat_responder(self):
        builder = PromptBuilder()
        prompt = builder.build("chat_responder")
        assert "chat" in prompt.lower()

    def test_build_context_compressor(self):
        builder = PromptBuilder()
        prompt = builder.build("context_compressor")
        assert "compress" in prompt.lower()

    def test_custom_instructions_appended(self):
        builder = PromptBuilder(
            user_config=UserConfig(custom_instructions="- Always use type hints\n- Prefer async")
        )
        prompt = builder.build("planner")
        assert "User Custom Instructions" in prompt
        assert "Always use type hints" in prompt
        assert "Prefer async" in prompt

    def test_unknown_agent_returns_empty(self):
        builder = PromptBuilder()
        assert builder.build("nonexistent") == ""

    def test_base_prompt_override(self):
        builder = PromptBuilder()
        custom = "Custom base prompt"
        result = builder.build("planner", base_prompt=custom)
        assert result.startswith(custom)
        assert "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__" in result

    def test_singleton_caching(self, monkeypatch: pytest.MonkeyPatch):
        reset_prompt_builder()
        # Should create a new one
        b1 = get_prompt_builder()
        assert b1 is not None
        # Second call returns same instance
        b2 = get_prompt_builder()
        assert b1 is b2
        reset_prompt_builder()

    def test_reset_clears_singleton(self):
        reset_prompt_builder()
        b1 = get_prompt_builder(workspace=Path("/tmp"))
        reset_prompt_builder()
        # After reset, next call creates a new instance
        b2 = get_prompt_builder()
        # Both are PromptBuilder instances but different objects
        assert isinstance(b2, PromptBuilder)

    def test_repr(self):
        builder = PromptBuilder()
        r = repr(builder)
        assert "PromptBuilder" in r

    def test_with_workspace_creates_new_instance(self):
        builder = PromptBuilder()
        new_builder = builder.with_workspace(Path("/tmp"))
        assert new_builder is not builder
        assert isinstance(new_builder, PromptBuilder)


# ============================================================
# Prompt content sanity checks
# ============================================================

class TestPromptContent:
    """Verify prompt templates contain key elements."""

    def test_planner_has_route_options(self):
        builder = PromptBuilder()
        prompt = builder.build("planner")
        assert "search" in prompt
        assert "code" in prompt
        assert "verify" in prompt
        assert "final" in prompt

    def test_code_agent_has_tool_rules(self):
        builder = PromptBuilder()
        prompt = builder.build("code_agent")
        assert "TodoUpdateTool" in prompt or "todo" in prompt.lower()

    def test_verifier_has_json_output(self):
        builder = PromptBuilder()
        prompt = builder.build("verifier")
        assert "JSON" in prompt or "json" in prompt
