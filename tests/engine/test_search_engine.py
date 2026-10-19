from __future__ import annotations

from pathlib import Path

from dotman.engine import DotmanEngine

from tests.helpers import write_named_manager_config


def write_search_repo(repo_root: Path, *, package_description: str) -> None:
    (repo_root / "packages" / "git").mkdir(parents=True)
    (repo_root / "packages" / "git" / "package.toml").write_text(
        "\n".join(
            [
                f'description = "{package_description}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_search_repo_with_group(repo_root: Path) -> None:
    write_search_repo(repo_root, package_description="Base Git configuration")
    (repo_root / "groups").mkdir(parents=True)
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


def test_search_selectors_orders_equal_bare_matches_by_repo_order(tmp_path: Path) -> None:
    alpha_repo = tmp_path / "alpha-repo"
    beta_repo = tmp_path / "beta-repo"
    write_search_repo(alpha_repo, package_description="Alpha Git configuration")
    write_search_repo(beta_repo, package_description="Beta Git configuration")
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_repo, "beta": beta_repo})

    engine = DotmanEngine.from_config_path(config_path)
    matches = engine.search_selectors("git")

    assert [match.qualified_selector for match in matches] == ["alpha:git", "beta:git"]
    assert [match.match_reason for match in matches] == ["exact_selector", "exact_selector"]
    assert [match.rank for match in matches] == [1, 2]


def test_search_selectors_includes_group_descriptions(tmp_path: Path) -> None:
    repo_root = tmp_path / "search-repo"
    write_search_repo_with_group(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    engine = DotmanEngine.from_config_path(config_path)
    matches = engine.search_selectors("collection")

    assert [match.kind for match in matches] == ["group"]
    assert matches[0].qualified_selector == "fixture:infra"
    assert matches[0].match_reason == "substring_description"
    assert matches[0].description == "Git configuration collection"


def test_search_selectors_does_not_treat_slash_as_repo_qualifier(tmp_path: Path) -> None:
    repo_root = tmp_path / "search-repo"
    write_search_repo(repo_root, package_description="Base Git configuration")
    config_path = write_named_manager_config(tmp_path, {"work": repo_root})

    engine = DotmanEngine.from_config_path(config_path)

    assert engine.search_selectors("work/git") == []
    colon_matches = engine.search_selectors("work:git")
    assert [match.qualified_selector for match in colon_matches] == ["work:git"]
    assert colon_matches[0].match_reason == "exact_repo_qualified_selector"
    assert colon_matches[0].rank == 1

