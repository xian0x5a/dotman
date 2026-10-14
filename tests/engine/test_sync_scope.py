from __future__ import annotations

from pathlib import Path

import pytest

from dotman.engine import DotmanEngine
from dotman.models import ResolvedSyncScope
from dotman.sync_scope import sync_unit_identity_bytes
from tests.helpers import write_named_manager_config, write_tracked_packages_state


def _write_repo(repo_root: Path, *, repo_name: str, package_id: str = "app", multi_instance: bool = False) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    package_root = repo_root / "packages" / package_id / "files"
    package_root.mkdir(parents=True)
    (package_root / "config.conf").write_text("config\n", encoding="utf-8")
    (package_root / "settings").mkdir()
    (package_root / "settings" / "nested.conf").write_text("nested\n", encoding="utf-8")
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    binding = 'binding_mode = "multi_instance"\n' if multi_instance else ""
    (repo_root / "packages" / package_id / "package.toml").write_text(
        "\n".join(
            [
                binding.rstrip(),
                "",
                "[targets.config]",
                'source = "files/config.conf"',
                f'path = "~/.config/{package_id}/config.conf"',
                "",
                "[targets.settings]",
                'source = "files/settings"',
                f'path = "~/.config/{package_id}/settings"',
                'type = "directory"',
            ]
        ),
        encoding="utf-8",
    )


