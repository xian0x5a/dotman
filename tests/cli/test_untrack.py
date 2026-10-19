from __future__ import annotations

import json
from pathlib import Path

import pytest

import dotman.cli_interaction as cli
from dotman.cli import main
from dotman.interaction import ScriptedInteraction

from tests.helpers import (
    EXAMPLE_REPO,
    write_manager_config,
    write_multi_instance_repo,
    write_named_manager_config,
    write_tracked_packages_state,
    write_untrack_conflict_repo,
)


def test_untrack_cli_updates_state(
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
            "untrack",
            "example:git@basic",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "state-only"
    assert payload["operation"] == "untrack"
    assert payload["package_entry"] == {
        "repo": "example",
        "package_id": "git",
        "profile": "basic",
    }
    assert payload["still_tracked_package"] == {
        "repo": "example",
        "package_id": "git",
        "package_entries": [
            {
                "repo": "example",
                "package_id": "core-cli-meta",
                "profile": "basic",
                "tracked_reason": "implicit",
            }
        ],
    }
    assert (state_dir / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
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

@pytest.mark.parametrize(
    "package_id",
    [
        pytest.param("git", id="resolvable"),
        pytest.param("gone", id="invalid"),
    ],
)
def test_untrack_cli_removes_every_duplicate_state_row(
    tmp_path: Path,
    monkeypatch,
    package_id: str,
) -> None:
    # A hand-edited state file can repeat a row; untrack must leave none behind.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_path = write_manager_config(tmp_path)
    state_root = tmp_path / "state"
    write_tracked_packages_state(
        state_root,
        repo_name="example",
        entries=[(package_id, "basic"), (package_id, "basic"), ("nvim", "basic")],
    )

    exit_code = main(["--config", str(config_path), "--json", "untrack", f"example:{package_id}@basic"])

    assert exit_code == 0
    state_path = state_root / "dotman" / "repos" / "example" / "tracked-packages.toml"
    remaining_state = state_path.read_text(encoding="utf-8")
    assert f'package_id = "{package_id}"' not in remaining_state
    assert 'package_id = "nvim"' in remaining_state


def test_untrack_cli_allows_selector_only_when_unique(
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
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--json",
            "untrack",
            "example:git",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["package_entry"] == {
        "repo": "example",
        "package_id": "git",
        "profile": "basic",
    }


def test_untrack_cli_removes_tracked_entries_from_group_selector(
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
            "untrack",
            "example:os/arch",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "untracked 1 package entry from example:os/arch@basic" in output
    assert "example:core-cli-meta@basic" in output
    assert (state_dir / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
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


def test_untrack_cli_emits_group_untrack_json(
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
            "untrack",
            "example:os/arch@basic",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "untrack"
    assert payload["request"] == {
        "repo": "example",
        "selector": "os/arch",
        "selector_kind": "group",
        "profile": "basic",
    }
    assert payload["package_entries"] == [
        {
            "repo": "example",
            "package_id": "core-cli-meta",
            "profile": "basic",
        }
    ]


def test_untrack_cli_removes_group_singletons_across_profiles_without_prompt(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "singleton-group-repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    for package_id in ("alpha", "beta"):
        (repo_root / "packages" / package_id).mkdir(parents=True)
        (repo_root / "packages" / package_id / "package.toml").write_text('', encoding="utf-8")
    (repo_root / "groups").mkdir()
    (repo_root / "groups" / "bundle.toml").write_text('members = ["alpha", "beta"]\n', encoding="utf-8")
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
                'package_id = "alpha"',
                'profile = "basic"',
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "beta"',
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
            "untrack",
            "fixture:bundle",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "untracked 2 package entries from fixture:bundle" in output
    assert "fixture:alpha@basic" in output
    assert "fixture:beta@work" in output
    assert (state_dir / "tracked-packages.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
        ]
    )


def test_untrack_cli_requires_group_profile_when_tracked_members_span_profiles(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "multi-instance-repo"
    write_multi_instance_repo(repo_root)
    (repo_root / "groups").mkdir()
    (repo_root / "groups" / "bundle.toml").write_text('members = ["profiled"]\n', encoding="utf-8")
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
            "untrack",
            "fixture:bundle",
        ]
    )

    assert exit_code == 2
    assert "tracked group 'fixture:bundle' is ambiguous across package instances: profiled<basic>, profiled<work>" in capsys.readouterr().err


def test_untrack_cli_errors_for_untracked_binding(
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
            "untrack",
            "example:git@basic",
        ]
    )

    assert exit_code == 2
    assert "is not currently tracked" in capsys.readouterr().err

def test_untrack_cli_reports_dependency_owner_for_untracked_package_selector(
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
            "untrack",
            "nvim@basic",
        ]
    )

    assert exit_code == 2
    assert "cannot untrack 'example:nvim': required by tracked package entries: example:core-cli-meta@basic" in capsys.readouterr().err

def test_untrack_cli_rejects_removal_that_exposes_singleton_dependency_profile_ambiguity(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_untrack_conflict_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    original_state = "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "shared"',
            'profile = "direct"',
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "stack-a"',
            'profile = "work"',
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "stack-b"',
            'profile = "personal"',
            "",
        ]
    )
    (state_dir / "tracked-packages.toml").write_text(original_state, encoding="utf-8")

    exit_code = main(
        [
            "--config",
            str(config_path),
            "untrack",
            "fixture:shared@direct",
        ]
    )

    assert exit_code == 2
    assert "ambiguous implicit profile contexts for fixture:shared" in capsys.readouterr().err
    assert (state_dir / "tracked-packages.toml").read_text(encoding="utf-8") == original_state

def test_untrack_cli_uses_rendered_binding_label_for_terminal_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

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
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "untrack",
            "example:git@basic",
        ]
    )

    assert exit_code == 0
    assert f"untracked {cli.render_full_spec_selector_label(repo_name='example', selector='git', profile='basic')}" in capsys.readouterr().out

def test_untrack_cli_reports_remaining_package_tracking_after_success(
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
            "untrack",
            "example:git@basic",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "untracked example:git@basic" in output
    assert "example:git remains tracked via:" in output
    assert "implicit: example:core-cli-meta@basic" in output


def test_untrack_cli_removes_chosen_orphan_record_sharing_identity_with_configured_record(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    # The orphan state directory holds the same repo:package@profile identity as the
    # configured repo, so only the chosen record's state directory tells them apart.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config_path = write_named_manager_config(tmp_path, {"example": EXAMPLE_REPO})
    state_root = tmp_path / "state"
    write_tracked_packages_state(state_root, repo_name="example", entries=[("git", "basic")])
    configured_state_path = state_root / "dotman" / "repos" / "example" / "tracked-packages.toml"
    orphan_state_path = state_root / "dotman" / "repos" / "removed" / "tracked-packages.toml"
    orphan_state_path.parent.mkdir(parents=True)
    orphan_state_path.write_text(configured_state_path.read_text(encoding="utf-8"), encoding="utf-8")
    configured_state_before = configured_state_path.read_text(encoding="utf-8")
    interaction = ScriptedInteraction(choices=["entry:removed:example:git@basic"])

    exit_code = main(
        ["--config", str(config_path), "untrack", "example:git@basic"],
        interaction=interaction,
    )

    assert exit_code == 0, capsys.readouterr().err
    assert orphan_state_path.read_text(encoding="utf-8") == "schema_version = 1\n"
    assert configured_state_path.read_text(encoding="utf-8") == configured_state_before
