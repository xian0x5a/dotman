from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from dotman.cli import build_parser, main
from dotman.interaction import TerminalInteraction
from dotman.ui_context import current_ui_config
from tests.helpers import write_named_manager_config


def test_parser_provides_full_path_default_for_commands_without_the_flag() -> None:
    args = build_parser().parse_args(["list", "tracked"])

    assert args.full_path is None


def test_parser_rejects_removed_forget_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["forget", "example:git"])


def test_configuration_independent_render_ignores_missing_manager_config(
    tmp_path: Path,
    capsys,
) -> None:
    template_path = tmp_path / "template.txt"
    template_path.write_text("standalone\n", encoding="utf-8")

    exit_code = main(
        [
            "--config",
            str(tmp_path / "missing-config.toml"),
            "render",
            "jinja",
            str(template_path),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "standalone\n"


def test_config_only_edit_uses_manager_ui_scope(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    alpha_root = tmp_path / "alpha"
    alpine_root = tmp_path / "alpine"
    alpha_root.mkdir()
    alpine_root.mkdir()
    config_path = write_named_manager_config(
        tmp_path,
        {"alpha": alpha_root, "alpine": alpine_root},
    )
    with config_path.open("a", encoding="utf-8") as config_file:
        config_file.write("[ui.menus]\nbottom_up = false\n")
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    interaction_output = StringIO()
    interaction = TerminalInteraction(
        input_stream=StringIO("2\n"),
        output_stream=interaction_output,
        fzf_available=lambda: False,
        use_color=False,
    )

    exit_code = main(
        ["--config", str(config_path), "edit", "local", "al"],
        interaction=interaction,
    )

    assert exit_code == 0
    rendered_menu = interaction_output.getvalue()
    assert rendered_menu.index("alpha") < rendered_menu.index("alpine")
    assert current_ui_config() is None
    assert "Local override path:" in capsys.readouterr().out


def test_no_confirm_is_global_command_policy() -> None:
    assert build_parser().parse_args(["--no-confirm", "push"]).no_confirm


@pytest.mark.parametrize("command", [["edit", "config"], ["reconcile", "editor", "--repo-path", "repo", "--live-path", "live"]])
def test_no_confirm_rejects_editor_commands_before_opening(command, capsys, monkeypatch) -> None:
    monkeypatch.setattr("dotman.cli_interaction.open_editor_path", lambda *a, **kw: pytest.fail("editor opened"))
    assert main(["--no-confirm", *command]) == 1
    assert "--no-confirm" in capsys.readouterr().err


def test_interaction_required_error_colors_follow_stderr(capsys, monkeypatch) -> None:
    # The error is written to stderr, so `2>log` must stay plain even when stdout is a terminal.
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert main(["--no-confirm", "edit", "config"]) == 1
    assert "\x1b[" not in capsys.readouterr().err


def test_no_confirm_disables_terminal_interaction_scope(monkeypatch) -> None:
    from dotman import cli_interaction, interaction_policy
    monkeypatch.setattr("sys.stdin", StringIO())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with interaction_policy.interaction_scope(no_confirm=True):
        assert not cli_interaction.interactive_mode_enabled(json_output=False)
    assert cli_interaction.interactive_mode_enabled(json_output=False)


@pytest.mark.parametrize("json_output", [False, True])
def test_push_requires_execution_consent_without_terminal(json_output, tmp_path, monkeypatch, capsys) -> None:
    from tests.helpers import write_manager_config
    config = write_manager_config(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert main(["--config", str(config), "track", "example:git@basic"]) == 0
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    flags = ["--json"] if json_output else []
    assert main(["--config", str(config), *flags, "push"]) == 1
    # JSON reports the refusal in the result document; human output on stderr.
    captured = capsys.readouterr()
    assert "Push requires a terminal or explicit --no-confirm." in (captured.out if json_output else captured.err)
    assert not (tmp_path / "home/.gitconfig").exists()


def test_sync_accepts_full_path_display_option() -> None:
    assert build_parser().parse_args(["sync", "--full-path"]).full_path is True