def _engine(tmp_path: Path, repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> DotmanEngine:
    state_home = tmp_path / "state"
    config_path = write_named_manager_config(tmp_path, repos)
    # The resolver reads the same manager state location as normal commands.
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    return DotmanEngine.from_config_path(config_path)


def test_resolved_sync_scope_accepts_exact_target_and_directory_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope(["main:app.settings/nested.conf"])

    assert isinstance(scope, ResolvedSyncScope)
    assert [item.canonical for item in scope.targets] == ["main:app.settings/nested.conf"]
    assert [selection.package_id for selection in scope.package_selections] == ["app"]


def test_resolved_sync_scope_preserves_angle_brackets_in_directory_child_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    settings_root = repo_root / "packages" / "app" / "files" / "settings"
    (settings_root / "foo<bar>.txt").write_text("bracketed\n", encoding="utf-8")
    (settings_root / "foo<bar").write_text("unclosed\n", encoding="utf-8")
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    for child_name in ("foo<bar>.txt", "foo<bar"):
        scope = engine.resolve_sync_scope([f"main:app.settings/{child_name}"])
        assert scope.targets[0].canonical == f"main:app.settings/{child_name}"


def _write_base_dependency(repo_root: Path) -> None:
    """Add package `base` and make `app` depend on it."""
    base_root = repo_root / "packages" / "base" / "files"
    base_root.mkdir(parents=True)
    (base_root / "base.conf").write_text("base\n", encoding="utf-8")
    (repo_root / "packages" / "base" / "package.toml").write_text(
        '\n'.join(
            [
                "",
                "[targets.base]",
                'source = "files/base.conf"',
                'path = "~/.config/app/base.conf"',
            ]
        ),
        encoding="utf-8",
    )
    app_manifest = (repo_root / "packages" / "app" / "package.toml").read_text(encoding="utf-8")
    (repo_root / "packages" / "app" / "package.toml").write_text(
        'depends = ["base"]\n' + app_manifest,
        encoding="utf-8",
    )


def test_resolved_sync_scope_expands_dependencies_and_deduplicates_union(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    _write_base_dependency(repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope(["main:app", "main:app.config", "main:app.config"])

    assert scope.selectors == ("main:app", "main:app.config")
    assert [selection.package_id for selection in scope.package_selections] == ["base", "app"]
    assert [item.canonical for item in scope.targets] == [
        "main:base.base",
        "main:app.config",
        "main:app.settings",
    ]


def test_resolved_sync_scope_records_which_package_input_included_a_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    _write_base_dependency(repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope(["main:app"])

    # Keyed by package and target scope, so hook and probe rows resolve the same way as units.
    assert {scope_label: [identity.package_id for identity in via] for scope_label, via in scope.included_via.items()} == {
        "main:base": ["app"],
        "main:base.base": ["app"],
    }
    # A requested package, or a selector-less run, includes nothing through a dependency.
    assert engine.resolve_sync_scope(["main:app", "main:base"]).included_via == {}
    assert engine.resolve_sync_scope().included_via == {}


def test_resolved_sync_scope_target_input_leaves_its_package_dependencies_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    _write_base_dependency(repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope(["main:app.config"])

    # Naming one target is the narrowest scope: no dependency package, so none of its hooks either.
    assert [selection.package_id for selection in scope.package_selections] == ["app"]
    assert [item.canonical for item in scope.targets] == ["main:app.config"]


def test_resolved_sync_scope_without_dependencies_keeps_only_requested_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    _write_base_dependency(repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope(["main:app"], include_dependencies=False)

    assert [selection.package_id for selection in scope.package_selections] == ["app"]
    assert [item.canonical for item in scope.targets] == ["main:app.config", "main:app.settings"]
    assert scope.included_via == {}


def test_resolved_sync_scope_without_inputs_expands_tracked_state_across_repositories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _write_repo(alpha, repo_name="alpha")
    _write_repo(beta, repo_name="beta", package_id="other")
    write_tracked_packages_state(tmp_path / "state", repo_name="alpha", entries=[("app", "default")])
    write_tracked_packages_state(tmp_path / "state", repo_name="beta", entries=[("other", "default")])
    engine = _engine(tmp_path, {"alpha": alpha, "beta": beta}, monkeypatch)

    scope = engine.resolve_sync_scope()

    assert [(selection.identity.repo, selection.package_id) for selection in scope.package_selections] == [("alpha", "app"), ("beta", "other")]
    assert [item.canonical for item in scope.targets] == [
        "alpha:app.config",
        "alpha:app.settings",
        "beta:other.config",
        "beta:other.settings",
    ]


def test_resolved_sync_scope_rejects_noncanonical_or_group_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    (repo_root / "groups").mkdir()
    (repo_root / "groups" / "all.toml").write_text('members = ["app"]\n', encoding="utf-8")
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    with pytest.raises(ValueError, match="canonical"):
        engine.resolve_sync_scope(["app"])
    with pytest.raises(ValueError, match="group"):
        engine.resolve_sync_scope(["main:all"])
    with pytest.raises(ValueError, match="normalized POSIX"):
        engine.resolve_sync_scope(["main:app.settings/../nested.conf"])
    with pytest.raises(ValueError, match="directory target"):
        engine.resolve_sync_scope(["main:app.config/missing.conf"])


def test_resolved_sync_scope_accepts_multi_instance_profile_with_dot_and_roundtrips_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main", multi_instance=True)
    (repo_root / "profiles" / "work.v2.toml").write_text("", encoding="utf-8")
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "work.v2")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope(["main:app<work.v2>.settings"])

    assert scope.targets[0].canonical == "main:app<work.v2>.settings"
    assert scope.targets[0].bound_profile == "work.v2"
    assert sync_unit_identity_bytes(scope.targets[0]) == b"main:app<work.v2>.settings"

    package_scope = engine.resolve_sync_scope(["main:app<work.v2>"])
    assert {target.canonical for target in package_scope.targets} == {
        "main:app<work.v2>.config",
        "main:app<work.v2>.settings",
    }


def test_resolved_sync_scope_accepts_exact_multi_instance_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main", multi_instance=True)
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "work")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope(["main:app<work>.config"])

    assert scope.targets[0].canonical == "main:app<work>.config"
    assert scope.package_selections[0].bound_profile == "work"


def _write_policy_package(repo_root: Path, package_id: str, policy: str) -> None:
    package_root = repo_root / "packages" / package_id / "files"
    package_root.mkdir(parents=True)
    (package_root / "value.conf").write_text(package_id + "\n", encoding="utf-8")
    (repo_root / "packages" / package_id / "package.toml").write_text(
        "\n".join(
            [
                f'sync_policy = "{policy}"',
                "",
                f"[targets.{package_id}]",
                'source = "files/value.conf"',
                f'path = "~/.config/{package_id}.conf"',
            ]
        ),
        encoding="utf-8",
    )


def test_resolved_sync_scope_includes_targets_from_any_applicable_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    policies = ("push-only", "pull-only", "both", "push-only-delete")
    for package_id, policy in zip(("push", "pull", "both", "delete"), policies):
        _write_policy_package(repo_root, package_id, policy)
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="main",
        entries=[(package_id, "default") for package_id in ("push", "pull", "both", "delete")],
    )
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope()

    assert {target.package_id for target in scope.targets} == {"push", "pull", "both", "delete"}


def test_resolved_sync_scope_uses_explicit_singleton_profile_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    shared_root = repo_root / "packages" / "shared" / "files"
    shared_root.mkdir(parents=True)
    (shared_root / "value.conf").write_text("shared\n", encoding="utf-8")
    (repo_root / "packages" / "shared" / "package.toml").write_text(
        '[targets.shared]\nsource = "files/value.conf"\npath = "~/.config/shared.conf"\n',
        encoding="utf-8",
    )
    meta_root = repo_root / "packages" / "meta"
    meta_root.mkdir(parents=True)
    (meta_root / "package.toml").write_text(
        'depends = ["shared"]\n',
        encoding="utf-8",
    )
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="main",
        entries=[("meta", "basic"), ("shared", "work")],
    )
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope(["main:meta"])

    assert {selection.package_id for selection in scope.package_selections} == {"meta", "shared"}
    assert [target.canonical for target in scope.targets] == ["main:shared.shared"]

    shared = [selection for selection in scope.package_selections if selection.package_id == "shared"]
    assert len(shared) == 1
    assert shared[0].requested_profile == "work"
    assert [target.canonical for target in scope.targets] == ["main:shared.shared"]
    payload = scope.to_dict()
    assert "tracked_entries" not in payload
    assert all("owner_identity" not in package for package in payload["packages"])


def test_resolved_sync_scope_checks_collisions_outside_requested_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    for package_id in ("one", "two"):
        package_root = repo_root / "packages" / package_id / "files"
        package_root.mkdir(parents=True)
        (package_root / "value.conf").write_text(package_id + "\n", encoding="utf-8")
        (repo_root / "packages" / package_id / "package.toml").write_text(
            "\n".join(
                [
                    "",
                    f"[targets.{package_id}]",
                    'source = "files/value.conf"',
                    'path = "~/.config/shared.conf"',
                ]
            ),
            encoding="utf-8",
        )
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="main",
        entries=[("one", "default"), ("two", "default")],
    )
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    with pytest.raises(ValueError, match="conflicting"):
        engine.resolve_sync_scope(["main:one"])


def test_resolved_sync_scope_keeps_explicit_ownership_winner_over_dependency_loser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    for package_id in ("winner", "loser"):
        package_root = repo_root / "packages" / package_id / "files"
        package_root.mkdir(parents=True)
        (package_root / "value.conf").write_text(package_id + "\n", encoding="utf-8")
    (repo_root / "packages" / "winner" / "package.toml").write_text(
        '[targets.shared]\nsource = "files/value.conf"\npath = "~/.config/shared.conf"\n',
        encoding="utf-8",
    )
    (repo_root / "packages" / "loser" / "package.toml").write_text(
        'sync_policy = "push-only"\n\n[targets.shared]\nsource = "../winner/files/value.conf"\npath = "~/.config/shared.conf"\n',
        encoding="utf-8",
    )
    meta_root = repo_root / "packages" / "meta"
    meta_root.mkdir(parents=True)
    (meta_root / "package.toml").write_text(
        'depends = ["loser"]\n',
        encoding="utf-8",
    )
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="main",
        entries=[("winner", "default"), ("meta", "default")],
    )
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope()

    assert [target.canonical for target in scope.targets] == ["main:winner.shared"]


