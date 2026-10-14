from __future__ import annotations

import json
from pathlib import Path

from dotman.cli import main

from tests.helpers import write_named_manager_config


def write_search_repo(repo_root: Path) -> None:
    (repo_root / "packages" / "git").mkdir(parents=True)
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
    (repo_root / "groups" / "infra.toml").write_text(
        "\n".join(
            [
                'members = ["git"]',
                'description = "Git configuration collection"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_search_cli_emits_ranked_json_results(tmp_path: Path, capsys) -> None:
    repo_root = tmp_path / "search-repo"
    write_search_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main(["--config", str(config_path), "--json", "search", "git"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "matches": [
            {
                "binding_mode": "singleton",
                "description": "Base Git configuration",
                "kind": "package",
                "match_reason": "exact_selector",
                "member_count": None,
                "qualified_selector": "fixture:git",
                "rank": 1,
                "repo": "fixture",
                "selector": "git",
            },
            {
                "binding_mode": None,
                "description": "Git configuration collection",
                "kind": "group",
                "match_reason": "substring_description",
                "member_count": 1,
                "qualified_selector": "fixture:infra",
                "rank": 2,
                "repo": "fixture",
                "selector": "infra",
            },
        ],
        "operation": "search",
        "query": "git",
    }


def test_search_cli_emits_readable_text_output(tmp_path: Path, capsys) -> None:
    repo_root = tmp_path / "search-repo"
    write_search_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main(["--config", str(config_path), "search", "git"])

    assert exit_code == 0
    assert capsys.readouterr().out == "\n".join(
        [
            "fixture:git [package] [singleton] (Base Git configuration)",
            "fixture:infra [group] [1 members] (Git configuration collection)",
            "",
        ]
    )


def test_search_cli_emits_no_match_message(tmp_path: Path, capsys) -> None:
    repo_root = tmp_path / "search-repo"
    write_search_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main(["--config", str(config_path), "search", "missing"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "no packages or groups matched 'missing'"
