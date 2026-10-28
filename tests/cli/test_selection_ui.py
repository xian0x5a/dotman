from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import dotman.cli_interaction as cli
from dotman import cli_style
import pytest
from dotman.cli import main
from dotman.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    ScriptedInteraction,
    TerminalInteraction,
    TextInputRequest,
)
from dotman.models import UiConfig, UiMenusConfig
from dotman.resolver import resolve_match
from dotman.terminal import ESCAPE_INPUT
from dotman.ui_context import ui_config_scope

from tests.helpers import EXAMPLE_REPO, write_manager_config, write_named_manager_config_with_state_keys



def test_render_tracked_binding_label_uses_selection_menu_style() -> None:
    assert cli_style.render_full_spec_selector_label(repo_name="example", selector="git", profile="basic", use_color=True) == (
        "\033[2;34mexample\033[0m"
        "\033[2m:\033[0m"
        "\033[1mgit\033[0m"
        "\033[2m@basic\033[0m"
    )

def test_render_package_label_can_prioritize_package_name() -> None:
    assert cli.render_package_label(
        repo_name="example",
        package_id="git",
        package_first=True,
        include_repo_context=True,
    ) == "example:git"

def test_render_package_target_label_uses_dot_separator_and_target_style(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert cli.render_package_target_label(repo_name="example", package_id="git", target_name="gitconfig") == (
        "\033[2;34mexample\033[0m"
        "\033[2m:\033[0m"
        "\033[1mgit\033[0m"
        "\033[2m.\033[0m"
        "\033[2;33mgitconfig\033[0m"
    )


def test_render_package_target_label_renders_package_instance_targets() -> None:
    assert cli.render_package_target_label(
        repo_name="example",
        package_id="profiled",
        bound_profile="work",
        target_name="managed",
    ) == "example:profiled<work>.managed"


def test_resolve_match_ranks_leftmost_selector_segments_first() -> None:
    interaction = ScriptedInteraction(choices=["sandbox:sunshine"])

    selected = resolve_match(
        interaction,
        exact_matches=[],
        partial_matches=[
            ("sandbox", "host/linux-meta"),
            ("sandbox", "sunshine"),
        ],
        query_text="s",
        exact_header_text="unused",
        partial_header_text="Select a selector match for 's':",
        option_resolver=lambda match: cli.ResolverOption(
            display_label=f"{match[0]}:{match[1]}",
            match_fields=cli.build_selector_match_fields(repo_name=match[0], selector=match[1]),
        ),
        value_resolver=lambda match: f"{match[0]}:{match[1]}",
        not_found_text="unused",
    )

    assert selected == ("sandbox", "sunshine")
    [request] = interaction.requests
    assert isinstance(request, ChoiceRequest)
    assert [option.label for option in request.options] == ["sandbox:sunshine", "sandbox:host/linux-meta"]

def test_resolve_match_routes_unique_partial_through_menu() -> None:
    # A unique partial match is a one-item menu, not a yes/no confirmation.
    interaction = ScriptedInteraction(choices=["beta"])

    selected = resolve_match(
        interaction,
        exact_matches=[],
        partial_matches=["beta"],
        query_text="bet",
        exact_header_text="unused",
        partial_header_text="Select a repo for local overrides:",
        option_resolver=lambda match: cli.ResolverOption(
            display_label=match,
            match_fields=(match,),
            field_kinds=("repo",),
        ),
        value_resolver=lambda match: match,
        not_found_text="unused",
    )

    assert selected == "beta"
    assert interaction.requests == [
        ChoiceRequest(
            header_text="Select a repo for local overrides:",
            options=(ChoiceOption(value="beta", label="beta", display_fields=("beta",)),),
        )
    ]


def test_run_diff_review_menu_prints_separator_before_each_diff_for_all(
    monkeypatch,
    capsys,
) -> None:
    review_items = [
        cli.ReviewItem(
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
        ),
        cli.ReviewItem(
            selection_label="example:zsh@basic",
            package_id="zsh",
            target_name="zshrc",
            action="update",
            operation="push",
            repo_path=Path("/repo/.zshrc"),
            live_path=Path("/live/.zshrc"),
            source_path="/repo/.zshrc",
            destination_path="/live/.zshrc",
            before_bytes=b"before\n",
            after_bytes=b"after\n",
        ),
    ]
    interaction = ScriptedInteraction(text_inputs=["a", "s"])
    inspected: list[str] = []

    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: inspected.append(item.target_name))

    assert cli.run_diff_review_menu(review_items, interaction=interaction, operation="push") is True

    output = capsys.readouterr().out
    assert inspected == ["gitconfig", "zshrc"]
    assert "----- Diff 1/2: example:git.gitconfig [update] -----" in output
    assert "file: /live/gitconfig" in output
    assert "----- End Diff 1/2 -----" in output
    assert "----- Diff 2/2: example:zsh.zshrc [update] -----" in output
    assert "file: /live/.zshrc" in output
    assert "----- End Diff 2/2 -----" in output

