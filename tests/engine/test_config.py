from __future__ import annotations

import re
from pathlib import Path

import pytest

from dotman.config import default_snapshot_root, expand_path
from dotman.engine import DotmanEngine
from tests.helpers import (
    EXAMPLE_REPO,
    REFERENCE_REPO,
    write_single_repo_config,
    write_single_repo_config_with_state_key,
)


def test_config_validation_rejects_duplicate_repo_order(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.one]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                "",
                "[repos.two]",
                f'path = "{REFERENCE_REPO}"',
                "order = 10",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unique"):
        DotmanEngine.from_config_path(config_path)


def test_config_defaults_snapshot_settings(tmp_path: Path) -> None:
    config_path = write_single_repo_config(tmp_path, repo_name="example", repo_path=EXAMPLE_REPO)

    engine = DotmanEngine.from_config_path(config_path)

    assert engine.config.snapshots.enabled is True
    assert engine.config.snapshots.path == default_snapshot_root()
    assert engine.config.snapshots.max_generations == 10
    assert engine.config.ui.full_paths is False
    assert engine.config.ui.menus.bottom_up is True
    assert engine.config.ui.compact_path_tail_segments == 2
    assert engine.config.file_symlink_mode == "prompt"
    assert engine.config.dir_symlink_mode == "fail"


def test_config_reads_ui_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                "",
                "[ui]",
                "full_paths = true",
                "compact_path_tail_segments = 3",
                "",
                "[ui.menus]",
                "bottom_up = false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    assert engine.config.ui.full_paths is True
    assert engine.config.ui.menus.bottom_up is False
    assert engine.config.ui.compact_path_tail_segments == 3


@pytest.mark.parametrize("value", ["0", "false", '"2"'])
def test_config_rejects_invalid_compact_path_tail_segments(tmp_path: Path, value: str) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                "",
                "[ui]",
                f"compact_path_tail_segments = {value}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="compact_path_tail_segments"):
        DotmanEngine.from_config_path(config_path)


def test_config_rejects_legacy_selection_menu_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                "",
                "[selection_menu]",
                "full_paths = true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="selection_menu"):
        DotmanEngine.from_config_path(config_path)


def test_config_reads_symlink_mode_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                "",
                "[symlinks]",
                'file_symlink_mode = "follow"',
                'dir_symlink_mode = "follow"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    assert engine.config.file_symlink_mode == "follow"
    assert engine.config.dir_symlink_mode == "follow"


def test_config_reads_snapshot_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    snapshot_path = tmp_path / "custom-snapshots"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                "",
                "[snapshots]",
                "enabled = false",
                f'path = "{snapshot_path}"',
                "max_generations = 2",
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    assert engine.config.snapshots.enabled is False
    assert engine.config.snapshots.path == snapshot_path.resolve()
    assert engine.config.snapshots.max_generations == 2


def test_expand_path_can_preserve_declared_symlink_path(tmp_path: Path) -> None:
    real_path = tmp_path / "real-target"
    real_path.mkdir()
    symlink_path = tmp_path / "declared-link"
    symlink_path.symlink_to(real_path, target_is_directory=True)

    assert expand_path(str(symlink_path), dereference=False) == symlink_path
    assert expand_path(str(symlink_path)) == real_path.resolve()



def test_config_rejects_non_positive_snapshot_retention(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                "",
                "[snapshots]",
                "max_generations = 0",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="max_generations"):
        DotmanEngine.from_config_path(config_path)


def test_config_defaults_local_override_path_from_xdg_config_home(tmp_path: Path) -> None:
    config_path = write_single_repo_config(tmp_path, repo_name="example", repo_path=EXAMPLE_REPO)

    engine = DotmanEngine.from_config_path(config_path)

    assert engine.config.repos["example"].local_override_path == (
        tmp_path / "xdg-config" / "dotman" / "repos" / "example" / "local.toml"
    ).resolve()


def test_config_defaults_state_key_to_repo_name_and_derives_state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_home = tmp_path / "xdg-state"
    state_home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    config_path = write_single_repo_config_with_state_key(tmp_path, repo_name="example", repo_path=EXAMPLE_REPO)

    engine = DotmanEngine.from_config_path(config_path)

    repo_config = engine.config.repos["example"]
    assert repo_config.state_key == "example"
    assert repo_config.state_path == (state_home / "dotman" / "repos" / "example").resolve()


