from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import dotman.cli_interaction as cli
import pytest
from dotman.cli import build_parser, main
from dotman.models import FullSpecSelector, HookPlan, TargetPlan

from tests.helpers import (
    EXAMPLE_REPO,
    REFERENCE_REPO,
    capture_parser_help,
    write_implicit_conflict_repo,
    write_manager_config,
    write_multi_instance_repo,
    write_named_manager_config,
    write_package_override_preview_repo,
    write_profile_switch_repo,
    write_untrack_conflict_repo,
)


def test_track_help_uses_explicit_selector_profile_placeholder(capsys) -> None:
    output = capture_parser_help(capsys, "track")
    assert "usage: dotman track [-h] [<repo>:]<selector>[@<profile>]" in output
    assert "positional arguments:" in output
    assert "[<repo>:]<selector>[@<profile>]" in output


def test_search_help_uses_explicit_query_placeholder(capsys) -> None:
    output = capture_parser_help(capsys, "search")
    assert "usage: dotman search [-h] <query>" in output
    assert "Search packages and groups across configured repos" in output
    assert "<query>" in output

def test_push_help_lists_dry_run_and_full_path_flags(capsys) -> None:
    output = capture_parser_help(capsys, "push")
    assert "usage: dotman push [-h] [-d] [--full-path] [--report] [--no-deps] [--run-noop]" in output
    assert "[<repo:package.target> ...]" in output
    assert "-d, --dry-run" in output
    assert "--full-path" in output


def test_pull_help_lists_dry_run_and_full_path_flags(capsys) -> None:
    output = capture_parser_help(capsys, "pull")
    assert "usage: dotman pull [-h] [-d] [--full-path] [--report] [--commit] [--no-deps]" in output
    assert "[<repo:package.target> ...]" in output
    assert "-d, --dry-run" in output
    assert "--full-path" in output

def test_add_help_lists_yes_flag(capsys) -> None:
    output = capture_parser_help(capsys, "add")
    assert "usage: dotman add [-h] <live-path> [<package-query>]" in output


def test_restore_help_lists_yes_flag(capsys) -> None:
    output = capture_parser_help(capsys, "restore")
    assert "usage: dotman restore [-h] [-d] [--full-path] [<snapshot>]" in output


def test_removed_snapshot_command_is_rejected_instead_of_aliased(capsys) -> None:
    removed_command = "roll" + "back"

    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args([removed_command])

    assert exc_info.value.code == 2
    error_output = capsys.readouterr().err
    assert f"invalid choice: '{removed_command}'" in error_output
    assert "restore" in error_output


def test_top_level_help_uses_command_placeholder_and_summaries(capsys) -> None:
    output = capture_parser_help(capsys)
    assert "usage: dotman [-h] [--version] [--config <config-path>] [--json]" in output
    assert "[--file-symlink-mode <mode>]" in output
    assert "[--dir-symlink-mode <mode>]" in output
    assert "<command>" in output
    assert "commands:" in output
    assert "Track packages in manager state" in output
    assert "Search packages and groups" in output
    assert "Diagnose manager config and tracked package state" in output
    assert "Patch review content back into repo source" in output
    assert "Open a tracked package, target, local override, config" in output
    assert "repo path" in output
    assert "Re-run a reconcile helper subcommand" in output
    assert "Render built-in template helpers" in output
    assert "Run a standalone structure-agnostic text rewrite" in output
    assert "Run a standalone structured transform" in output


def test_edit_help_lists_package_and_target_subcommands(capsys) -> None:
    output = capture_parser_help(capsys, "edit")
    assert "usage: dotman edit [-h] <edit-command> ..." in output
    assert "edit commands:" in output
    assert "package" in output
    assert "Open a tracked package directory in an editor" in output
    assert "target" in output
    assert "Open a tracked target repo path in an editor" in output
    assert "local" in output
    assert "Open a per-repo local override file in an editor" in output
    assert "config" in output
    assert "Open dotman manager config in an editor" in output
    assert "repo" in output
    assert "Open a configured repo root in an editor" in output


def test_edit_local_help_uses_optional_repo_placeholder(capsys) -> None:
    output = capture_parser_help(capsys, "edit", "local")
    assert "usage: dotman edit local [-h] [<repo>]" in output
    assert "--help" in output
    assert "<repo>" in output


def test_edit_config_help_has_no_extra_arguments(capsys) -> None:
    output = capture_parser_help(capsys, "edit", "config")
    assert "usage: dotman edit config [-h]" in output
    assert "--help" in output