def test_run_diff_review_menu_prints_footer_after_single_inspect(
    monkeypatch,
    capsys,
) -> None:
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
    interaction = ScriptedInteraction(text_inputs=["1", "s"])

    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: None)

    assert cli.run_diff_review_menu([review_item], interaction=interaction, operation="push") is True

    output = capsys.readouterr().out
    assert "----- Diff 1/1: example:git.gitconfig [update] -----" in output
    assert "file: /live/gitconfig" in output
    assert "----- End Diff 1/1 -----" in output


def test_run_diff_review_menu_list_command_reprints_menu(
    monkeypatch,
    capsys,
) -> None:
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
    interaction = ScriptedInteraction(text_inputs=["list", "s"])


    assert cli.run_diff_review_menu([review_item], interaction=interaction, operation="push") is True

    output = capsys.readouterr().out
    assert output.count("Review pending diffs for push:") == 2
    assert output.count("  1) [update] example:git.gitconfig: /repo/gitconfig -> /live/gitconfig") == 2


def test_print_review_diff_header_dims_metadata_prefix_when_colored(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

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


    cli.print_review_diff_header(review_item, index=1, total=1)

    output = capsys.readouterr().out
    assert "\033[2m-----\033[0m \033[2mDiff 1/1:\033[0m " in output
    assert "\033[2;34mexample\033[0m\033[2m:\033[0m\033[1mgit\033[0m\033[2m.\033[0m\033[2;33mgitconfig\033[0m" in output
    assert "\033[1;36m[update]\033[0m" in output
    assert "\033[2mfile: /live/gitconfig\033[0m" in output


def test_run_diff_review_menu_default_command_views_next_diff(
    monkeypatch,
    capsys,
) -> None:
    review_items = [
        cli.ReviewItem(
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
        ),
        cli.ReviewItem(
            selection_label="example:zsh@basic",
            package_id="zsh",
            target_name="zshrc",
            action="update",
            operation="push",
            repo_path=Path("/repo/.zshrc"),
            live_path=Path("/live/.zshrc"),
            source_path="/repo/.zshrc",
            destination_path="/live/.zshrc",
            before_bytes=b"before\n",
            after_bytes=b"after\n",
        ),
    ]
    interaction = ScriptedInteraction(text_inputs=["", "", "s"])
    inspected: list[str] = []

    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: inspected.append(item.target_name))

    assert cli.run_diff_review_menu(review_items, interaction=interaction, operation="push") is True

    assert inspected == ["gitconfig", "zshrc"]
    output = capsys.readouterr().out
    assert "----- Diff 1/2: example:git.gitconfig [update] -----" in output
    assert "----- Diff 2/2: example:zsh.zshrc [update] -----" in output


def test_run_diff_review_menu_next_command_uses_last_viewed_file(
    monkeypatch,
    capsys,
) -> None:
    review_items = [
        cli.ReviewItem(
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
        ),
        cli.ReviewItem(
            selection_label="example:zsh@basic",
            package_id="zsh",
            target_name="zshrc",
            action="update",
            operation="push",
            repo_path=Path("/repo/.zshrc"),
            live_path=Path("/live/.zshrc"),
            source_path="/repo/.zshrc",
            destination_path="/live/.zshrc",
            before_bytes=b"before\n",
            after_bytes=b"after\n",
        ),
        cli.ReviewItem(
            selection_label="example:nvim@basic",
            package_id="nvim",
            target_name="init_lua",
            action="update",
            operation="push",
            repo_path=Path("/repo/init.lua"),
            live_path=Path("/live/init.lua"),
            source_path="/repo/init.lua",
            destination_path="/live/init.lua",
            before_bytes=b"before\n",
            after_bytes=b"after\n",
        ),
    ]
    interaction = ScriptedInteraction(text_inputs=["2", "n", "s"])
    inspected: list[str] = []

    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: inspected.append(item.target_name))

    assert cli.run_diff_review_menu(review_items, interaction=interaction, operation="push") is True

    assert inspected == ["zshrc", "init_lua"]
    output = capsys.readouterr().out
    assert "----- Diff 2/3: example:zsh.zshrc [update] -----" in output
    assert "----- Diff 3/3: example:nvim.init_lua [update] -----" in output


