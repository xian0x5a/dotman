from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from dotman.cli import main
from dotman.interaction import ScriptedInteraction
from dotman.models import FullSpecSelector

from tests.helpers import (
    REFERENCE_REPO,
    write_implicit_conflict_repo,
    write_manager_config,
    write_named_manager_config,
    write_package_override_preview_repo,
    write_profile_switch_repo,
)


def test_track_cli_emits_state_only_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "--json",
            "track",
            "example:git@basic",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "state-only"
    assert payload["operation"] == "track"
    assert payload["package_entry"]["repo"] == "example"
    assert payload["package_entry"]["package_id"] == "git"
    assert payload["package_entry"]["profile"] == "basic"
    assert (tmp_path / "state" / "dotman" / "repos" / "example" / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            'package_id = "git"',
            'profile = "basic"',
            "",
        ]
    )

def test_track_cli_interactively_selects_profile_when_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction(choices=["basic"])

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "example:git",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert interaction.requests[0].header_text == "Select a profile for example:git:"
    assert "tracked example:git@basic" in output
    assert (tmp_path / "state" / "dotman" / "repos" / "example" / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            'package_id = "git"',
            'profile = "basic"',
            "",
        ]
    )

def test_track_cli_interactively_switches_to_non_conflicting_profile(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "switch-repo"
    write_profile_switch_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "beta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    interaction = ScriptedInteraction(choices=["basic", "work"])

    exit_code = main(
        [
            "--config",
            str(config_path),
            "track",
            "fixture:alpha",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert [request.header_text for request in interaction.requests] == [
        "Select a profile for fixture:alpha:",
        "Select a non-conflicting profile for fixture:alpha@basic:",
    ]
    assert "tracked fixture:alpha@work" in output
    assert (state_dir / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "alpha"',
            'profile = "work"',
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "beta"',
            'profile = "basic"',
            "",
        ]
    )

def test_track_cli_returns_130_on_keyboard_interrupt_during_profile_selection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    class InterruptingInteraction(ScriptedInteraction):
        def choose(self, request):
            raise KeyboardInterrupt

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "example:git",
        ],
        interaction=InterruptingInteraction(),
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "interrupted" in captured.err

def test_track_cli_interactively_selects_repo_for_exact_selector_collision(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction(choices=["beta:sunshine"])

    config_path = write_named_manager_config(
        tmp_path,
        {
            "alpha": REFERENCE_REPO,
            "beta": REFERENCE_REPO,
        },
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "track",
            "sunshine@host/linux",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    request = interaction.requests[0]
    assert request.header_text == "Select a repo for exact selector 'sunshine':"
    assert [option.value for option in request.options] == ["alpha:sunshine", "beta:sunshine"]
    assert "tracked beta:sunshine@host/linux" in output
    assert (tmp_path / "state" / "dotman" / "repos" / "beta" / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "beta"',
            'package_id = "sunshine"',
            'profile = "host/linux"',
            "",
        ]
    )

def test_track_cli_interactively_selects_partial_selector_match(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction(choices=["sandbox:linux/1password"])

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "sandbox:1pass@host/linux",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert interaction.requests[0].header_text == "Select a selector match for '1pass':"
    assert "tracked sandbox:linux/1password@host/linux" in output
    assert (tmp_path / "state" / "dotman" / "repos" / "sandbox" / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "sandbox"',
            'package_id = "linux/1password"',
            'profile = "host/linux"',
            "",
        ]
    )

def test_track_cli_rejects_unique_partial_profile_in_non_interactive_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "--json",
            "track",
            "example:git@wor",
        ]
    )

    assert exit_code == 1
    assert "no exact match for 'wor'; use exact name 'work'" in capsys.readouterr().err


def test_track_cli_selects_unique_partial_profile_in_interactive_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction(choices=["work"])

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "example:git@wor",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    request = interaction.requests[0]
    assert request.header_text == "Select a profile match for 'wor' in example:git:"
    assert [option.value for option in request.options] == ["work"]
    assert "tracked example:git@work" in capsys.readouterr().out

