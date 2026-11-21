"""Tests for CLI presentation."""

import argparse
import re

import pytest

import cli


def test_top_level_help_lists_commands_alphabetically(capsys):
    commands = [
        "annotate", "backup", "book", "engraving", "generate", "index",
        "media", "metadata", "migration", "search", "shotlist", "silhouette", "subtitles",
        "sync", "tool", "visualizer",
    ]
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as result:
        parser.parse_args(["-h"])

    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "{" + ",".join(commands) + "}" in parser.format_usage()
    assert re.findall(r"^    (\w+)\s", help_text, re.MULTILINE) == commands


def test_all_subcommand_help_lists_commands_alphabetically():
    pending = [cli.build_parser()]

    while pending:
        parser = pending.pop()
        if parser.epilog and parser.epilog.startswith("Subcommands:\n"):
            summary = parser.epilog.split("\n\n", 1)[0]
            summary_commands = re.findall(r"^  (\S+)\s", summary, re.MULTILINE)
            assert summary_commands == sorted(summary_commands), parser.prog
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            commands = sorted(action.choices)
            assert "{" + ",".join(commands) + "}" in parser.format_usage(), parser.prog
            listed_commands = [
                name for name in re.findall(r"^    (\S+)\s", parser.format_help(), re.MULTILINE)
                if name in action.choices
            ]
            assert listed_commands == commands, parser.prog
            pending.extend(action.choices.values())


def test_archive_operation_help_lists_operations_alphabetically(capsys):
    operations = [
        "apply", "initialize", "pilot-run", "pilot-select", "plan",
        "population-verify", "preflight", "protected-baseline", "protected-verify",
        "prune", "resume", "rollback", "status", "verify",
    ]
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as result:
        parser.parse_args(["silhouette", "archive", "-h"])

    assert result.value.code == 0
    assert "{" + ",".join(operations) + "}" in capsys.readouterr().out
    assert parser.parse_args(["silhouette", "archive"]).archive_operation == "plan"