def test_config_rejects_duplicate_state_key(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.one]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                'state_key = "shared"',
                "",
                "[repos.two]",
                f'path = "{REFERENCE_REPO}"',
                "order = 20",
                'state_key = "shared"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="state_key"):
        DotmanEngine.from_config_path(config_path)


def test_config_rejects_invalid_state_key_format(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                'state_key = "bad/key"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="state_key"):
        DotmanEngine.from_config_path(config_path)


def test_config_rejects_legacy_state_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                f'state_path = "{tmp_path / "legacy-state"}"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="state_path"):
        DotmanEngine.from_config_path(config_path)


def test_repository_loads_local_overrides_from_xdg_config(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "local.toml").write_text('[vars]\nsource = "repo-root"\n', encoding="utf-8")

    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    local_override_path = tmp_path / "xdg-config" / "dotman" / "repos" / "fixture" / "local.toml"
    local_override_path.parent.mkdir(parents=True, exist_ok=True)
    local_override_path.write_text('[vars]\nsource = "xdg"\n', encoding="utf-8")

    engine = DotmanEngine.from_config_path(config_path)

    assert engine.get_repo("fixture").local_vars == {"source": "xdg"}


def test_repository_rejects_unknown_local_override_top_level_keys(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    local_override_path = tmp_path / "xdg-config" / "dotman" / "repos" / "fixture" / "local.toml"
    local_override_path.parent.mkdir(parents=True, exist_ok=True)
    local_override_path.write_text('[vars]\nvalue = "ok"\n\n[hooks]\npre_push = "echo nope"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="unknown top-level keys"):
        DotmanEngine.from_config_path(config_path)


def test_repository_rejects_non_table_vars_in_local_override(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    local_override_path = tmp_path / "xdg-config" / "dotman" / "repos" / "fixture" / "local.toml"
    local_override_path.parent.mkdir(parents=True, exist_ok=True)
    local_override_path.write_text('vars = "bad"\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"\[vars\] must be a table"):
        DotmanEngine.from_config_path(config_path)


def test_config_reports_toml_parse_error_with_config_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                "",
                "[snapshots]",
                'enabled = true',
                'path = "unterminated',
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=rf"invalid TOML in manager config {re.escape(str(config_path))}:",
    ):
        DotmanEngine.from_config_path(config_path)


def test_repository_reports_toml_parse_error_with_package_context(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    package_manifest_path = repo_root / "packages" / "git" / "package.toml"
    package_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    package_manifest_path.write_text(
        "\n".join(
            [
                'id = "git"',
                "",
                "[targets.git]",
                'source = "files/gitconfig"',
                'path = "~/.gitconfig',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)

    with pytest.raises(
        ValueError,
        match=rf"invalid TOML in package manifest for 'fixture:git' {re.escape(str(package_manifest_path))}:",
    ):
        DotmanEngine.from_config_path(config_path)


def test_repository_rejects_package_ids_with_dots(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    package_manifest_path = repo_root / "packages" / "git" / "package.toml"
    package_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    package_manifest_path.write_text('id = "git.ui"\n', encoding="utf-8")
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)

    with pytest.raises(
        ValueError,
        match=rf"package manifest {re.escape(str(package_manifest_path))}: invalid package id 'git\.ui'",
    ):
        DotmanEngine.from_config_path(config_path)


def test_repository_rejects_target_names_with_slashes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    package_manifest_path = repo_root / "packages" / "git" / "package.toml"
    package_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    package_manifest_path.write_text(
        "\n".join(
            [
                'id = "git"',
                "",
                '[targets."git/config"]',
                'source = "files/gitconfig"',
                'path = "~/.gitconfig"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)

    with pytest.raises(
        ValueError,
        match=rf"package manifest {re.escape(str(package_manifest_path))}: invalid target name 'git/config'",
    ):
        DotmanEngine.from_config_path(config_path)


def test_repository_rejects_target_names_with_dots(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    package_manifest_path = repo_root / "packages" / "git" / "package.toml"
    package_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    package_manifest_path.write_text(
        "\n".join(
            [
                'id = "git"',
                "",
                '[targets."git.config"]',
                'source = "files/gitconfig"',
                'path = "~/.gitconfig"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)

    with pytest.raises(
        ValueError,
        match=rf"package manifest {re.escape(str(package_manifest_path))}: invalid target name 'git\.config'",
    ):
        DotmanEngine.from_config_path(config_path)
