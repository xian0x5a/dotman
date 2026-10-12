"""The removed `lease` and `broker` elevation modes fail validation on every elevation surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from dotman.engine import DotmanEngine
from tests.helpers import write_single_repo_config

REMOVED_MODES = ("lease", "broker")


def _write_repo(
    tmp_path: Path,
    *,
    repo_manifest: list[str] = (),
    package_manifest: list[str] = (),
    target_manifest: list[str] = (),
) -> Path:
    repo_root = tmp_path / "repo"
    source_root = repo_root / "packages" / "app" / "files" / "config"
    source_root.mkdir(parents=True)
    (source_root / "settings.txt").write_text("value\n", encoding="utf-8")
    (repo_root / "profiles").mkdir()
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "repo.toml").write_text("\n".join([*repo_manifest, ""]), encoding="utf-8")
    (repo_root / "packages" / "app" / "package.toml").write_text(
        "\n".join(
            [
                'id = "app"',
                *package_manifest,
                "",
                "[targets.config]",
                'source = "files/config"',
                'path = "~/.config/app"',
                *target_manifest,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return repo_root


def _load_package(tmp_path: Path, repo_root: Path) -> None:
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))
    engine.get_repo("fixture").resolve_package("app")


def _command(mode: str) -> str:
    return f'{{ run = "true", elevation = "{mode}" }}'


SURFACES = {
    "repo-hook": lambda mode: {"repo_manifest": ["[hooks]", f"pre_push = {_command(mode)}"]},
    "repo-guard": lambda mode: {"repo_manifest": ["[hooks]", f"guard_push = {_command(mode)}"]},
    "package-hook": lambda mode: {"package_manifest": ["[hooks]", f"post_pull = [{_command(mode)}]"]},
    "package-guard": lambda mode: {"package_manifest": ["[hooks]", f"guard_pull = {_command(mode)}"]},
    "target-hook": lambda mode: {"target_manifest": ["[targets.config.hooks]", f"pre_push = {_command(mode)}"]},
    "target-guard": lambda mode: {"target_manifest": ["[targets.config.hooks]", f"guard_push = {_command(mode)}"]},
    "path-rule-guard": lambda mode: {
        "target_manifest": [
            "[targets.config.path_rules.text]",
            'pattern = "*.txt"',
            f"hooks = {{ guard_push = {_command(mode)} }}",
        ]
    },
    "target-editor": lambda mode: {"target_manifest": [f'editor = {{ run = "vim", elevation = "{mode}" }}']},
    "path-rule-editor": lambda mode: {
        "target_manifest": [
            "[targets.config.path_rules.text]",
            'pattern = "*.txt"',
            f'editor = {{ run = "vim", elevation = "{mode}" }}',
        ]
    },
}


@pytest.mark.parametrize("mode", REMOVED_MODES)
@pytest.mark.parametrize("surface", SURFACES)
def test_removed_elevation_modes_are_rejected_with_allowed_values(tmp_path: Path, surface: str, mode: str) -> None:
    repo_root = _write_repo(tmp_path, **SURFACES[surface](mode))

    with pytest.raises(
        ValueError,
        match=rf"unsupported (editor\.)?elevation '{mode}'; expected one of: none, root, intercept",
    ):
        _load_package(tmp_path, repo_root)