def test_run_diff_review_menu_next_command_at_end_prompts_for_continue(monkeypatch) -> None:
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
    interaction = ScriptedInteraction(text_inputs=["", ""], confirmations=[True])
    inspected: list[str] = []

    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: inspected.append(item.target_name))

    assert cli.run_diff_review_menu([review_item], interaction=interaction, operation="push") is True

    assert inspected == ["gitconfig"]
    review_command_request = TextInputRequest(
        prompt='\nReview command ("?", number, "n", "a", "l", "s", Esc; default: next): ',
        escape_result=ESCAPE_INPUT,
    )
    assert interaction.requests == [
        review_command_request,
        review_command_request,
        ConfirmationRequest(prompt="Continue? [Y/n] ", default=True),
    ]


def test_run_diff_review_menu_aborts_on_escape() -> None:
    review_item = cli.ReviewItem(
        selection_label="example:git@basic",
        package_id="git",
        target_name="gitconfig",
        action="update",
        operation="restore",
        repo_path=Path("/repo/gitconfig"),
        live_path=Path("/live/gitconfig"),
        source_path="/repo/gitconfig",
        destination_path="/live/gitconfig",
        before_bytes=b"before\n",
        after_bytes=b"after\n",
    )

    assert cli.run_diff_review_menu(
        [review_item],
        interaction=ScriptedInteraction(text_inputs=[ESCAPE_INPUT]),
        operation="restore",
    ) is False


def test_review_menu_header_prepends_blank_line(capsys) -> None:
    cli.print_review_menu_items([], operation="pull")

    assert capsys.readouterr().out == "\nReview pending diffs for pull:\n"

def test_review_menu_prompt_prepends_blank_line() -> None:
    assert cli.review_menu_prompt() == '\nReview command ("?", number, "n", "a", "l", "s", Esc; default: next): '


def test_parse_review_command_aborts_on_escape() -> None:
    assert cli.parse_review_command("\x1b", 1) == ("abort", None)


def test_parse_review_command_rejects_q() -> None:
    with pytest.raises(ValueError, match="unsupported review command: q"):
        cli.parse_review_command("q", 1)


@pytest.mark.parametrize("command", ["l", "list"])
def test_parse_review_command_lists_review_items(command: str) -> None:
    assert cli.parse_review_command(command, 1) == ("list", None)


@pytest.mark.parametrize("command", ["s", "skip"])
def test_parse_review_command_skips_remaining_review(command: str) -> None:
    assert cli.parse_review_command(command, 1) == ("skip_review", None)


def test_parse_review_command_rejects_legacy_continue_shortcut() -> None:
    with pytest.raises(ValueError, match="unsupported review command: c"):
        cli.parse_review_command("c", 1)


@pytest.mark.parametrize(
    ("env_value", "ui_config", "bottom_up"),
    [
        pytest.param(None, None, True, id="default-bottom-up"),
        pytest.param("0", None, False, id="env-disables"),
        pytest.param(None, UiConfig(menus=UiMenusConfig(bottom_up=False)), False, id="manager-config-disables"),
    ],
)
def test_terminal_menu_order_follows_bottom_up_setting(
    monkeypatch,
    env_value: str | None,
    ui_config: UiConfig | None,
    bottom_up: bool,
) -> None:
    if env_value is None:
        monkeypatch.delenv("DOTMAN_MENU_BOTTOM_UP", raising=False)
    else:
        monkeypatch.setenv("DOTMAN_MENU_BOTTOM_UP", env_value)
    output_stream = StringIO()
    interaction = TerminalInteraction(
        input_stream=StringIO("\n"),
        output_stream=output_stream,
        fzf_available=lambda: False,
        use_color=False,
    )
    request = ChoiceRequest(
        header_text="Select a profile:",
        options=tuple(ChoiceOption(value=label, label=label) for label in ("basic", "work", "host/linux")),
    )

    if ui_config is None:
        selected = interaction.choose(request)
    else:
        with ui_config_scope(ui_config):
            selected = interaction.choose(request)

    output = output_stream.getvalue()
    positions = [output.index("  1) basic"), output.index("  2) work"), output.index("  3) host/linux")]
    assert selected == "basic"
    assert positions == sorted(positions, reverse=bottom_up)


