from __future__ import annotations

import json
from pathlib import Path

from dotman.cli import main

from tests.helpers import write_named_manager_config


def write_trackables_repo(repo_root: Path) -> None:
    (repo_root / "packages" / "git").mkdir(parents=True)
    (repo_root / "packages" / "profiled").mkdir(parents=True)
    (repo_root / "groups").mkdir(parents=True)

    (repo_root / "packages" / "git" / "package.toml").write_text(
        "\n".join(
            [
                'description = "Base Git configuration"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "profiled" / "package.toml").write_text(
        "\n".join(
            [
                'binding_mode = "multi_instance"',
                'description = "Profile-driven package"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "groups" / "bundle.toml").write_text(
        "\n".join(
            [
                'members = ["git", "profiled"]',
                'description = "Bundle group"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_list_trackables_cli_emits_json_results(tmp_path: Path, capsys) -> None:
    repo_root = tmp_path / "trackables-repo"
    write_trackables_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main(["--config", str(config_path), "--json", "list", "trackables"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "list-trackables"
    assert [trackable["qualified_selector"] for trackable in payload["trackables"]] == [
        "fixture:git",
        "fixture:profiled",
        "fixture:bundle",
    ]
    assert payload["trackables"][0]["binding_mode"] == "singleton"
    assert payload["trackables"][1]["binding_mode"] == "multi_instance"
    assert payload["trackables"][2]["member_count"] == 2


def test_list_trackables_cli_emits_readable_text_output(tmp_path: Path, capsys) -> None:
    repo_root = tmp_path / "trackables-repo"
    write_trackables_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main(["--config", str(config_path), "list", "trackables"])

    assert exit_code == 0
    assert capsys.readouterr().out == "\n".join(
        [
            "fixture:git [package] [singleton] (Base Git configuration)",
            "fixture:profiled [package] [multi_instance] (Profile-driven package)",
            "fixture:bundle [group] [2 members] (Bundle group)",
            "",
        ]
    )
