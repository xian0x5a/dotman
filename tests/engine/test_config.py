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
    write_tracked_packages_state,
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
    package_manifest_path = repo_root / "packages" / "git.ui" / "package.toml"
    package_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    package_manifest_path.write_text('', encoding="utf-8")
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


def write_tracked_fixture_repo(
    tmp_path: Path,
    *,
    packages: dict[str, list[str]],
    profile_lines: list[str] | None = None,
) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("\n".join(profile_lines or []), encoding="utf-8")
    for package_id, manifest_lines in packages.items():
        package_root = repo_root / "packages" / package_id
        package_root.mkdir(parents=True)
        (package_root / "package.toml").write_text("\n".join([*manifest_lines, ""]), encoding="utf-8")
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[(package_id, "default") for package_id in packages],
    )
    return write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)


def test_repository_rejects_package_extends_cycle(tmp_path: Path) -> None:
    config_path = write_tracked_fixture_repo(
        tmp_path,
        packages={"alpha": ['extends = ["beta"]'], "beta": ['extends = ["alpha"]']},
    )

    # Either load-time or resolution-time validation is acceptable; a
    # RecursionError escaping from either stage is the reported bug.
    with pytest.raises(ValueError, match=r"(?i)extends.*cycle|cycle.*extends") as exc_info:
        engine = DotmanEngine.from_config_path(config_path)
        engine.resolve_sync_scope(["fixture:alpha"])

    assert "alpha" in str(exc_info.value)
    assert "beta" in str(exc_info.value)


def test_repository_rejects_profile_include_of_missing_profile(tmp_path: Path) -> None:
    config_path = write_tracked_fixture_repo(
        tmp_path,
        packages={"app": []},
        profile_lines=['includes = ["absent-profile"]'],
    )

    # Either load-time or resolution-time validation is acceptable; a
    # KeyError escaping from either stage is the reported bug.
    with pytest.raises(ValueError, match="absent-profile") as exc_info:
        engine = DotmanEngine.from_config_path(config_path)
        engine.resolve_sync_scope(["fixture:app"])

    assert "profile" in str(exc_info.value)


@pytest.mark.parametrize(
    ("repo_lines", "extra_lines", "field_pattern"),
    [
        (["order = true"], [], r"\border\b"),
        (["order = 10"], ["[snapshots]", "max_generations = true"], r"max_generations"),
        (["order = 10"], ["[snapshots]", "path = 123"], r"snapshots.*path"),
    ],
    ids=["repo-order-bool", "snapshots-max-generations-bool", "snapshots-path-int"],
)
def test_config_rejects_wrongly_typed_scalar_settings(
    tmp_path: Path, repo_lines: list[str], extra_lines: list[str], field_pattern: str
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                *repo_lines,
                "",
                *extra_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=field_pattern):
        DotmanEngine.from_config_path(config_path)


def write_chmod_repo(tmp_path: Path, *, target_chmod: str | None = None, path_rule_chmod: str | None = None) -> Path:
    repo_root = tmp_path / "repo"
    package_manifest_path = repo_root / "packages" / "app" / "package.toml"
    package_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        "[targets.config]",
        'source = "files/config"',
        'path = "~/.config/app"',
        'type = "directory"',
    ]
    if target_chmod is not None:
        lines.append(f'chmod = "{target_chmod}"')
    if path_rule_chmod is not None:
        lines.extend(["", "[targets.config.path_rules.secrets]", 'pattern = "*.conf"', f'chmod = "{path_rule_chmod}"'])
    package_manifest_path.write_text("\n".join([*lines, ""]), encoding="utf-8")
    return write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)


@pytest.mark.parametrize("location", ["target_chmod", "path_rule_chmod"])
@pytest.mark.parametrize("chmod", ["77777", "-755", "8", "75", "0o755"])
def test_repository_rejects_chmod_outside_three_or_four_octal_digits(tmp_path: Path, location: str, chmod: str) -> None:
    config_path = write_chmod_repo(tmp_path, **{location: chmod})

    with pytest.raises(ValueError, match="chmod"):
        DotmanEngine.from_config_path(config_path)


@pytest.mark.parametrize("location", ["target_chmod", "path_rule_chmod"])
@pytest.mark.parametrize("chmod", ["644", "600", "0755", "0700", "4755"])
def test_repository_accepts_three_or_four_digit_octal_chmod(tmp_path: Path, location: str, chmod: str) -> None:
    config_path = write_chmod_repo(tmp_path, **{location: chmod})

    DotmanEngine.from_config_path(config_path)


@pytest.mark.parametrize(
    ("child_lines", "expected_binding_mode"),
    [
        (['extends = ["base"]'], "multi_instance"),
        # A later parent that never sets binding_mode must not reset an earlier parent's choice.
        (['extends = ["base", "plain"]'], "multi_instance"),
        (['extends = ["base"]', 'binding_mode = "singleton"'], "singleton"),
    ],
)
def test_package_binding_mode_inherits_through_extends(
    tmp_path: Path, child_lines: list[str], expected_binding_mode: str
) -> None:
    config_path = write_tracked_fixture_repo(
        tmp_path,
        packages={
            "base": ['binding_mode = "multi_instance"'],
            "plain": ['description = "plain"'],
            "child": child_lines,
        },
    )
    engine = DotmanEngine.from_config_path(config_path)

    assert engine.get_repo("fixture").resolve_package("child").binding_mode == expected_binding_mode
    catalog_modes = {entry.selector: entry.binding_mode for entry in engine.list_trackables() if entry.kind == "package"}
    assert catalog_modes == {"base": "multi_instance", "plain": "singleton", "child": expected_binding_mode}
