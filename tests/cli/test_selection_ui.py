from __future__ import annotations

import re
from pathlib import Path

import dotman.cli_interaction as cli
import pytest
from dotman.cli import main
from dotman.command_runtime import ArgvCommand, CommandResult, MemoryCommandRuntime
from dotman.models import UiConfig, UiMenusConfig
from dotman.ui_context import ui_config_scope

from tests.helpers import write_manager_config



def test_render_tracked_binding_label_uses_selection_menu_style(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert cli.render_full_spec_selector_label(repo_name="example", selector="git", profile="basic") == (
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

def test_render_full_spec_selector_label_can_prioritize_selector_name() -> None:
    assert cli.render_full_spec_selector_label(
        repo_name="example",
        selector="git",
        profile="basic",
        selector_first=True,
    ) == "example:git@basic"


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


def test_select_menu_option_prefers_fzf_for_long_lists(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_should_use_fzf_for_selection", lambda _option_labels: True)
    monkeypatch.setattr(cli, "_fzf_available", lambda: True)
    monkeypatch.setattr(cli, "_select_menu_option_with_prompt", lambda **_kwargs: pytest.fail("prompt fallback should not run"))
    monkeypatch.setattr(cli, "_select_menu_option_with_fzf", lambda **_kwargs: 1)

    assert cli.select_menu_option(header_text="Select a package:", option_labels=["alpha", "beta"]) == 1

def test_resolve_candidate_match_ranks_leftmost_selector_segments_first(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_select_menu_option(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "select_menu_option", fake_select_menu_option)

    selected = cli.resolve_candidate_match(
        exact_matches=[],
        partial_matches=[
            ("sandbox", "host/linux-meta"),
            ("sandbox", "sunshine"),
        ],
        query_text="s",
        interactive=True,
        exact_header_text="unused",
        partial_header_text="Select a selector match for 's':",
        option_resolver=lambda match: cli.ResolverOption(
            display_label=f"{match[0]}:{match[1]}",
            match_fields=cli.build_selector_match_fields(repo_name=match[0], selector=match[1]),
        ),
        exact_name_resolver=lambda match: f"{match[0]}:{match[1]}",
        exact_error_text="unused",
        partial_error_text="unused",
        not_found_text="unused",
    )

    assert selected == ("sandbox", "sunshine")
    assert captured["option_labels"] == ["sandbox:sunshine", "sandbox:host/linux-meta"]

def test_resolve_candidate_match_routes_unique_partial_through_menu_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_select_menu_option(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "select_menu_option", fake_select_menu_option)
    monkeypatch.setattr(
        cli,
        "prompt",
        lambda _message: (_ for _ in ()).throw(AssertionError("expected resolver menu, not partial confirmation")),
    )

    selected = cli.resolve_candidate_match(
        exact_matches=[],
        partial_matches=["beta"],
        query_text="bet",
        interactive=True,
        exact_header_text="unused",
        partial_header_text="Select a repo for local overrides:",
        option_resolver=lambda match: cli.ResolverOption(
            display_label=match,
            match_fields=(match,),
            field_kinds=("repo",),
        ),
        exact_name_resolver=lambda match: match,
        exact_error_text="unused",
        partial_error_text="unused",
        not_found_text="unused",
    )

    assert selected == "beta"
    assert captured["header_text"] == "Select a repo for local overrides:"
    assert captured["option_labels"] == ["beta"]


def test_select_menu_option_with_prompt_renders_bottom_up_by_default(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "ui_menus_bottom_up_enabled", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda _message: "")

    assert cli._select_menu_option_with_prompt(
        header_text="Select a package:",
        option_labels=["alpha", "beta", "gamma"],
    ) == 0

    output = capsys.readouterr().out
    assert output.index("  3) gamma") < output.index("  2) beta") < output.index("  1) alpha")

def test_select_menu_option_with_fzf_uses_structured_display_fields(monkeypatch) -> None:
    captured: dict[str, object] = {}

    runtime = MemoryCommandRuntime([CommandResult(exit_code=0, stdout=b"2\n")])
    monkeypatch.setattr(cli, "current_command_runtime", lambda: runtime)
    monkeypatch.setattr(cli, "ui_menus_bottom_up_enabled", lambda: True)

    selected_index = cli._select_menu_option_with_fzf(
        header_text="Select a package:",
        option_labels=["sandbox/sunshine [package]", "sandbox/host/linux-meta [group]"],
        option_display_fields=[
            ("sandbox/sunshine", "[package]"),
            ("sandbox/host/linux-meta", "[group]"),
        ],
    )

    request = runtime.requests[0]
    assert isinstance(request.command, ArgvCommand)
    captured["command"] = request.command.arguments
    captured["input"] = request.input.decode("utf-8")
    assert selected_index == 1
    assert "--wrap" in captured["command"]
    assert "--with-nth=2.." in captured["command"]
    assert "--accept-nth=1" in captured["command"]
    assert "--layout=reverse-list" not in captured["command"]
    assert "--layout=reverse" not in captured["command"]
    assert captured["input"] == (
        "1 sandbox/sunshine [package]\n"
        "2 sandbox/host/linux-meta [group]\n"
    )

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
    prompts = iter(["a", "s"])
    inspected: list[str] = []

    monkeypatch.setattr(cli, "prompt", lambda _message, **_options: next(prompts))
    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: inspected.append(item.target_name))

    assert cli.run_diff_review_menu(review_items, operation="push") is True

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
    prompts = iter(["1", "s"])

    monkeypatch.setattr(cli, "prompt", lambda _message, **_options: next(prompts))
    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: None)

    assert cli.run_diff_review_menu([review_item], operation="push") is True

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
    prompts = iter(["list", "s"])

    monkeypatch.setattr(cli, "prompt", lambda _message, **_options: next(prompts))

    assert cli.run_diff_review_menu([review_item], operation="push") is True

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


def test_print_review_diff_header_renders_probe_no_files_as_hint_text(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    review_item = cli.ReviewItem(
        selection_label="sandbox:app@default",
        package_id="app",
        target_name="version",
        action="install",
        operation="push",
        repo_path=Path("/repo/app"),
        live_path=Path("/repo/app"),
        source_path="",
        destination_path="",
        is_probe=True,
    )


    cli.print_review_diff_header(review_item, index=1, total=1)

    output = capsys.readouterr().out
    assert "target: probe" not in output
    assert "\033[2mprobe target: no files\033[0m" in output


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
    prompts = iter(["", "", "s"])
    inspected: list[str] = []

    monkeypatch.setattr(cli, "prompt", lambda _message, **_options: next(prompts))
    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: inspected.append(item.target_name))

    assert cli.run_diff_review_menu(review_items, operation="push") is True

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
    prompts = iter(["2", "n", "s"])
    inspected: list[str] = []

    monkeypatch.setattr(cli, "prompt", lambda _message, **_options: next(prompts))
    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: inspected.append(item.target_name))

    assert cli.run_diff_review_menu(review_items, operation="push") is True

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
    prompt_messages: list[str] = []
    prompts = iter(["", "", ""])
    inspected: list[str] = []

    def fake_prompt(message: str, **_options: object) -> str:
        prompt_messages.append(message)
        return next(prompts)

    monkeypatch.setattr(cli, "prompt", fake_prompt)
    monkeypatch.setattr(cli, "run_review_item_diff", lambda item: inspected.append(item.target_name))

    assert cli.run_diff_review_menu([review_item], operation="push") is True

    assert inspected == ["gitconfig"]
    assert prompt_messages == [
        '\nReview command ("?", number, "n", "a", "l", "s", Esc; default: next): ',
        '\nReview command ("?", number, "n", "a", "l", "s", Esc; default: next): ',
        'Continue? [Y/n] ',
    ]


def test_print_selection_header_prepends_blank_line(capsys) -> None:
    cli.print_selection_header("Review pending diffs for pull:")

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


def test_confirm_review_continue_skips_prompt_when_no_confirm(monkeypatch) -> None:
    monkeypatch.setattr(cli, "prompt", lambda _message: (_ for _ in ()).throw(AssertionError("prompt should not run")))

    assert cli.confirm_review_continue(no_confirm=True) is True


def test_select_menu_option_renders_bottom_up_by_default(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DOTMAN_MENU_BOTTOM_UP", raising=False)
    monkeypatch.setattr(cli, "prompt", lambda _message: "")

    selected_index = cli.select_menu_option(
        header_text="Select a profile:",
        option_labels=["basic", "work", "host/linux"],
    )

    output = capsys.readouterr().out
    assert selected_index == 0
    assert output.index("  3) host/linux") < output.index("  2) work") < output.index("  1) basic")

def test_select_menu_option_can_disable_bottom_up_with_env(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DOTMAN_MENU_BOTTOM_UP", "0")
    monkeypatch.setattr(cli, "prompt", lambda _message: "")

    selected_index = cli.select_menu_option(
        header_text="Select a profile:",
        option_labels=["basic", "work", "host/linux"],
    )

    output = capsys.readouterr().out
    assert selected_index == 0
    assert output.index("  1) basic") < output.index("  2) work") < output.index("  3) host/linux")


def test_select_menu_option_uses_manager_bottom_up_default(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DOTMAN_MENU_BOTTOM_UP", raising=False)
    monkeypatch.setattr(cli, "prompt", lambda _message: "")

    with ui_config_scope(UiConfig(menus=UiMenusConfig(bottom_up=False))):
        selected_index = cli.select_menu_option(
            header_text="Select a profile:",
            option_labels=["basic", "work", "host/linux"],
        )

    output = capsys.readouterr().out
    assert selected_index == 0
    assert output.index("  1) basic") < output.index("  2) work") < output.index("  3) host/linux")


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


def test_print_review_item_shows_probe_badge_like_selection_menu(capsys) -> None:
    review_item = cli.ReviewItem(
        selection_label="sandbox:app@default",
        package_id="app",
        target_name="version",
        action="install",
        operation="push",
        repo_path=Path("/repo/app"),
        live_path=Path("/repo/app"),
        source_path="",
        destination_path="",
        is_probe=True,
        hook_command_summaries=("pre_push: echo target pre",),
    )


    cli.print_review_item(1, review_item)

    assert capsys.readouterr().out == "   1) [install] sandbox:app.version [probe]\n"


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