def test_track_cli_interactively_selects_ambiguous_partial_profile_match(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction(choices=["os/mac"])

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "sandbox:1password@os/",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert interaction.requests[0].header_text == "Select a profile match for 'os/' in sandbox:1password:"
    assert "tracked sandbox:1password@os/mac" in output

def test_track_cli_interactively_falls_back_to_full_profile_menu_for_unknown_profile(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction(
        choices=["sandbox:1password", "host/linux"],
    )

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "ss@wor",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert [request.header_text for request in interaction.requests] == [
        "Select a selector match for 'ss':",
        "Select a profile for sandbox:1password:",
    ]
    assert "tracked sandbox:1password@host/linux" in output

def test_track_cli_confirms_before_updating_existing_binding_profile(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction(choices=["work"], confirmations=[True])

    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "example:git",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert interaction.requests[0].header_text == "Select a profile for example:git:"
    assert "Confirm tracked package entry replacement for example:git:" in interaction.requests[1].message
    assert "tracked example:git@work" in output
    assert (tmp_path / "state" / "dotman" / "repos" / "example" / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            'package_id = "git"',
            'profile = "work"',
            "",
        ]
    )

def test_track_cli_keeps_existing_binding_when_profile_replacement_is_declined(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction(confirmations=[False])

    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "example:git@work",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Confirm tracked package entry replacement for example:git:" in interaction.requests[0].message
    assert "kept existing tracked package entry example:git@basic" in output
    assert (tmp_path / "state" / "dotman" / "repos" / "example" / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            'package_id = "git"',
            'profile = "basic"',
            "",
        ]
    )


def test_track_cli_no_confirm_replaces_profile_without_interactive_stdin(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "--no-confirm",
            "track",
            "example:git@work",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Confirm tracked package entry replacement for example:git:" in output
    assert "Confirm replacement?" not in output
    assert "tracked example:git@work" in output


def test_track_cli_json_no_confirm_replaces_profile_without_human_summary(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "--json",
            "--no-confirm",
            "track",
            "example:git@work",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["package_entry"]["package_id"] == "git"
    assert payload["package_entry"]["profile"] == "work"


def test_track_cli_refuses_silent_profile_replacement_in_non_interactive_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "example:git@work",
        ]
    )

    assert exit_code == 1
    assert (
        "refusing to replace tracked package entry 'example:git@basic' with 'example:git@work' in non-interactive mode"
        in capsys.readouterr().err
    )
    assert (tmp_path / "state" / "dotman" / "repos" / "example" / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            'package_id = "git"',
            'profile = "basic"',
            "",
        ]
    )

def test_track_cli_confirms_before_overriding_implicit_targets(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction(confirmations=[True])

    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "core-cli-meta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "example:work/git@work",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Confirm explicit override for example:work/git@work:" in interaction.requests[0].message
    assert "implicit: example:git@basic (git)" in interaction.requests[0].message
    assert "tracked example:work/git@work" in output

def test_track_cli_refuses_implicit_override_in_non_interactive_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "core-cli-meta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "example:work/git@work",
        ]
    )

    assert exit_code == 1
    assert (
        "refusing to let 'example:work/git@work' explicitly override implicitly tracked targets in non-interactive mode"
        in capsys.readouterr().err
    )

def test_track_cli_writes_expanded_package_bindings_for_group_selector(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    state_path = tmp_path / "state" / "dotman" / "repos" / "example" / "tracked-packages.toml"

    exit_code = main(
        [
            "--config",
            str(write_manager_config(tmp_path)),
            "track",
            "example:os/arch@basic",
        ]
    )

    assert exit_code == 0
    assert state_path.read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            'package_id = "core-cli-meta"',
            'profile = "basic"',
            "",
        ]
    )


def test_track_cli_can_promote_conflicting_package_from_implicit_conflict(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "implicit-conflict-repo"
    write_implicit_conflict_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "beta-meta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    interaction = ScriptedInteraction(
        choices=[
            FullSpecSelector(
                repo="fixture",
                selector="alpha",
                selector_kind="package",
                profile="basic",
            )
        ]
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "track",
            "fixture:alpha-stack@basic",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert interaction.requests[0].header_text == (
        "Select a conflicting package to track explicitly alongside fixture:alpha-stack@basic:"
    )
    assert "Confirm explicit override for fixture:alpha@basic:" not in output
    assert "implicit: fixture:beta@basic (beta)" not in output
    # The promotion breaks the tie; the typed request is tracked next to it.
    assert output.index("tracked fixture:alpha@basic") < output.index("tracked fixture:alpha-stack@basic")
    assert (state_dir / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "alpha"',
            'profile = "basic"',
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "alpha-meta"',
            'profile = "basic"',
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "beta-meta"',
            'profile = "basic"',
            "",
        ]
    )


