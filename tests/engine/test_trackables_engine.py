from __future__ import annotations

from pathlib import Path

from dotman.engine import DotmanEngine

from tests.helpers import write_named_manager_config


def _write_trackable_repo(repo_root: Path, *, package_id: str, package_description: str, group_description: str) -> None:
    (repo_root / "packages" / package_id).mkdir(parents=True)
    (repo_root / "packages" / package_id / "package.toml").write_text(
        "\n".join(
            [
                f'description = "{package_description}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "groups").mkdir(parents=True)
    (repo_root / "groups" / f"{package_id}-group.toml").write_text(
        "\n".join(
            [
                f'members = ["{package_id}"]',
                f'description = "{group_description}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_list_trackables_orders_packages_before_groups_by_repo(tmp_path: Path) -> None:
    alpha_repo = tmp_path / "alpha-repo"
    beta_repo = tmp_path / "beta-repo"
    _write_trackable_repo(alpha_repo, package_id="git", package_description="Alpha Git", group_description="Alpha group")
    _write_trackable_repo(beta_repo, package_id="vim", package_description="Beta Vim", group_description="Beta group")
    config_path = write_named_manager_config(tmp_path, {"alpha": alpha_repo, "beta": beta_repo})

    engine = DotmanEngine.from_config_path(config_path)
    trackables = engine.list_trackables()

    assert [trackable.qualified_selector for trackable in trackables] == [
        "alpha:git",
        "alpha:git-group",
        "beta:vim",
        "beta:vim-group",
    ]
    assert [trackable.kind for trackable in trackables] == ["package", "group", "package", "group"]
    assert trackables[0].binding_mode == "singleton"
    assert trackables[1].member_count == 1
