from __future__ import annotations

from pathlib import Path

from mokioclaw.memory.auto_memory import (
    auto_memory_summary,
    extract_preferences,
    learn_from_message,
    load_auto_memory,
)


def test_extract_preferences_finds_always(tmp_path: Path) -> None:
    result = extract_preferences("always use type hints in Python code")
    assert len(result) >= 1
    assert result[0]["kind"] == "preference"
    assert "type hints" in result[0]["text"]


def test_extract_preferences_finds_never(tmp_path: Path) -> None:
    result = extract_preferences("never commit directly to main branch")
    assert len(result) >= 1
    assert result[0]["kind"] == "avoid"


def test_learn_persists_and_deduplicates(tmp_path: Path) -> None:
    new1 = learn_from_message(tmp_path, "always use async for IO operations")
    assert len(new1) == 1

    new2 = learn_from_message(tmp_path, "always use async for IO operations")
    assert len(new2) == 0

    entries = load_auto_memory(tmp_path)
    assert len(entries) == 1


def test_auto_memory_summary_returns_readable(tmp_path: Path) -> None:
    learn_from_message(tmp_path, "always use type hints")
    summary = auto_memory_summary(tmp_path)
    assert "Learned Preferences" in summary
    assert "type hints" in summary


def test_auto_memory_empty_summary(tmp_path: Path) -> None:
    assert auto_memory_summary(tmp_path) == ""