def test_edit_package_help_uses_explicit_package_placeholder(capsys) -> None:
    output = capture_parser_help(capsys, "edit", "package")
    assert "usage: dotman edit package [-h] <package>" in output
    assert "--help" in output
    assert "<package>" in output


def test_edit_target_help_uses_explicit_target_placeholder(capsys) -> None:
    output = capture_parser_help(capsys, "edit", "target")
    assert "usage: dotman edit target [-h] <target>" in output
    assert "--help" in output
    assert "<target>" in output


def test_edit_repo_help_uses_explicit_repo_placeholder(capsys) -> None:
    output = capture_parser_help(capsys, "edit", "repo")
    assert "usage: dotman edit repo [-h] <repo>" in output
    assert "--help" in output
    assert "<repo>" in output

def test_info_help_uses_nested_command_placeholder_and_summaries(capsys) -> None:
    output = capture_parser_help(capsys, "info")
    assert "usage: dotman info [-h] <info-command> ..." in output
    assert "info commands:" in output
    assert "Show tracked package details" in output
    assert "Show resolved variable details" in output
    assert "installed" not in output
    assert "==SUPPRESS==" not in output


def test_info_var_help_uses_explicit_variable_placeholder(capsys) -> None:
    output = capture_parser_help(capsys, "info", "var")
    assert "usage: dotman info var [-h] <var>" in output
    assert "Show resolved variable details" in output

def test_list_help_shows_tracked_state_and_catalog_subcommands(capsys) -> None:
    output = capture_parser_help(capsys, "list")
    assert "usage: dotman list [-h] <list-command> ..." in output
    assert "list commands:" in output
    assert "List tracked packages" in output
    assert "List configured repos" in output
    assert "List trackable packages and groups" in output
    assert "List resolved variables" in output
    assert "installed" not in output
    assert "==SUPPRESS==" not in output


@pytest.mark.parametrize(
    ("args", "rejected_token"),
    [
        (("list", "installed"), "installed"),
        (("info", "installed", "example:git"), "installed"),
    ],
)
def test_removed_installed_subcommands_are_rejected(capsys, args: tuple[str, ...], rejected_token: str) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(list(args))

    assert exc_info.value.code == 2
    assert f"invalid choice: '{rejected_token}'" in capsys.readouterr().err

def test_reconcile_editor_help_uses_explicit_option_placeholders(capsys) -> None:
    output = capture_parser_help(capsys, "reconcile", "editor")
    assert "--repo-path <repo-path>" in output
    assert "--live-path <live-path>" in output
    assert "--review-repo-path <review-repo-path>" in output
    assert "--review-live-path <review-live-path>" in output
    assert "--additional-source <source-path>" in output
    assert "--editor <editor-command>" in output


def test_capture_help_lists_patch_shortcut(capsys) -> None:
    output = capture_parser_help(capsys, "capture")
    assert "usage: dotman capture [-h] <capture-command> ..." in output
    assert "capture commands:" in output
    assert "patch" in output
    assert "Patch review content back into repo source" in output


def test_capture_patch_help_uses_explicit_option_placeholders(capsys) -> None:
    output = capture_parser_help(capsys, "capture", "patch")
    assert "usage: dotman capture patch [-h] --repo-path <repo-path>" in output
    assert "--render <render-command>" in output
    assert "--review-repo-path <review-repo-path>" in output
    assert "--review-live-path <review-live-path>" in output
    assert "--profile <profile>" in output
    assert "--os <os>" in output
    assert "--cpu-arch <cpu-arch>" in output
    assert "--var <key=value>" in output


def test_reconcile_help_lists_jinja_shortcut(capsys) -> None:
    output = capture_parser_help(capsys, "reconcile")
    assert "usage: dotman reconcile [-h] <reconcile-command> ..." in output
    assert "reconcile commands:" in output
    assert "editor" in output
    assert "Open repo and live files in an editor" in output
    assert "jinja" in output
    assert "Reconcile a Jinja source with its recursive template" in output



def test_reconcile_jinja_help_uses_explicit_option_placeholders(capsys) -> None:
    output = capture_parser_help(capsys, "reconcile", "jinja")
    assert "usage: dotman reconcile jinja [-h] --repo-path <repo-path>" in output
    assert "--live-path <live-path>" in output
    assert "--review-repo-path <review-repo-path>" in output
    assert "--review-live-path <review-live-path>" in output
    assert "--editor <editor-command>" in output


def test_render_help_uses_nested_command_placeholder_and_summaries(capsys) -> None:
    output = capture_parser_help(capsys, "render")
    assert "usage: dotman render [-h] <render-command> ..." in output
    assert "render commands:" in output
    assert "Render a file with the built-in Jinja renderer" in output


