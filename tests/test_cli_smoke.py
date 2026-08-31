"""CLI smoke tests for the dot agent entry point."""
from __future__ import annotations

from typer.testing import CliRunner

from dot.coding.cli.app import app

runner = CliRunner()


def test_cli_help_available() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "interactive" in result.output.lower()
    assert "run" in result.output


def test_cli_tui_help() -> None:
    result = runner.invoke(app, ["tui", "--help"])
    assert result.exit_code == 0
    assert "--workspace" in result.output
    assert "--mode" in result.output


def test_cli_run_help() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "task" in result.output


def test_cli_console_help() -> None:
    result = runner.invoke(app, ["console", "--help"])
    assert result.exit_code == 0
