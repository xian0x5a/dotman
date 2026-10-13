from __future__ import annotations

import json
from pathlib import Path

import dotman.cli_interaction as cli
from dotman.cli import main

from tests.helpers import (
    EXAMPLE_REPO,
    REFERENCE_REPO,
    write_manager_config,
    write_multi_instance_repo,
    write_named_manager_config,
    write_single_repo_config_with_state_key,
)


def test_render_tracked_state_uses_warning_colors_for_orphan_and_invalid(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert cli.render_tracked_state("explicit") == "\x1b[2mexplicit\x1b[0m"
    assert cli.render_tracked_state("implicit") == "\x1b[2mimplicit\x1b[0m"
    assert cli.render_tracked_state("orphan") == "\x1b[2;33morphan\x1b[0m"
    assert cli.render_tracked_state("invalid") == "\x1b[2;31minvalid\x1b[0m"


def test_list_tracked_cli_lists_unique_tracked_packages(
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
            "list",
            "tracked",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "list-tracked"
    assert payload["invalid_package_entries"] == []
    assert [item["package_id"] for item in payload["packages"]] == ["core-cli-meta", "git", "nvim"]
    packages = {item["package_id"]: item for item in payload["packages"]}
    assert set(packages) == {"core-cli-meta", "git", "nvim"}
    assert packages["core-cli-meta"]["state"] == "explicit"
    assert packages["git"]["state"] == "explicit"
    assert packages["nvim"]["state"] == "implicit"
    assert [binding["package_id"] for binding in packages["git"]["package_entries"]] == ["core-cli-meta", "git"]
    assert packages["git"]["description"] == "Base Git configuration"


def test_list_repo_cli_emits_configured_repos_in_json(tmp_path: Path, capsys) -> None:
    config_path = write_named_manager_config(
        tmp_path,
        {
            "example": EXAMPLE_REPO,
            "sandbox": REFERENCE_REPO,
        },
    )

    exit_code = main(["--config", str(config_path), "--json", "list", "repo"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "list-repos"
    assert [(repo["name"], repo["path"], repo["order"], repo["state_key"]) for repo in payload["repos"]] == [
        ("example", str(EXAMPLE_REPO), 10, "example"),
        ("sandbox", str(REFERENCE_REPO), 20, "sandbox"),
    ]


def test_list_repo_cli_emits_readable_text_output(tmp_path: Path, capsys) -> None:
    config_path = write_named_manager_config(tmp_path, {"example": EXAMPLE_REPO})

    exit_code = main(["--config", str(config_path), "list", "repo"])

    assert exit_code == 0
    assert capsys.readouterr().out == f"example [order 10] {EXAMPLE_REPO}\n"


def test_list_repo_cli_does_not_load_repo_manifests(tmp_path: Path, capsys) -> None:
    repo_root = tmp_path / "repo"
    bad_package_dir = repo_root / "packages" / "bad"
    bad_package_dir.mkdir(parents=True)
    (bad_package_dir / "package.toml").write_text("not valid toml = [", encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"broken": repo_root})

    exit_code = main(["--config", str(config_path), "--json", "list", "repo"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["repos"][0]["name"] == "broken"


def test_list_tracked_cli_emits_readable_text_output(
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
            "list",
            "tracked",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "\n".join(
        [
            "example:core-cli-meta explicit",
            "example:git explicit",
            "example:nvim implicit",
            "",
        ]
    )


def test_list_vars_cli_emits_resolved_values_and_provenance_in_json(
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
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main([
        "--config",
        str(config_path),
        "--json",
        "list",
        "vars",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "list-vars"
    variables = {item["variable"]: item for item in payload["variables"]}
    assert variables["git.user_name"]["value"] == "Example User"
    assert variables["git.user_name"]["provenance"] == {
        "source_kind": "package",
        "source_label": "git",
        "source_path": str(EXAMPLE_REPO / "packages" / "git" / "package.toml"),
    }
    assert variables["git.user_email"]["value"] == "local@example.test"
    assert variables["git.user_email"]["provenance"] == {
        "source_kind": "local",
        "source_label": "repo local override",
        "source_path": str(tmp_path / "xdg-config" / "dotman" / "repos" / "example" / "local.toml"),
    }
    assert variables["nvim.leader"]["value"] == " "
    assert variables["nvim.leader"]["provenance"] == {
        "source_kind": "profile",
        "source_label": "basic",
        "source_path": str(EXAMPLE_REPO / "profiles" / "basic.toml"),
    }


def test_list_vars_cli_emits_readable_text_output(
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

    exit_code = main([
        "--config",
        str(config_path),
        "list",
        "vars",
    ])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "git.user_name (example:core-cli-meta@basic)" in output
    assert "nvim.leader (example:core-cli-meta@basic)" in output
    assert output.count("git.user_email (") == 1

def test_list_tracked_cli_lists_multi_instance_packages_per_bound_profile(
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
            "list",
            "tracked",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [package["package_ref"] for package in payload["packages"]] == [
        "profiled<basic>",
        "profiled<work>",
    ]
    assert [package["bound_profile"] for package in payload["packages"]] == ["basic", "work"]
    assert [package["state"] for package in payload["packages"]] == ["explicit", "explicit"]


def test_list_tracked_cli_reports_invalid_package_entries_in_json_and_human_output(
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
                'package_id = "old-meta"',
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
            "list",
            "tracked",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [package["package_id"] for package in payload["packages"]] == ["git"]
    assert payload["invalid_package_entries"] == [
        {
            "message": "unknown selector",
            "profile": "basic",
            "reason": "unknown_selector",
            "repo": "example",
            "package_id": "old-meta",
            "state": "invalid",
            "state_key": "example",
        }
    ]

    exit_code = main(
        [
            "--config",
            str(config_path),
            "list",
            "tracked",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "\n".join(
        [
            "example:git explicit",
            "example:old-meta invalid",
            "",
        ]
    )


def test_list_tracked_cli_human_output_sorts_orphans_before_invalids_after_packages(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_home = tmp_path / "xdg-state"
    state_home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    config_path = write_single_repo_config_with_state_key(tmp_path, repo_name="example", repo_path=EXAMPLE_REPO)
    configured_state_dir = state_home / "dotman" / "repos" / "example"
    configured_state_dir.mkdir(parents=True, exist_ok=True)
    (configured_state_dir / "tracked-packages.toml").write_text(
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
                'package_id = "old-meta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    orphan_state_dir = state_home / "dotman" / "repos" / "removed"
    orphan_state_dir.mkdir(parents=True, exist_ok=True)
    (orphan_state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "removed-repo"',
                'package_id = "linux"',
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
            "list",
            "tracked",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "\n".join(
        [
            "example:git explicit",
            "removed-repo:linux orphan",
            "example:old-meta invalid",
            "",
        ]
    )


def test_list_tracked_cli_discovers_orphan_bindings_under_state_root(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_home = tmp_path / "xdg-state"
    state_home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    config_path = write_single_repo_config_with_state_key(tmp_path, repo_name="example", repo_path=EXAMPLE_REPO)
    configured_state_dir = state_home / "dotman" / "repos" / "example"
    configured_state_dir.mkdir(parents=True, exist_ok=True)
    (configured_state_dir / "tracked-packages.toml").write_text(
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
    orphan_state_dir = state_home / "dotman" / "repos" / "removed"
    orphan_state_dir.mkdir(parents=True, exist_ok=True)
    (orphan_state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "removed-repo"',
                'package_id = "linux"',
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
            "list",
            "tracked",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [package["package_id"] for package in payload["packages"]] == ["git"]
    assert payload["invalid_package_entries"] == [
        {
            "message": "repo not in config",
            "profile": "basic",
            "reason": "unknown_repo",
            "repo": "removed-repo",
            "package_id": "linux",
            "state": "orphan",
            "state_key": "removed",
        }
    ]
