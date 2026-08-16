from __future__ import annotations

import json
from pathlib import Path

from mokioclaw.config.settings import (
    load_settings,
    save_project_settings,
    settings_sources,
)


def test_load_settings_safe_mode_returns_empty(tmp_path: Path) -> None:
    settings = load_settings(tmp_path, safe_mode=True)
    assert settings == {}


def test_save_and_load_project_settings(tmp_path: Path) -> None:
    save_project_settings(tmp_path, {"model": "gpt-4o", "max_attempts": 5})
    settings = load_settings(tmp_path)
    assert settings["model"] == "gpt-4o"
    assert settings["max_attempts"] == 5


def test_local_overrides_project(tmp_path: Path) -> None:
    save_project_settings(tmp_path, {"model": "gpt-4o"})
    save_project_settings(tmp_path, {"model": "claude-sonnet"}, local=True)
    settings = load_settings(tmp_path)
    assert settings["model"] == "claude-sonnet"


def test_settings_sources_lists_files(tmp_path: Path) -> None:
    save_project_settings(tmp_path, {"a": 1})
    save_project_settings(tmp_path, {"b": 2}, local=True)
    sources = settings_sources(tmp_path)
    assert any("settings.json" in s for s in sources)
    assert any("settings.local.json" in s for s in sources)
