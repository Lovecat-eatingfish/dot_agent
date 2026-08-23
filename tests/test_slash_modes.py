"""Slash command and run-mode unit tests."""
from __future__ import annotations

from dot.cli import slash
from dot.cli.modes import cycle_mode, is_valid_mode, normalize_mode


# ============================================================
# slash 解析
# ============================================================

def test_is_slash_input() -> None:
    assert slash.is_slash_input("/help")
    assert slash.is_slash_input("  /clear")
    assert not slash.is_slash_input("hello")
    assert not slash.is_slash_input("")


def test_parse_slash_command() -> None:
    assert slash.parse("/help") == ("help", "")
    assert slash.parse("/mode code") == ("mode", "code")
    assert slash.parse("/save my-session") == ("save", "my-session")
    assert slash.parse("hello") is None
    assert slash.parse("/") is None


def test_parse_is_case_insensitive() -> None:
    name, _ = slash.parse("/HELP")
    assert name == "help"


def test_complete_single_match() -> None:
    matches = slash.complete("/hel")
    assert matches == ["/help "]


def test_complete_multiple_matches() -> None:
    matches = slash.complete("/c")
    assert "/clear" in matches
    assert "/compact" in matches


def test_complete_non_slash_returns_empty() -> None:
    assert slash.complete("hello") == []


def test_execute_unknown_command_returns_error_toast() -> None:
    result = slash.execute(None, "/nope")
    assert result.kind == "toast"
    assert result.level == "error"
    assert "未知命令" in result.text


def test_execute_invalid_input_returns_error() -> None:
    result = slash.execute(None, "not a command")
    assert result.kind == "toast"
    assert result.level == "error"


def test_execute_help_returns_message() -> None:
    result = slash.execute(None, "/help")
    assert result.kind == "message"
    assert "/exit" in result.text


def test_execute_clear_returns_clear_screen() -> None:
    result = slash.execute(None, "/clear")
    assert result.kind == "clear_screen"


def test_execute_exit_returns_quit() -> None:
    result = slash.execute(None, "/exit")
    assert result.kind == "quit"


# ============================================================
# modes
# ============================================================

def test_normalize_mode() -> None:
    assert normalize_mode("agent") == "agent"
    assert normalize_mode("CHAT") == "chat"
    assert normalize_mode("code") == "code"
    assert normalize_mode("a") == "agent"
    assert normalize_mode("c") == "chat"
    assert normalize_mode("co") == "code"
    assert normalize_mode("bogus") == "agent"
    assert normalize_mode("") == "agent"


def test_is_valid_mode() -> None:
    assert is_valid_mode("agent")
    assert is_valid_mode("chat")
    assert is_valid_mode("code")
    assert not is_valid_mode("bogus")


def test_cycle_mode_forward() -> None:
    assert cycle_mode("agent") == "chat"
    assert cycle_mode("chat") == "code"
    assert cycle_mode("code") == "agent"


def test_cycle_mode_backward() -> None:
    assert cycle_mode("code", forward=False) == "chat"
    assert cycle_mode("chat", forward=False) == "agent"
    assert cycle_mode("agent", forward=False) == "code"


def test_cycle_mode_invalid_falls_back() -> None:
    assert cycle_mode("bogus") == "chat"