def test_track_cli_promotes_conflicting_package_in_its_own_repo(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Both repos define alpha/beta, so a promotion that kept only the package id
    # and reused the typed repo would silently track first:beta.
    first_root = tmp_path / "first-repo"
    second_root = tmp_path / "second-repo"
    write_implicit_conflict_repo(first_root)
    write_implicit_conflict_repo(second_root)
    # `second` tracks beta-meta under a profile other than the typed `basic`, so a
    # promotion that reused the typed profile would record beta@basic next to it.
    (second_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"first": first_root, "second": second_root})

    first_state = tmp_path / "state" / "dotman" / "repos" / "first" / "tracked-packages.toml"
    second_state = tmp_path / "state" / "dotman" / "repos" / "second" / "tracked-packages.toml"
    second_state.parent.mkdir(parents=True)
    second_state.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "second"',
                'package_id = "beta-meta"',
                'profile = "work"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Promoting second:beta@work overrides the request's own implicit first:alpha@basic.
    interaction = ScriptedInteraction(
        choices=[
            FullSpecSelector(
                repo="second",
                selector="beta",
                selector_kind="package",
                profile="work",
            )
        ],
        confirmations=[True],
    )

    exit_code = main(
        ["--config", str(config_path), "track", "first:alpha-stack@basic"],
        interaction=interaction,
    )

    assert exit_code == 0
    assert [
        (option.value.repo, option.value.selector, option.value.profile)
        for option in interaction.requests[0].options
    ] == [("first", "alpha", "basic"), ("second", "beta", "work")]
    assert "Confirm explicit override for first:alpha-stack@basic:" in interaction.requests[1].message
    output = capsys.readouterr().out
    # Promotion first, then the typed request in its own repo.
    assert output.index("tracked second:beta@work") < output.index("tracked first:alpha-stack@basic")
    assert second_state.read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "second"',
            'package_id = "beta"',
            'profile = "work"',
            "",
            "[[packages]]",
            'repo = "second"',
            'package_id = "beta-meta"',
            'profile = "work"',
            "",
        ]
    )
    assert first_state.read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "first"',
            'package_id = "alpha-meta"',
            'profile = "basic"',
            "",
        ]
    )


def _tracked_state_text(repo: str, entries: list[tuple[str, str]]) -> str:
    lines = ["schema_version = 1", ""]
    for package_id, profile in entries:
        lines.extend(
            [
                "[[packages]]",
                f'repo = "{repo}"',
                f'package_id = "{package_id}"',
                f'profile = "{profile}"',
                "",
            ]
        )
    return "\n".join(lines)


def test_track_cli_declined_promotion_override_leaves_state_untouched(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    first_root = tmp_path / "first-repo"
    second_root = tmp_path / "second-repo"
    write_implicit_conflict_repo(first_root)
    write_implicit_conflict_repo(second_root)
    (second_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"first": first_root, "second": second_root})

    first_state = tmp_path / "state" / "dotman" / "repos" / "first" / "tracked-packages.toml"
    second_state = tmp_path / "state" / "dotman" / "repos" / "second" / "tracked-packages.toml"
    second_state.parent.mkdir(parents=True)
    second_state_text = _tracked_state_text("second", [("beta-meta", "work")])
    second_state.write_text(second_state_text, encoding="utf-8")

    # Promoting first:alpha@basic is accepted (it is picked), but it would explicitly
    # override the implicit second:beta@work, and that confirmation is declined.
    interaction = ScriptedInteraction(
        choices=[
            FullSpecSelector(
                repo="first",
                selector="alpha",
                selector_kind="package",
                profile="basic",
            )
        ],
        confirmations=[False],
    )

    exit_code = main(
        ["--config", str(config_path), "track", "first:alpha-stack@basic"],
        interaction=interaction,
    )

    assert exit_code == 0
    confirmation = interaction.requests[1]
    assert "Confirm explicit override for first:alpha-stack@basic:" in confirmation.message
    assert "new: first:alpha@basic (alpha)" in confirmation.message
    assert "implicit: second:beta@work (beta)" in confirmation.message
    output = capsys.readouterr().out
    # The declined request is the typed one, not the promotion it was held with.
    assert "skipped tracking first:alpha-stack@basic" in output
    assert "tracked " not in output
    assert not first_state.exists()
    assert second_state.read_text(encoding="utf-8") == second_state_text


