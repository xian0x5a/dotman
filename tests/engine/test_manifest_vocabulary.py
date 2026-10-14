from __future__ import annotations

from pathlib import Path

import pytest

from dotman.engine import DotmanEngine
from tests.helpers import write_single_repo_config, write_tracked_packages_state


def write_manifest_repo(
    tmp_path: Path,
    *,
    repo_manifest: list[str] | None = None,
    target_manifest: list[str] | None = None,
    target_is_directory: bool = False,
) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "packages" / "app" / "files").mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (repo_root / "repo.toml").write_text(
        "\n".join([*(repo_manifest or []), ""]),
        encoding="utf-8",
    )
    target_source = repo_root / "packages" / "app" / "files" / "config"
    if target_is_directory:
        target_source.mkdir()
        (target_source / "example.conf").write_text("config\n", encoding="utf-8")
    else:
        target_source.write_text("config\n", encoding="utf-8")
    (repo_root / "packages" / "app" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.config]",
                'source = "files/config"',
                'path = "~/.config/app/config"',
                *(target_manifest or []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    return repo_root


def load_manifest_repo(tmp_path: Path, repo_root: Path) -> DotmanEngine:
    config_path = write_single_repo_config(
        tmp_path,
        repo_name="fixture",
        repo_path=repo_root,
    )
    return DotmanEngine.from_config_path(config_path)


def test_target_rejects_unsupported_schema_fields(tmp_path: Path) -> None:
    repo_root = write_manifest_repo(
        tmp_path,
        target_manifest=['unexpected = "value"'],
    )

    with pytest.raises(
        ValueError,
        match=r"target 'config' has unsupported keys: unexpected",
    ):
        load_manifest_repo(tmp_path, repo_root)


def test_target_path_rule_rejects_unsupported_schema_fields(tmp_path: Path) -> None:
    repo_root = write_manifest_repo(
        tmp_path,
        target_manifest=[
            "",
            "[targets.config.path_rules.rule]",
            'pattern = "*.conf"',
            'unexpected = "value"',
        ],
    )

    with pytest.raises(
        ValueError,
        match=r"target 'config' path_rules.rule has unsupported keys: unexpected",
    ):
        load_manifest_repo(tmp_path, repo_root)


def test_target_ignore_table_rejects_unsupported_schema_fields(tmp_path: Path) -> None:
    repo_root = write_manifest_repo(
        tmp_path,
        target_manifest=[
            "",
            "[targets.config.ignore]",
            'unexpected = ["*.tmp"]',
        ],
    )

    with pytest.raises(
        ValueError,
        match=r"target 'config' ignore has unsupported keys: unexpected",
    ):
        load_manifest_repo(tmp_path, repo_root)


def test_repo_ignore_table_rejects_unsupported_schema_fields(tmp_path: Path) -> None:
    repo_root = write_manifest_repo(
        tmp_path,
        repo_manifest=[
            "[ignore]",
            'unexpected = ["*.tmp"]',
        ],
    )

    with pytest.raises(
        ValueError,
        match=r"repo config .+ \[ignore\] has unsupported keys: unexpected",
    ):
        load_manifest_repo(tmp_path, repo_root)


def test_repo_config_rejects_unsupported_schema_fields(tmp_path: Path) -> None:
    repo_root = write_manifest_repo(
        tmp_path,
        repo_manifest=['unexpected = "value"'],
    )

    with pytest.raises(
        ValueError,
        match=r"repo config .+ has unsupported keys: unexpected",
    ):
        load_manifest_repo(tmp_path, repo_root)


def test_canonical_manifest_vocabulary_loads_unchanged(tmp_path: Path) -> None:
    live_target = Path.home() / ".config" / "app" / "config"
    live_target.mkdir(parents=True)
    (live_target / "example.conf").write_text("old config\n", encoding="utf-8")
    repo_root = write_manifest_repo(
        tmp_path,
        target_is_directory=True,
        repo_manifest=[
            '[hooks.pre_push]',
            'commands = [{ run = "echo ready", elevation = "root" }]',
            "",
            "[ignore]",
            'patterns = ["repo.one", "repo.two"]',
            'gitignore = true',
        ],
        target_manifest=[
            'type = "directory"',
            'render = "cat \\"$DOTMAN_REPO_PATH\\""',
            'compare = { repo = "render", live = "raw" }',
            "",
            "[targets.config.path_rules.rule]",
            'pattern = "*.conf"',
            'compare = { repo = "render", live = "raw" }',
            "",
            "[targets.config.ignore]",
            'patterns = ["target.one", "target.two"]',
        ],
    )

    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])

    engine = load_manifest_repo(tmp_path, repo_root)
    repo = engine.get_repo("fixture")

    assert repo.hooks["pre_push"].commands[0].elevation == "root"
    assert repo.ignore_defaults.patterns == ("repo.one", "repo.two")
    with engine.open_push_session(engine.resolve_sync_scope(), preview=True) as session:
        child, = session.view.observations
    assert child.identity.child_path == "example.conf"
    assert (child.compare_repo, child.compare_live) == ("render", "raw")


def test_ignore_schema_has_no_directional_or_anonymous_fields(tmp_path: Path) -> None:
    repo_root = write_manifest_repo(tmp_path)
    package_path = repo_root / "packages" / "app" / "package.toml"
    package_path.write_text(
        package_path.read_text(encoding="utf-8").replace(
            '[targets.config]',
            '[ignore]\npush = ["*.tmp"]\n\n[targets.config]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"package manifest .+ ignore has unsupported keys: push"):
        load_manifest_repo(tmp_path, repo_root)


def test_disabled_must_be_boolean(tmp_path: Path) -> None:
    # A quoted "false" is truthy, so accepting strings would silently disable the target.
    repo_root = write_manifest_repo(tmp_path, target_manifest=['disabled = "false"'])
    with pytest.raises(ValueError, match=r"target 'config' disabled must be a boolean"):
        load_manifest_repo(tmp_path, repo_root)