def test_print_review_item_compacts_long_paths(capsys) -> None:
    review_item = cli.ReviewItem(
        selection_label="example:git@basic",
        package_id="git",
        target_name="gitconfig",
        action="update",
        operation="push",
        repo_path=Path.home() / ".config" / "git" / "config",
        live_path=Path.home() / ".local" / "share" / "git" / "config",
        source_path=str(Path.home() / ".config" / "git" / "config"),
        destination_path=str(Path.home() / ".local" / "share" / "git" / "config"),
        before_bytes=b"before\n",
        after_bytes=b"after\n",
    )


    cli.print_review_item(1, review_item)

    output = capsys.readouterr().out
    assert "  1) [update] example:git.gitconfig:" in output
    assert "[diff]" not in output
    assert "~/.../git/config -> ~/.../git/config" in output
    assert str(Path.home()) not in output


def test_print_review_item_shows_unavailable_badge(capsys) -> None:
    review_item = cli.ReviewItem(
        selection_label="example:git@basic",
        package_id="git",
        target_name="gitconfig",
        action="update",
        operation="push",
        repo_path=Path.home() / ".config" / "git" / "config",
        live_path=Path.home() / ".local" / "share" / "git" / "config",
        source_path=str(Path.home() / ".config" / "git" / "config"),
        destination_path=str(Path.home() / ".local" / "share" / "git" / "config"),
        before_bytes=b"before\n",
        after_bytes=None,
        diff_unavailable_reason="diff preview is unavailable",
    )


    cli.print_review_item(1, review_item)

    output = capsys.readouterr().out
    assert "[diff unavailable]" in output
    assert "~/.../git/config -> ~/.../git/config" in output


def test_print_review_item_preserves_root_prefix_for_system_paths(capsys) -> None:
    review_item = cli.ReviewItem(
        selection_label="main:sddm@basic",
        package_id="sddm",
        target_name="kde_settings.conf",
        action="delete",
        operation="push",
        repo_path=Path("/etc/sddm.conf.d/kde_settings.conf"),
        live_path=Path("/repo/sddm.conf.d/kde_settings.conf"),
        source_path="/etc/sddm.conf.d/kde_settings.conf",
        destination_path="/repo/sddm.conf.d/kde_settings.conf",
        before_bytes=b"before\n",
        after_bytes=b"after\n",
    )


    cli.print_review_item(1, review_item)

    output = capsys.readouterr().out
    assert ": /etc/sddm.conf.d/kde_settings.conf -> /repo/sddm.conf.d/kde_settings.conf" in output


@pytest.mark.parametrize(
    ("command", "profile_suffix"),
    [
        pytest.param(["info", "trackable"], "", id="info-trackable"),
        pytest.param(["track"], "@basic", id="track"),
    ],
)
def test_single_partial_match_error_suggests_a_name_the_command_accepts(
    tmp_path: Path,
    capsys,
    command: list[str],
    profile_suffix: str,
) -> None:
    # Without a prompt, a unique partial match fails with a suggested exact
    # name; pasting that suggestion back must resolve.
    config_path = write_manager_config(tmp_path)
    query = "core-cli"

    assert main(["--config", str(config_path), "--no-confirm", *command, f"{query}{profile_suffix}"]) != 0
    error_output = capsys.readouterr().err
    suggestions = [quoted for quoted in re.findall(r"'([^']+)'", error_output) if quoted != query]
    assert suggestions, error_output

    exit_code = main(["--config", str(config_path), "--no-confirm", *command, f"{suggestions[-1]}{profile_suffix}"])

    assert exit_code == 0, capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "query"),
    [
        pytest.param(["untrack"], "gi", id="untrack"),
        pytest.param(["sync", "--dry-run"], "gitconf", id="sync"),
    ],
)
def test_single_partial_tracked_match_error_suggests_a_name_the_command_accepts(
    tmp_path: Path,
    capsys,
    command: list[str],
    query: str,
) -> None:
    # Same paste-back contract for queries over tracked state. The state key
    # badges tracked entry labels, and neither badges nor kinds belong in a name.
    config_path = write_named_manager_config_with_state_keys(
        tmp_path, {"example": EXAMPLE_REPO}, state_keys={"example": "laptop"}
    )
    for binding in ("example:git@basic", "example:nvim@basic"):
        assert main(["--config", str(config_path), "--no-confirm", "track", binding]) == 0
    capsys.readouterr()

    assert main(["--config", str(config_path), "--no-confirm", *command, query]) != 0
    error_output = capsys.readouterr().err
    suggestions = [quoted for quoted in re.findall(r"'([^']+)'", error_output) if quoted != query]
    assert suggestions, error_output

    exit_code = main(["--config", str(config_path), "--no-confirm", *command, suggestions[-1]])

    assert exit_code == 0, capsys.readouterr().err
