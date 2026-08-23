"""Tests for console helpers and keybindings metadata."""
from __future__ import annotations

from dot.cli import console
from dot.cli.keybindings import SHORTCUTS, SHORTCUTS_HELP, textual_bindings


# ============================================================
# console._parse_mode
# ============================================================

def test_parse_mode_valid() -> None:
    assert console._parse_mode("/mode plan", "auto") == "plan"
    assert console._parse_mode("/mode edit", "auto") == "edit"
    assert console._parse_mode("/mode auto", "plan") == "auto"


def test_parse_mode_invalid_returns_none() -> None:
    assert console._parse_mode("/mode bogus", "auto") is None
    assert console._parse_mode("/mode", "auto") is None


# ============================================================
# console._safe_chunk
# ============================================================

def test_safe_chunk_passes_through_primitives() -> None:
    chunk = {"a": 1, "b": "x", "c": True, "d": None}
    assert console._safe_chunk(chunk) == chunk


def test_safe_chunk_stringifies_non_serializable() -> None:
    class Obj:
        def __str__(self) -> str:
            return "obj-str"

    chunk = {"node": {"value": Obj(), "keep": 1}}
    out = console._safe_chunk(chunk)
    assert out["node"]["value"] == "obj-str"
    assert out["node"]["keep"] == 1


# ============================================================
# keybindings
# ============================================================

def test_shortcuts_are_unique_keys() -> None:
    keys = [s.key for s in SHORTCUTS]
    assert len(keys) == len(set(keys))


def test_textual_bindings_excludes_up_down() -> None:
    bindings = textual_bindings()
    keys = [b[0] for b in bindings]
    assert "up" not in keys
    assert "down" not in keys
    assert "tab" in keys
    assert "ctrl+c" in keys


def test_textual_bindings_shape() -> None:
    for key, action, desc in textual_bindings():
        assert isinstance(key, str) and key
        assert isinstance(action, str) and action
        assert isinstance(desc, str) and desc


def test_shortcuts_help_contains_all_labels() -> None:
    for s in SHORTCUTS:
        assert s.label in SHORTCUTS_HELP