def _write_package(root: Path, package_id: str, targets: dict[str, str], depends: Sequence[str] = ()) -> None:
    package_dir = root / "packages" / package_id
    (package_dir / "files").mkdir(parents=True)
    lines = [f"depends = {json.dumps(list(depends))}", ""] if depends else []
    for name, path in targets.items():
        (package_dir / "files" / f"{name}.conf").write_text(f"{package_id} {name}\n", encoding="utf-8")
        lines.extend([f"[targets.{name}]", f'source = "files/{name}.conf"', f'path = "{path}"', ""])
    (package_dir / "package.toml").write_text("\n".join(lines), encoding="utf-8")


def _write_basic_profile(root: Path) -> None:
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "basic.toml").write_text("", encoding="utf-8")


def _write_two_path_conflict_repos(tmp_path: Path) -> Path:
    """Write repos where `first:alpha-stack` ties with `second:beta-meta@work` on two paths.

    alpha owns the shared path and delta the other path, both templated by profile, so
    the ties exist at `basic` only. beta owns both paths in `second` and so clashes
    explicitly with alpha as soon as alpha is promoted. State tracks `second:beta-meta@work`.
    """
    first_root = tmp_path / "first-repo"
    second_root = tmp_path / "second-repo"
    for root in (first_root, second_root):
        _write_basic_profile(root)
        (root / "profiles" / "work.toml").write_text("", encoding="utf-8")

    _write_package(first_root, "alpha", {"shared": "~/.config/shared-{{ profile }}.conf"})
    _write_package(first_root, "delta", {"other": "~/.config/other-{{ profile }}.conf"})
    _write_package(first_root, "alpha-meta", {}, depends=["alpha", "delta"])
    (first_root / "groups").mkdir()
    (first_root / "groups" / "alpha-stack.toml").write_text('members = ["alpha-meta"]\n', encoding="utf-8")
    _write_package(
        second_root,
        "beta",
        {"shared": "~/.config/shared-basic.conf", "other": "~/.config/other-basic.conf"},
    )
    _write_package(second_root, "beta-meta", {}, depends=["beta"])

    second_state = tmp_path / "state" / "dotman" / "repos" / "second" / "tracked-packages.toml"
    second_state.parent.mkdir(parents=True)
    second_state.write_text(_tracked_state_text("second", [("beta-meta", "work")]), encoding="utf-8")
    return write_named_manager_config(tmp_path, {"first": first_root, "second": second_root})


