from __future__ import annotations

import json
from pathlib import Path

from dotman.cli import main
from dotman.interaction import ChoiceRequest, ScriptedInteraction

from tests.helpers import (
    EXAMPLE_REPO,
    REFERENCE_REPO,
    write_manager_config,
    write_multi_instance_repo,
    write_named_manager_config,
    write_single_repo_config,
)

def test_info_tracked_cli_interactively_selects_ambiguous_package(
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
    for repo_name in ("alpha", "beta"):
        state_dir = tmp_path / "state" / "dotman" / "repos" / repo_name
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "tracked-packages.toml").write_text(
            "\n".join(
                [
                    "schema_version = 1",
                    "",
                    "[[packages]]",
                    f'repo = "{repo_name}"',
                    'package_id = "sunshine"',
                    'profile = "host/linux"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "info",
            "tracked",
            "sunshine",
        ],
        interaction=interaction,
    )

    assert exit_code == 0
    [request] = interaction.requests
    assert isinstance(request, ChoiceRequest)
    assert request.header_text == "Select a tracked package for 'sunshine':"
    assert [option.label for option in request.options] == ["alpha:sunshine", "beta:sunshine"]
    assert capsys.readouterr().out.startswith("beta:sunshine\n")

def test_info_tracked_cli_emits_package_details_for_tracked_package(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
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
            str(config_path),
            "--json",
            "info",
            "tracked",
            "git",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "info-tracked"
    package = payload["package"]
    assert package["repo"] == "example"
    assert package["package_id"] == "git"
    assert package["description"] == "Base Git configuration"
    assert [binding["package_id"] for binding in package["package_entries"]] == ["core-cli-meta", "git"]
    assert [binding["tracked_reason"] for binding in package["package_entries"]] == ["implicit", "explicit"]
    assert package["owned_targets"] == [
        {
            "additional_sources": [],
            "capture": "raw",
            "chmod": "600",
            "compare": {"live": "capture", "repo": "raw"},
            "editor": {"elevation": "none", "io": "tty", "type": "default"},
            "live_path": str(home / ".gitconfig"),
            "package_id": "git",
            "profile": "basic",
            "render": "jinja",
            "repo": "example",
            "repo_path": str(EXAMPLE_REPO / "packages" / "git" / "files" / "gitconfig"),
            "target_kind": "file",
            "target_name": "gitconfig",
        }
    ]
    assert "target_refs" not in package
    target_names = {target["target_name"] for target in package["package_entries"][0]["targets"]}
    assert target_names == {"gitconfig"}
    for target in package["package_entries"][0]["targets"] + package["owned_targets"]:
        assert not {"render_command", "capture_command", "reconcile", "pull_view_repo", "pull_view_live"} & target.keys()
    assert package["package_entries"][0]["hooks"] == {}
    pre_push = package["package_entries"][1]["hooks"]["pre_push"]
    assert pre_push[0]["package_id"] == "git"
    assert "git" in pre_push[0]["command"]


def test_info_trackable_cli_emits_resolved_target_schema(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    exit_code = main(
        [
            "--config",
            str(config_path),
            "--json",
            "info",
            "trackable",
            "git",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    target = payload["trackable"]["targets"][0]
    assert target["render"] == "jinja"
    assert target["capture"] == "raw"
    assert target["compare"] == {"repo": "raw", "live": "capture"}
    assert target["editor"] == {"type": "default", "io": "tty", "elevation": "none"}
    assert target["additional_sources"] == []
    assert not {"render_command", "capture_command", "reconcile", "pull_view_repo", "pull_view_live"} & target.keys()


def test_info_tracked_cli_emits_readable_text_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
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
            str(config_path),
            "info",
            "tracked",
            "git",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "\n".join(
        [
            "example:git",
            "  Base Git configuration",
            "",
            "  :: provenance",
            "    implicit: example:core-cli-meta@basic",
            "    explicit: example:git@basic",
            "",
            "  :: hooks",
            "    [guard_push]",
            "      command -v git >/dev/null 2>&1",
            "    [pre_push]",
            "      [1] printf 'install %s\\n' git",
            '      [2] sh "$DOTMAN_REPO_ROOT/scripts/log-package-event.sh" "install-packages" "$DOTMAN_PACKAGE_ID"',
            "    [post_push]",
            "      sh hooks/post-push.sh",
            "",
            "  :: owned targets",
            f"    gitconfig -> {home / '.gitconfig'}",
            "",
        ]
    )


def test_info_tracked_cli_handles_empty_directory_targets_when_repo_source_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "claude").mkdir(parents=True)
    (repo_root / "packages" / "claude" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.hooks]",
                'source = "files/claude/hooks"',
                'path = "~/.config/claude/hooks"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")

    live_root = home / ".config" / "claude" / "hooks"
    live_root.mkdir(parents=True)

    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "claude"',
                'profile = "default"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["--config", str(config_path), "--json", "info", "tracked", "claude"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["package"]["owned_targets"][0]["target_kind"] == "directory"
    assert payload["package"]["owned_targets"][0]["repo_path"] == str(
        repo_root / "packages" / "claude" / "files" / "claude" / "hooks"
    )


def test_info_var_cli_emits_provenance_for_resolved_variable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
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
            str(config_path),
            "--json",
            "info",
            "var",
            "vars.git.user_email",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "info-var"
    variable = payload["variable"]
    assert variable["variable"] == "git.user_email"
    assert variable["occurrences"] == [
        {
            "profile": "basic",
            "provenance": {
                "source_kind": "local",
                "source_label": "repo local override",
                "source_path": str(tmp_path / "xdg-config" / "dotman" / "repos" / "example" / "local.toml"),
            },
            "repo": "example",
            "selector": "core-cli-meta",
            "selector_kind": "package",
            "value": "local@example.test",
            "variable": "git.user_email",
        }
    ]


def test_info_var_cli_emits_readable_text_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
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
            str(config_path),
            "info",
            "var",
            "vars.git.user_email",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0] == "git.user_email"
    assert "  :: reason" in output
    assert "      example:core-cli-meta@basic" in output
    assert "  :: resolved value" in output
    assert "      local@example.test" in output
    assert "  :: provenance" in output
    assert f"repo local override: {tmp_path / 'xdg-config' / 'dotman' / 'repos' / 'example' / 'local.toml'}" in output

def test_info_tracked_cli_shows_effective_values_for_target_preset(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "packages" / "shell" / "files").mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (repo_root / "packages" / "shell" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.profile]",
                'source = "files/profile"',
                'path = "~/.profile"',
                'preset = "jinja-editor"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "shell" / "files" / "profile").write_text("hello\n", encoding="utf-8")
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")

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
                'package_id = "shell"',
                'profile = "default"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--json",
            "info",
            "tracked",
            "shell",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    target = payload["package"]["owned_targets"][0]
    assert target["render"] == "jinja"
    assert target["capture"] == "raw"
    assert target["compare"] == {"repo": "render", "live": "raw"}
    assert target["editor"] == {"type": "jinja", "io": "tty", "elevation": "none"}
    assert "render_command" not in target
    assert "capture_command" not in target
    assert "pull_view_repo" not in target
    assert "pull_view_live" not in target
    assert "reconcile" not in target


def test_info_tracked_cli_shows_capture_and_editor_values_for_target_preset(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "packages" / "shell" / "files").mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (repo_root / "packages" / "shell" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.profile]",
                'source = "files/profile"',
                'path = "~/.profile"',
                'preset = "jinja-patch-editor"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "shell" / "files" / "profile").write_text("hello\n", encoding="utf-8")
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")

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
                'package_id = "shell"',
                'profile = "default"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--json",
            "info",
            "tracked",
            "shell",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    target = payload["package"]["owned_targets"][0]
    assert target["render"] == "jinja"
    assert target["capture"] == "patch"
    assert target["compare"] == {"repo": "render", "live": "raw"}
    assert target["editor"] == {"type": "jinja", "io": "tty", "elevation": "none"}
    assert "render_command" not in target
    assert "capture_command" not in target
    assert "pull_view_repo" not in target
    assert "pull_view_live" not in target
    assert "reconcile" not in target



def test_info_tracked_cli_requires_specific_multi_instance_package_identity_in_non_interactive_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_multi_instance_repo(repo_root)
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
                'package_id = "profiled"',
                'profile = "basic"',
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "profiled"',
                'profile = "work"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--json",
            "info",
            "tracked",
            "profiled",
        ]
    )

    assert exit_code == 1
    assert "ambiguous" in capsys.readouterr().err

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--json",
            "info",
            "tracked",
            "profiled<work>",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["package"]["package_ref"] == "profiled<work>"
    assert payload["package"]["bound_profile"] == "work"
    assert [binding["profile"] for binding in payload["package"]["package_entries"]] == ["work"]

def test_info_tracked_cli_uses_resolver_for_ambiguous_multi_instance_identity_in_interactive_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_multi_instance_repo(repo_root)
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
                'package_id = "profiled"',
                'profile = "basic"',
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "profiled"',
                'profile = "work"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "info",
            "tracked",
            "profiled",
        ],
        interaction=ScriptedInteraction(choices=["fixture:profiled<work>"]),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert output.startswith("fixture:profiled<work>\n")
    assert f"managed -> {home / '.config' / 'profiled' / 'work.conf'}" in output
