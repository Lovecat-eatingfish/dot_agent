"""
Auto-memory: automatically learn and persist user preferences from conversation.

Aligns with Claude Code's ability to auto-save preferences to ~/.claude/projects/<project>/memory/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger
from mokioclaw.core.utils import truncate, utc_now

logger = get_logger(__name__)

AUTO_MEMORY_FILE = "auto_memory.json"

_PREFERENCE_PATTERNS = [
    (re.compile(r"(?:always|please always|make sure to)\s+(.{10,120})", re.I), "preference"),
    (re.compile(r"(?:never|don't|do not)\s+(.{10,120})", re.I), "avoid"),
    (re.compile(r"(?:I prefer|prefer)\s+(.{5,100})", re.I), "preference"),
    (re.compile(r"(?:remember that|note that)\s+(.{10,150})", re.I), "note"),
]


def auto_memory_path(workspace: Path) -> Path:
    return workspace / ".mokioclaw" / AUTO_MEMORY_FILE


def extract_preferences(user_message: str) -> list[dict[str, str]]:
    """Extract candidate preferences from a user message."""
    found: list[dict[str, str]] = []
    for pattern, kind in _PREFERENCE_PATTERNS:
        for match in pattern.finditer(user_message):
            text = match.group(1).strip().rstrip(".")
            if text and len(text) >= 5:
                found.append({"kind": kind, "text": truncate(text, 200), "extracted_at": utc_now()})
    return found


def load_auto_memory(workspace: Path) -> list[dict[str, str]]:
    path = auto_memory_path(workspace)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_auto_memory(workspace: Path, entries: list[dict[str, str]]) -> None:
    path = auto_memory_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def learn_from_message(workspace: Path, user_message: str) -> list[dict[str, str]]:
    """Extract and persist new preferences from a user message.

    Returns newly added entries (deduplicated against existing).
    """
    candidates = extract_preferences(user_message)
    if not candidates:
        return []
    existing = load_auto_memory(workspace)
    existing_texts = {e.get("text", "").lower() for e in existing}
    new_entries: list[dict[str, str]] = []
    for candidate in candidates:
        if candidate["text"].lower() not in existing_texts:
            existing.append(candidate)
            new_entries.append(candidate)
            existing_texts.add(candidate["text"].lower())
    if new_entries:
        save_auto_memory(workspace, existing)
        logger.info("auto-memory learned %d new preference(s)", len(new_entries))
    return new_entries


def auto_memory_summary(workspace: Path) -> str:
    """Return a readable summary of learned preferences for prompt injection."""
    entries = load_auto_memory(workspace)
    if not entries:
        return ""
    lines = ["# Learned Preferences (auto-memory)", ""]
    for entry in entries:
        lines.append(f"- [{entry.get('kind', 'note')}] {entry.get('text', '')}")
    return "\n".join(lines)