def test_track_cli_confirms_promotion_overriding_implicit_packages_of_the_request(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_path = _write_two_path_conflict_repos(tmp_path)
    first_state = tmp_path / "state" / "dotman" / "repos" / "first" / "tracked-packages.toml"
    second_state = tmp_path / "state" / "dotman" / "repos" / "second" / "tracked-packages.toml"
    second_state_text = second_state.read_text(encoding="utf-8")

    interaction = ScriptedInteraction(
        choices=[FullSpecSelector(repo="second", selector="beta", selector_kind="package", profile="work")],
        confirmations=[False],
    )

    exit_code = main(
        ["--config", str(config_path), "track", "first:alpha-stack@basic"],
        interaction=interaction,
    )

    assert exit_code == 0
    # Promoting beta settles the shared path, but beta also takes over the other path
    # from delta, which the user never resolved; the confirmation must list both.
    message = interaction.requests[1].message
    assert "Confirm explicit override for first:alpha-stack@basic:" in message
    assert "new: second:beta@work (beta)" in message
    assert "implicit: first:alpha@basic (alpha)" in message
    assert "implicit: first:delta@basic (delta)" in message
    assert capsys.readouterr().out == "skipped tracking first:alpha-stack@basic\n"
    assert not first_state.exists()
    assert second_state.read_text(encoding="utf-8") == second_state_text


def test_track_cli_offers_only_promotions_that_fit_the_held_ones(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_path = _write_two_path_conflict_repos(tmp_path)

    # Promoting alpha settles the shared path. The other path still ties delta with
    # beta, but promoting beta would clash explicitly with alpha, so only delta is left
    # and it is asked as a single confirmation instead of a menu.
    interaction = ScriptedInteraction(
        choices=[FullSpecSelector(repo="first", selector="alpha", selector_kind="package", profile="basic")],
        confirmations=[True, True],
    )

    exit_code = main(
        ["--config", str(config_path), "track", "first:alpha-stack@basic"],
        interaction=interaction,
    )

    assert exit_code == 0
    menu, promotion, override = interaction.requests
    assert [option.label for option in menu.options] == ["first:alpha@basic", "second:beta@work"]
    assert promotion.prompt == "Confirm promotion? [y/n] "
    assert "promote:   first:delta@basic" in promotion.message
    assert "tracking it explicitly, alongside the request, breaks the implicit tie." in promotion.message
    assert "Confirm explicit override for first:alpha-stack@basic:" in override.message
    assert capsys.readouterr().out.splitlines() == [
        "tracked first:alpha@basic",
        "tracked first:delta@basic",
        "tracked first:alpha-stack@basic",
    ]
    first_state = tmp_path / "state" / "dotman" / "repos" / "first" / "tracked-packages.toml"
    assert first_state.read_text(encoding="utf-8") == _tracked_state_text(
        "first",
        [("alpha", "basic"), ("alpha-meta", "basic"), ("delta", "basic")],
    )


def test_track_cli_never_offers_a_promotion_that_clashes_explicitly_at_a_later_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    first_root = tmp_path / "first-repo"
    second_root = tmp_path / "second-repo"
    for root in (first_root, second_root):
        _write_basic_profile(root)
    shared, eps_path, z_path = "~/.config/shared.conf", "~/.config/e.conf", "~/.config/z.conf"
    _write_package(first_root, "eps", {"e": eps_path})
    _write_package(first_root, "alpha", {"shared": shared})
    _write_package(first_root, "alpha-meta", {}, depends=["eps", "alpha"])
    (first_root / "groups").mkdir()
    (first_root / "groups" / "alpha-stack.toml").write_text('members = ["alpha-meta"]\n', encoding="utf-8")
    _write_package(second_root, "beta", {"shared": shared})
    _write_package(second_root, "phi", {"e": eps_path, "z": z_path})
    _write_package(second_root, "zeta", {"z": z_path})
    _write_package(second_root, "beta-meta", {}, depends=["beta", "phi"])
    second_state = tmp_path / "state" / "dotman" / "repos" / "second" / "tracked-packages.toml"
    second_state.parent.mkdir(parents=True)
    second_state.write_text(
        _tracked_state_text("second", [("beta-meta", "basic"), ("zeta", "basic")]),
        encoding="utf-8",
    )
    config_path = write_named_manager_config(tmp_path, {"first": first_root, "second": second_root})
    first_state = tmp_path / "state" / "dotman" / "repos" / "first" / "tracked-packages.toml"
    second_state_text = second_state.read_text(encoding="utf-8")

    # Promoting phi settles the e.conf tie but clashes explicitly with the tracked zeta
    # at z.conf, so only eps is left and it is asked as a single confirmation.
    interaction = ScriptedInteraction(confirmations=[False])
    exit_code = main(["--config", str(config_path), "track", "first:alpha-stack@basic"], interaction=interaction)

    assert exit_code == 2
    [promotion] = interaction.requests
    assert promotion.prompt == "Confirm promotion? [y/n] "
    assert "promote:   first:eps@basic" in promotion.message
    assert not first_state.exists()
    assert second_state.read_text(encoding="utf-8") == second_state_text


def test_track_cli_names_the_explicit_clash_hidden_behind_an_implicit_tie(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    first_root = tmp_path / "first-repo"
    second_root = tmp_path / "second-repo"
    for root in (first_root, second_root):
        _write_basic_profile(root)
    shared, z_path = "~/.config/shared.conf", "~/.config/z.conf"
    # The request ties implicitly with beta at shared.conf and clashes explicitly
    # with the explicitly tracked zeta at z.conf, which is found later.
    _write_package(first_root, "alpha", {"shared": shared})
    _write_package(first_root, "gamma", {"z": z_path})
    _write_package(first_root, "alpha-meta", {}, depends=["alpha"])
    (first_root / "groups").mkdir()
    # gamma is a direct member, so it is explicit and clashes with the explicit zeta.
    (first_root / "groups" / "alpha-stack.toml").write_text('members = ["alpha-meta", "gamma"]\n', encoding="utf-8")
    _write_package(second_root, "beta", {"shared": shared})
    _write_package(second_root, "zeta", {"z": z_path})
    _write_package(second_root, "beta-meta", {}, depends=["beta"])
    second_state = tmp_path / "state" / "dotman" / "repos" / "second" / "tracked-packages.toml"
    second_state.parent.mkdir(parents=True)
    second_state_text = _tracked_state_text("second", [("beta-meta", "basic"), ("zeta", "basic")])
    second_state.write_text(second_state_text, encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"first": first_root, "second": second_root})
    first_state = tmp_path / "state" / "dotman" / "repos" / "first" / "tracked-packages.toml"

    interaction = ScriptedInteraction()
    exit_code = main(["--config", str(config_path), "track", "first:alpha-stack@basic"], interaction=interaction)

    assert exit_code == 2
    assert interaction.requests == []
    error = capsys.readouterr().err
    assert "conflicting explicit tracked targets for" in error
    assert "z.conf" in error
    assert not first_state.exists()
    assert second_state.read_text(encoding="utf-8") == second_state_text


def test_track_cli_drops_held_promotions_when_switching_profile(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_path = _write_two_path_conflict_repos(tmp_path)

    # alpha is promoted for the shared path, then the promotion for the other path is
    # declined and the user switches to `work`, where nothing ties any more. The alpha
    # promotion only settled the tie at `basic`, so it must not outlive the switch.
    interaction = ScriptedInteraction(
        choices=[
            FullSpecSelector(repo="first", selector="alpha", selector_kind="package", profile="basic"),
            "work",
        ],
        confirmations=[False],
    )

    exit_code = main(
        ["--config", str(config_path), "track", "first:alpha-stack@basic"],
        interaction=interaction,
    )

    assert exit_code == 0
    assert interaction.requests[2].header_text == "Select a non-conflicting profile for first:alpha-stack@basic:"
    assert capsys.readouterr().out == "tracked first:alpha-stack@work\n"
    first_state = tmp_path / "state" / "dotman" / "repos" / "first" / "tracked-packages.toml"
    assert first_state.read_text(encoding="utf-8") == _tracked_state_text("first", [("alpha-meta", "work")])


def test_track_cli_asks_for_replacement_once_when_promoting(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "implicit-conflict-repo"
    write_implicit_conflict_repo(repo_root)
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    state_path = tmp_path / "state" / "dotman" / "repos" / "fixture" / "tracked-packages.toml"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        _tracked_state_text("fixture", [("alpha-meta", "work"), ("beta-meta", "basic")]),
        encoding="utf-8",
    )

    # Switching alpha-meta from work to basic needs a replacement confirmation, and
    # then ties with beta-meta@basic. The typed binding is unchanged by the promotion,
    # so the replacement is not asked again; a second ask would exhaust the script.
    interaction = ScriptedInteraction(
        choices=[
            FullSpecSelector(
                repo="fixture",
                selector="alpha",
                selector_kind="package",
                profile="basic",
            )
        ],
        confirmations=[True],
    )

    exit_code = main(
        ["--config", str(config_path), "track", "fixture:alpha-meta@basic"],
        interaction=interaction,
    )

    assert exit_code == 0
    replacement_requests = [
        request
        for request in interaction.requests
        if "Confirm tracked package entry replacement" in getattr(request, "message", "")
    ]
    assert len(replacement_requests) == 1
    assert capsys.readouterr().out.splitlines() == [
        "tracked fixture:alpha@basic",
        "tracked fixture:alpha-meta@basic",
    ]
    assert state_path.read_text(encoding="utf-8") == _tracked_state_text(
        "fixture",
        [("alpha", "basic"), ("alpha-meta", "basic"), ("beta-meta", "basic")],
    )


def test_track_cli_skips_same_profile_package_override_prompt(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "override-preview-repo"
    write_package_override_preview_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "beta-meta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "track",
            "fixture:alpha@basic",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Confirm explicit override for fixture:alpha@basic:" not in output
    assert "implicit: fixture:beta@basic (beta)" not in output
    assert "~/.config/shared.conf" not in output
    assert "~/.config/extra.conf" not in output


@pytest.mark.parametrize("profile", ['work"laptop', "lab\\tab"])
def test_track_cli_round_trips_profile_names_with_toml_special_characters(
    tmp_path: Path,
    capsys,
    profile: str,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / f"{profile}.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "app").mkdir(parents=True)
    (repo_root / "packages" / "app" / "package.toml").write_text('', encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})

    assert main(["--config", str(config_path), "--no-confirm", "track", f"main:app@{profile}"]) == 0
    capsys.readouterr()

    exit_code = main(["--config", str(config_path), "--json", "list", "tracked"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [
        entry["profile"] for package in payload["packages"] for entry in package["package_entries"]
    ] == [profile]
