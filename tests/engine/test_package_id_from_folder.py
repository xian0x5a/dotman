"""Package ids come from the manifest folder under packages/, never from an `id` field (issue #214)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dotman.cli import main
from dotman.engine import DotmanEngine
from tests.helpers import write_named_manager_config, write_single_repo_config


def _write_manifest(repo_root: Path, folder: str, text: str = "") -> Path:
    manifest_path = repo_root / "packages" / folder / "package.toml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(text, encoding="utf-8")
    return manifest_path


@pytest.mark.parametrize(
    ("folder", "id_line"),
    [
        pytest.param("git", 'id = "git"', id="id-matches-folder"),
        pytest.param("git", 'id = "other"', id="id-disagrees-with-folder"),
        pytest.param("work/git", 'id = "work/git"', id="namespaced-folder"),
        pytest.param("git", "id = 1", id="non-string-id"),
    ],
)
def test_manifest_that_still_defines_id_fails_with_actionable_error(
    tmp_path: Path, folder: str, id_line: str
) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = _write_manifest(repo_root, folder, f'{id_line}\ndescription = "legacy"\n')
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)

    with pytest.raises(ValueError) as error:
        DotmanEngine.from_config_path(config_path)

    message = str(error.value)
    assert str(manifest_path) in message
    assert "must not define id" in message
    assert f"'{folder}' comes from its folder" in message
    assert "remove the id line" in message


def test_package_ids_are_the_manifest_folder_paths(tmp_path: Path, capsys) -> None:
    repo_root = tmp_path / "repo"
    _write_manifest(repo_root, "git", 'description = "base"\n')
    _write_manifest(repo_root, "work/git", 'extends = ["git"]\n')
    _write_manifest(repo_root, "work/tools/nvim")
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main(["--config", str(config_path), "--json", "list", "trackables"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    selectors = {entry["qualified_selector"] for entry in payload["trackables"] if entry["kind"] == "package"}
    assert selectors == {"fixture:git", "fixture:work/git", "fixture:work/tools/nvim"}


@pytest.mark.parametrize(
    ("folder", "derived_id"),
    [
        pytest.param("work/git.ui", "work/git.ui", id="dot-in-namespaced-segment"),
        pytest.param("my pkg", "my pkg", id="whitespace"),
        pytest.param("git@work", "git@work", id="at-sign"),
        pytest.param("", ".", id="manifest-directly-under-packages"),
    ],
)
def test_folder_that_is_not_a_valid_package_id_is_rejected(
    tmp_path: Path, folder: str, derived_id: str
) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = _write_manifest(repo_root, folder)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)

    with pytest.raises(
        ValueError,
        match=rf"package manifest {re.escape(str(manifest_path))}: invalid package id '{re.escape(derived_id)}'",
    ):
        DotmanEngine.from_config_path(config_path)