@pytest.mark.parametrize("other_policy", ["push-only", "push-only-delete", "pull-only", "both"])
def test_resolved_sync_scope_rejects_shared_primary_source_across_policies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_policy: str,
) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    manifest = repo_root / "packages/app/package.toml"
    manifest.write_text(
        'sync_policy = "push-only"\n' + manifest.read_text()
        + '\n[targets.other]\nsource = "files/config.conf"\n'
        + 'path = "~/.config/other.conf"\n'
        + f'sync_policy = "{other_policy}"\n',
    )
    write_tracked_packages_state(
        tmp_path / "state", repo_name="main", entries=[("app", "default")],
    )
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    # Even a narrow selection cannot make conflicting tracked ownership valid.
    with pytest.raises(ValueError, match="conflicting target ownership"):
        engine.resolve_sync_scope(["main:app.config"])


@pytest.mark.parametrize("excluded", [False, True])
def test_resolved_sync_scope_checks_nested_push_only_primary_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, excluded: bool,
) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    manifest = repo_root / "packages/app/package.toml"
    manifest.write_text(
        'sync_policy = "push-only"\n' + manifest.read_text()
        + ('\nignore = { patterns = ["nested.conf"] }\n' if excluded else "")
        + '\n[targets.other]\nsource = "files/settings/nested.conf"\n'
        + 'path = "~/.config/other.conf"\n',
    )
    write_tracked_packages_state(
        tmp_path / "state", repo_name="main", entries=[("app", "default")],
    )
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    if excluded:
        assert len(engine.resolve_sync_scope().targets) == 3
    else:
        with pytest.raises(ValueError, match="incompatible nested targets"):
            engine.resolve_sync_scope()


@pytest.mark.parametrize("text,expected", [
    ("app", ("app", None)),
    ("work/git.cfg", ("work/git.cfg", None)),
    ("main:app.dir/sub/leaf", ("main:app.dir", "sub/leaf")),
    ("app<work.v2>.dir/leaf", ("app<work.v2>.dir", "leaf")),
])
def test_split_scope_child_path_keeps_package_slashes(text, expected):
    from dotman.sync_scope import split_scope_child_path
    assert split_scope_child_path(text) == expected


def test_on_demand_targets_leave_package_scope_including_dependencies_unless_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, repo_name="main")
    (repo_root / "packages" / "base").mkdir()
    (repo_root / "packages" / "base" / "package.toml").write_text(
        '[targets.update]\nprobe = "true"\non_demand = true\n', encoding="utf-8",
    )
    app_manifest = repo_root / "packages" / "app" / "package.toml"
    app_manifest.write_text(
        'depends = ["base"]\n' + app_manifest.read_text(encoding="utf-8")
        + '\n[targets.check]\nprobe = "true"\non_demand = true\n',
        encoding="utf-8",
    )
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default")])
    engine = _engine(tmp_path, {"main": repo_root}, monkeypatch)

    scope = engine.resolve_sync_scope(["main:app"])

    assert [item.canonical for item in scope.targets] == ["main:app.config", "main:app.settings"]
    named = engine.resolve_sync_scope(["main:base.update"])
    assert [item.canonical for item in named.targets] == ["main:base.update"]
