"""
Layered settings: project-shared + personal-local settings.json.

Aligns with Claude Code's settings.json / settings.local.json:
- .mokioclaw/settings.json: shared project settings (committed to VCS)
- .mokioclaw/settings.local.json: personal local settings (gitignored)
- ~/.mokioclaw/settings.json: global personal settings
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

GLOBAL_SETTINGS_DIR = Path.home() / ".mokioclaw"
PROJECT_SETTINGS_FILE = "settings.json"
PROJECT_LOCAL_SETTINGS_FILE = "settings.local.json"


def load_settings(workspace: Path | None = None, *, safe_mode: bool = False) -> dict[str, Any]:
    """Load layered settings (global → project → local).

    Args:
        workspace: project workspace path
        safe_mode: if True, skip all custom settings (clean start)

    Returns:
        merged settings dict
    """
    if safe_mode:
        return {}
    merged: dict[str, Any] = {}
    paths: list[Path] = []

    global_path = GLOBAL_SETTINGS_DIR / PROJECT_SETTINGS_FILE
    if global_path.exists():
        paths.append(global_path)

    if workspace is not None:
        project_path = workspace / ".mokioclaw" / PROJECT_SETTINGS_FILE
        if project_path.exists():
            paths.append(project_path)
        local_path = workspace / ".mokioclaw" / PROJECT_LOCAL_SETTINGS_FILE
        if local_path.exists():
            paths.append(local_path)

    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update(data)
        except Exception as exc:
            logger.debug("settings load skipped (%s): %s", path, exc)

    return merged


def save_project_settings(workspace: Path, settings: dict[str, Any], *, local: bool = False) -> None:
    """Save project settings to settings.json or settings.local.json."""
    path = workspace / ".mokioclaw" / (PROJECT_LOCAL_SETTINGS_FILE if local else PROJECT_SETTINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def settings_sources(workspace: Path | None = None) -> list[str]:
    """List which settings files were found."""
    sources: list[str] = []
    global_path = GLOBAL_SETTINGS_DIR / PROJECT_SETTINGS_FILE
    if global_path.exists():
        sources.append(f"global:{global_path}")
    if workspace is not None:
        for name in (PROJECT_SETTINGS_FILE, PROJECT_LOCAL_SETTINGS_FILE):
            path = workspace / ".mokioclaw" / name
            if path.exists():
                sources.append(f"project:{path}")
    return sources