def test_render_jinja_help_uses_explicit_placeholders(capsys) -> None:
    output = capture_parser_help(capsys, "render", "jinja")
    assert "usage: dotman render jinja [-h] [--profile <profile>] [--os <os>]" in output
    assert "[--var <key=value>]" in output
    assert "<source-path>" in output
    assert "--profile <profile>" in output
    assert "--os <os>" in output
    assert "--cpu-arch <cpu-arch>" in output
    assert "--var <key=value>" in output


def test_rewrite_help_lists_home_rewrite(capsys) -> None:
    output = capture_parser_help(capsys, "rewrite")
    assert "usage: dotman rewrite [-h] <rewrite> ..." in output
    assert "rewrites:" in output
    assert "Rewrite active home path fragments" in output


def test_rewrite_home_help_lists_expand_and_collapse(capsys) -> None:
    output = capture_parser_help(capsys, "rewrite", "home")
    assert "usage: dotman rewrite home [-h] <action> ..." in output
    assert "actions:" in output
    assert "Expand standalone '~' fragments to the active $HOME" in output
    assert "Collapse active $HOME fragments to '~'" in output


@pytest.mark.parametrize("action", ["expand", "collapse"])
def test_rewrite_home_action_help_is_stdout_only(capsys, action: str) -> None:
    output = capture_parser_help(capsys, "rewrite", "home", action)
    assert f"usage: dotman rewrite home {action} [-h] [<input>]" in output
    assert "omit or use '-' to read stdin" in output
    assert "write to stdout" in output
    assert "--stdout" not in output
    assert "output" not in output.lower()

def test_selection_prompt_mentions_help() -> None:
    assert cli.selection_prompt() == 'Select a number ("?"; default: 1): '

def test_select_menu_option_shows_help_then_accepts_selection(monkeypatch, capsys) -> None:
    prompts = iter(["?", "2"])

    monkeypatch.setattr(cli, "prompt", lambda _message: next(prompts))

    selected_index = cli.select_menu_option(
        header_text="Select a profile:",
        option_labels=["basic", "work"],
    )

    output = capsys.readouterr().out
    assert selected_index == 1
    assert "Selection help:" in output
    assert "  <number>  choose that item" in output
    assert "Enter" not in output

def test_run_diff_review_menu_shows_help_then_skips_review(monkeypatch, capsys) -> None:
    review_item = cli.ReviewItem(
        selection_label="example:git@basic",
        package_id="git",
        target_name="gitconfig",
        action="update",
        operation="push",
        repo_path=Path("/repo/gitconfig"),
        live_path=Path("/live/gitconfig"),
        source_path="/repo/gitconfig",
        destination_path="/live/gitconfig",
        before_bytes=b"before\n",
        after_bytes=b"after\n",
    )
    prompts = iter(["?", "s"])

    monkeypatch.setattr(cli, "prompt", lambda _message, **_options: next(prompts))

    assert cli.run_diff_review_menu([review_item], operation="push") is True

    output = capsys.readouterr().out
    assert "Review commands:" in output
    assert "  n          inspect next diff" in output
    assert "  a          inspect all diffs" in output
    assert "  l, list    list review items" in output
    assert "  s, skip    skip remaining review" in output
    assert "  Esc        abort" in output
    assert "  ?          show this help" in output


def test_run_diff_review_menu_uses_full_paths_when_requested(monkeypatch, capsys) -> None:
    review_item = cli.ReviewItem(
        selection_label="example:git@basic",
        package_id="git",
        target_name="gitconfig",
        action="update",
        operation="push",
        repo_path=Path("/repo/very/long/path/gitconfig"),
        live_path=Path("/live/very/long/path/gitconfig"),
        source_path="/repo/very/long/path/gitconfig",
        destination_path="/live/very/long/path/gitconfig",
        before_bytes=b"before\n",
        after_bytes=b"after\n",
    )
    prompts = iter(["s"])

    monkeypatch.setattr(cli, "prompt", lambda _message, **_options: next(prompts))

    assert cli.run_diff_review_menu([review_item], operation="push", full_paths=True) is True

    output = capsys.readouterr().out
    assert "/repo/very/long/path/gitconfig -> /live/very/long/path/gitconfig" in output
    assert "repo/.../path/gitconfig" not in output

@pytest.mark.parametrize("command", ["apply", "upgrade", "import", "remove"])
def test_legacy_top_level_cli_commands_are_not_available(command: str) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([command, "example:nvim@basic"])

    assert exc_info.value.code == 2


def test_version_flag_prints_package_version(capsys) -> None:
    from importlib.metadata import version

    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"dotman {version('dotman')}\n"
