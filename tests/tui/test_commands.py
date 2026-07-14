"""Slash-command parsing (orchestrator/tui/commands.py)."""

import pytest

from orchestrator.tui.commands import (
    COMMANDS,
    CommandError,
    help_lines,
    parse_input,
)


def test_plain_text_is_a_task_not_a_command():
    assert parse_input("migrate tokens v1 -> v2 and run pytest") is None


def test_leading_whitespace_still_parses_as_command():
    cmd = parse_input("   /help  ")
    assert cmd.name == "help" and cmd.args == []


def test_args_are_split_shell_style():
    cmd = parse_input('/permissions allow run_shell')
    assert cmd.name == "permissions"
    assert cmd.args == ["allow", "run_shell"]


def test_quoted_args_stay_together():
    cmd = parse_input('/workspace "my dir/with spaces"')
    assert cmd.args == ["my dir/with spaces"]


def test_aliases_resolve():
    assert parse_input("/q").name == "quit"
    assert parse_input("/exit").name == "quit"
    assert parse_input("/perms").name == "permissions"


def test_unknown_command_suggests_nearest():
    with pytest.raises(CommandError, match="did you mean /theme"):
        parse_input("/thme")


def test_unknown_command_without_near_match_points_to_help():
    with pytest.raises(CommandError, match="/help"):
        parse_input("/zzzzzz")


def test_bare_slash_is_an_error():
    with pytest.raises(CommandError):
        parse_input("/")


def test_unbalanced_quote_is_a_command_error_not_a_crash():
    with pytest.raises(CommandError, match="could not parse"):
        parse_input('/model "unclosed')


def test_help_lines_cover_every_command():
    lines = help_lines()
    assert len(lines) == len(COMMANDS)
    for name in COMMANDS:
        assert any(f"/{name}" in line for line in lines)
