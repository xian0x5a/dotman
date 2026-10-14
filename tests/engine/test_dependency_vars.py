from __future__ import annotations

from pathlib import Path

import pytest

from dotman.sync_observation import _resolve_inputs
from tests.engine.test_sync_scope import _engine
from tests.helpers import write_tracked_packages_state


def _write_repo(repo_root: Path, *, owners: dict[str, str]) -> None:
    """`shared` reads `vars.where`; each owner depends on it and sets its own value."""
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    shared_root = repo_root / "packages" / "shared" / "files"
    shared_root.mkdir(parents=True)
    (shared_root / "value.conf").write_text("shared\n", encoding="utf-8")
    (repo_root / "packages" / "shared" / "package.toml").write_text(
        '[vars]\nwhere = "own"\n\n'
        '[targets.shared]\nsource = "files/value.conf"\npath = "~/.config/{{ vars.where }}.conf"\n',
        encoding="utf-8",
    )
    for owner, where in owners.items():
        (repo_root / "packages" / owner).mkdir(parents=True)
        (repo_root / "packages" / owner / "package.toml").write_text(
            f'depends = ["shared"]\n\n[vars]\nwhere = "{where}"\n',
            encoding="utf-8",
        )


def _info_live_paths(engine) -> set[str]:
    detail = engine.describe_tracked_package("main:shared")
    return {owned.target.live_path.name for owned in detail.owned_targets}


def _sync_live_paths(engine) -> set[str]:
    inputs, _ = _resolve_inputs(engine.resolve_sync_scope())
    return {metadata.live_path.name for _item, metadata in inputs.values()}


def test_dependency_renders_with_its_own_vars_not_its_dependents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, owners={"meta": "owner"})
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("meta", "default")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    assert _info_live_paths(engine) == {"own.conf"}
    assert _sync_live_paths(engine) == {"own.conf"}


def test_dependency_shared_by_two_dependents_renders_one_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, owners={"meta-a": "a", "meta-b": "b"})
    write_tracked_packages_state(
        tmp_path / "state", repo_name="main", entries=[("meta-a", "default"), ("meta-b", "default")],
    )
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    assert _info_live_paths(engine) == {"own.conf"}
    assert _sync_live_paths(engine) == {"own.conf"}
