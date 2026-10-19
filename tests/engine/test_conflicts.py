from __future__ import annotations

from pathlib import Path

import pytest

from dotman.engine import DotmanEngine
from tests.helpers import open_tracked_pull_session
from tests.helpers import (
    write_manager_config,
    write_profile_ambiguous_dependency_repo,
    write_single_repo_config,
    write_tracked_packages_state,
    write_untrack_conflict_repo,
    untrack_entry,
)


def write_same_live_path_repo(repo_root: Path, *, same_source_bytes: bool = True) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "beta" / "files").mkdir(parents=True)
    (repo_root / "packages" / "alpha-meta").mkdir(parents=True)
    (repo_root / "packages" / "beta-meta").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "files" / "shared.conf").write_text("same\n", encoding="utf-8")
    (repo_root / "packages" / "beta" / "files" / "shared.conf").write_text(
        "same\n" if same_source_bytes else "different\n",
        encoding="utf-8",
    )
    for package_id in ("alpha", "beta"):
        (repo_root / "packages" / package_id / "package.toml").write_text(
            "\n".join(
                [
                    "",
                    "[targets.shared]",
                    'source = "files/shared.conf"',
                    'path = "~/.config/shared.conf"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (repo_root / "packages" / f"{package_id}-meta" / "package.toml").write_text(
            "\n".join(
                [
                    f'depends = ["{package_id}"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )


def write_static_multi_instance_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "profiled" / "files").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "profiled" / "files" / "managed.conf").write_text("same\n", encoding="utf-8")
    (repo_root / "packages" / "profiled" / "package.toml").write_text(
        "\n".join(
            [
                'binding_mode = "multi_instance"',
                "",
                "[targets.managed]",
                'source = "files/managed.conf"',
                'path = "~/.config/profiled.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_nested_pull_only_repo(repo_root: Path, *, nested_repo_paths: bool) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "groups").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "groups" / "all.toml").write_text('members = ["parent", "child"]\n', encoding="utf-8")
    (repo_root / "packages" / "parent" / "files" / "config").mkdir(parents=True)
    (repo_root / "packages" / "child" / "files").mkdir(parents=True)
    child_source = "files/generated.conf" if not nested_repo_paths else "../parent/files/config/generated.conf"
    child_live_path = "~/.config/example/generated.conf" if not nested_repo_paths else "~/.config/generated.conf"
    (repo_root / "packages" / "child" / "files" / "generated.conf").write_text("child\n", encoding="utf-8")
    if nested_repo_paths:
        (repo_root / "packages" / "parent" / "files" / "config" / "generated.conf").write_text("child\n", encoding="utf-8")

    (repo_root / "packages" / "parent" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.config]",
                'source = "files/config"',
                'path = "~/.config/example"',
                'sync_policy = "pull-only"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "child" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.generated]",
                f'source = "{child_source}"',
                f'path = "{child_live_path}"',
                'sync_policy = "pull-only"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_same_package_nested_pull_only_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "app" / "files" / "config").mkdir(parents=True)
    (repo_root / "packages" / "app" / "files" / "generated").mkdir(parents=True)
    (repo_root / "packages" / "app" / "files" / "generated" / "generated.conf").write_text("child\n", encoding="utf-8")
    (repo_root / "packages" / "app" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.config]",
                'source = "files/config"',
                'path = "~/.config/example"',
                'sync_policy = "pull-only"',
                "",
                "[targets.generated]",
                'source = "files/generated/generated.conf"',
                'path = "~/.config/example/generated.conf"',
                'sync_policy = "pull-only"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_pull_only_same_live_path_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "beta" / "files").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "files" / "shared.conf").write_text("alpha\n", encoding="utf-8")
    (repo_root / "packages" / "beta" / "files" / "shared.conf").write_text("beta\n", encoding="utf-8")
    for package_id in ("alpha", "beta"):
        (repo_root / "packages" / package_id / "package.toml").write_text(
            "\n".join(
                [
                    "",
                    "[targets.shared]",
                    'source = "files/shared.conf"',
                    'path = "~/.config/shared.conf"',
                    'sync_policy = "pull-only"',
                    "",
                ]
            ),
            encoding="utf-8",
        )


def write_pull_only_same_repo_path_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "beta").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "files" / "shared.conf").write_text("shared\n", encoding="utf-8")
    for package_id, live_path in (("alpha", "~/.config/alpha.conf"), ("beta", "~/.config/beta.conf")):
        source = "files/shared.conf" if package_id == "alpha" else "../alpha/files/shared.conf"
        (repo_root / "packages" / package_id / "package.toml").write_text(
            "\n".join(
                [
                    "",
                    "[targets.shared]",
                    f'source = "{source}"',
                    f'path = "{live_path}"',
                    'sync_policy = "pull-only"',
                    "",
                ]
            ),
            encoding="utf-8",
        )


def test_pull_allows_nested_live_paths_within_package_when_repo_paths_do_not_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "example").mkdir(parents=True)
    (home / ".config" / "example" / "generated.conf").write_text("child\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_same_package_nested_pull_only_repo(repo_root)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    with open_tracked_pull_session(engine, tmp_path, entries=[("app", "default")]) as session:
        assert {unit.identity.target_name for unit in session.view.observations} == {"config", "generated"}


def test_pull_allows_nested_live_paths_when_repo_paths_do_not_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "example").mkdir(parents=True)
    (home / ".config" / "example" / "generated.conf").write_text("child\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_nested_pull_only_repo(repo_root, nested_repo_paths=False)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    with open_tracked_pull_session(engine, tmp_path, entries=[("parent", "default"), ("child", "default")]) as session:
        assert {unit.identity.package_id for unit in session.view.observations} == {"parent", "child"}


def test_pull_rejects_nested_repo_paths_even_when_live_paths_do_not_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "example").mkdir(parents=True)
    (home / ".config" / "generated.conf").write_text("child\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_nested_pull_only_repo(repo_root, nested_repo_paths=True)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    with pytest.raises(ValueError, match=r"incompatible nested targets: fixture:parent\.config contains fixture:child\.generated"):
        open_tracked_pull_session(engine, tmp_path, entries=[("parent", "default"), ("child", "default")])


def test_tracked_pull_allows_same_live_path_when_repo_paths_do_not_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "shared.conf").write_text("live\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_pull_only_same_live_path_repo(repo_root)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("alpha", "default"), ("beta", "default")],
    )
    engine = DotmanEngine.from_config_path(config_path)

    with open_tracked_pull_session(engine, tmp_path) as session:
        assert [unit.identity.package_id for unit in session.view.observations] == ["alpha", "beta"]


def test_tracked_pull_rejects_same_repo_path_when_live_paths_do_not_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "alpha.conf").write_text("alpha\n", encoding="utf-8")
    (home / ".config" / "beta.conf").write_text("beta\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_pull_only_same_repo_path_repo(repo_root)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("alpha", "default"), ("beta", "default")],
    )
    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(
        ValueError,
        match=r"conflicting explicit tracked targets for .+shared\.conf: fixture:alpha@default -> fixture:alpha\.shared, fixture:beta@default -> fixture:beta\.shared",
    ):
        engine.resolve_sync_scope()


def test_record_binding_allows_pull_only_same_live_path_when_repo_paths_do_not_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_pull_only_same_live_path_repo(repo_root)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    for selector_text in ("fixture:alpha@default", "fixture:beta@default"):
        _repo, selector = engine.resolve_full_spec_selector_text(selector_text)
        engine.record_tracked_package_entry(selector)

    assert [(entry.selector, entry.profile) for entry in engine.read_tracked_package_entries(engine.get_repo("fixture"))] == [
        ("alpha", "default"),
        ("beta", "default"),
    ]


def test_record_binding_rejects_pull_only_same_repo_path_when_live_paths_do_not_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_pull_only_same_repo_path_repo(repo_root)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    _repo, selector = engine.resolve_full_spec_selector_text("fixture:alpha@default")
    engine.record_tracked_package_entry(selector)

    with pytest.raises(
        ValueError,
        match=r"conflicting explicit tracked targets for .+shared\.conf: fixture:alpha@default -> fixture:alpha\.shared, fixture:beta@default -> fixture:beta\.shared",
    ):
        _repo, selector = engine.resolve_full_spec_selector_text("fixture:beta@default")
        engine.record_tracked_package_entry(selector)


@pytest.mark.parametrize(
    ("beta_binding_mode", "beta_label"),
    [("singleton", r"fixture:beta\.beta"), ("multi_instance", r"fixture:beta<default>\.beta")],
)
def test_package_reserved_paths_conflict_with_other_package_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    beta_binding_mode: str,
    beta_label: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "beta" / "files").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files" / "alpha.conf").write_text("alpha = 1\n", encoding="utf-8")
    (repo_root / "packages" / "beta" / "files" / "beta.conf").write_text("beta = 1\n", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "package.toml").write_text(
        "\n".join(
            [
                'reserved_paths = ["~/.config/shared"]',
                "",
                "[targets.alpha]",
                'source = "files/alpha.conf"',
                'path = "~/.config/alpha/alpha.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "beta" / "package.toml").write_text(
        "\n".join(
            [
                f'binding_mode = "{beta_binding_mode}"',
                "[targets.beta]",
                'source = "files/beta.conf"',
                'path = "~/.config/shared/beta.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("alpha", "default"), ("beta", "default")])
    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(
        ValueError,
        match=rf"reserved path conflict: fixture:alpha reserves .+shared and {beta_label} maps to .+shared/beta\.conf",
    ):
        engine.resolve_sync_scope()


def test_package_reserved_paths_conflict_with_other_package_reserved_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "alpha").mkdir(parents=True)
    (repo_root / "packages" / "beta").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "package.toml").write_text(
        "\n".join(
            [
                'reserved_paths = ["~/.cache/shared"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "beta" / "package.toml").write_text(
        "\n".join(
            [
                'reserved_paths = ["~/.cache/shared/session"]',
                "",
            ]
        ),
        encoding="utf-8",
    )

    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("alpha", "default"), ("beta", "default")])
    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(
        ValueError,
        match=r"reserved path conflict: fixture:alpha reserves .+shared and fixture:beta reserves .+shared/session",
    ):
        engine.resolve_sync_scope()


def write_multi_instance_reserved_path_repo(repo_root: Path, *, reserved_path: str, target_path: str) -> None:
    """Write a `multi_instance` package `app` with `home` and `office` profiles.

    Each profile names the other one as `vars.peer`, so a target path can point at
    the peer instance's area. Sources are per profile so the instances never share
    a Primary Source, which would trip an unrelated ownership check.
    """
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "app" / "files").mkdir(parents=True)
    for profile_name, peer_name in (("home", "office"), ("office", "home")):
        (repo_root / "profiles" / f"{profile_name}.toml").write_text(
            f'[vars]\npeer = "{peer_name}"\n',
            encoding="utf-8",
        )
        (repo_root / "packages" / "app" / "files" / f"{profile_name}.conf").write_text(
            f"profile={profile_name}\n",
            encoding="utf-8",
        )
    (repo_root / "packages" / "app" / "package.toml").write_text(
        "\n".join(
            [
                'binding_mode = "multi_instance"',
                f'reserved_paths = ["{reserved_path}"]',
                "",
                "[targets.config]",
                'source = "files/{{ profile }}.conf"',
                f'path = "{target_path}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("reserved_path", "target_path"),
    [
        pytest.param(
            "~/.local/share/app",
            "~/.config/app/{{ profile }}.conf",
            id="same-reserved-path",
        ),
        pytest.param(
            "~/.local/share/app/{{ profile }}",
            "~/.local/share/app/{{ vars.peer }}/app.conf",
            id="target-inside-peer-reserved-path",
        ),
    ],
)
def test_package_reserved_paths_conflict_between_instances_of_same_multi_instance_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reserved_path: str,
    target_path: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_multi_instance_reserved_path_repo(repo_root, reserved_path=reserved_path, target_path=target_path)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "home"), ("app", "office")])
    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(ValueError, match=r"^reserved path conflict") as exc_info:
        engine.resolve_sync_scope()

    message = str(exc_info.value)
    assert "app<home>" in message
    assert "app<office>" in message


def test_package_reserved_paths_allow_each_instance_own_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_multi_instance_reserved_path_repo(
        repo_root,
        reserved_path="~/.local/share/app/{{ profile }}",
        target_path="~/.local/share/app/{{ profile }}/app.conf",
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "home"), ("app", "office")])
    engine = DotmanEngine.from_config_path(config_path)

    scope = engine.resolve_sync_scope()

    assert sorted(target.canonical for target in scope.targets) == [
        "fixture:app<home>.config",
        "fixture:app<office>.config",
    ]
    # Tracked-target lookup validates each selection's own declarations too.
    _query, exact_matches, _partial_matches = engine.find_tracked_target_matches("app<home>.config")
    assert [(match.package_id, match.bound_profile, match.target_name) for match in exact_matches] == [
        ("app", "home", "config"),
    ]


def test_record_binding_rejects_conflicting_explicit_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    engine = DotmanEngine.from_config_path(write_manager_config(tmp_path))

    _repo, selector = engine.resolve_full_spec_selector_text("example:git@basic")
    engine.record_tracked_package_entry(selector)

    with pytest.raises(
        ValueError,
        match=r"conflicting explicit tracked targets for .+\.gitconfig: example:git@basic -> example:git\.gitconfig, example:work/git@work -> example:work/git\.gitconfig",
    ):
        _repo, selector = engine.resolve_full_spec_selector_text("example:work/git@work")
        engine.record_tracked_package_entry(selector)


def test_record_binding_rejects_same_signature_explicit_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_same_live_path_repo(repo_root, same_source_bytes=True)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    _repo, selector = engine.resolve_full_spec_selector_text("fixture:alpha@basic")
    engine.record_tracked_package_entry(selector)

    with pytest.raises(
        ValueError,
        match=r"conflicting explicit tracked targets for .+shared\.conf: fixture:alpha@basic -> fixture:alpha\.shared, fixture:beta@basic -> fixture:beta\.shared",
    ):
        _repo, selector = engine.resolve_full_spec_selector_text("fixture:beta@basic")
        engine.record_tracked_package_entry(selector)


def test_record_binding_rejects_same_signature_implicit_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_same_live_path_repo(repo_root, same_source_bytes=True)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    _repo, selector = engine.resolve_full_spec_selector_text("fixture:alpha-meta@basic")
    engine.record_tracked_package_entry(selector)

    with pytest.raises(
        ValueError,
        match=r"conflicting implicit tracked targets for .+shared\.conf: fixture:alpha@basic -> fixture:alpha\.shared, fixture:beta@basic -> fixture:beta\.shared",
    ):
        _repo, selector = engine.resolve_full_spec_selector_text("fixture:beta-meta@basic")
        engine.record_tracked_package_entry(selector)


def test_record_binding_rejects_multi_instance_same_live_path_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_static_multi_instance_repo(repo_root)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    _repo, selector = engine.resolve_full_spec_selector_text("fixture:profiled@basic")
    engine.record_tracked_package_entry(selector)

    with pytest.raises(
        ValueError,
        match=r"conflicting explicit tracked targets for .+profiled\.conf: fixture:profiled@basic -> fixture:profiled<basic>\.managed, fixture:profiled@work -> fixture:profiled<work>\.managed",
    ):
        _repo, selector = engine.resolve_full_spec_selector_text("fixture:profiled@work")
        engine.record_tracked_package_entry(selector)


def test_track_rejects_singleton_implicit_dependency_profile_ambiguity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_profile_ambiguous_dependency_repo(repo_root)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    _repo, selector = engine.resolve_full_spec_selector_text("fixture:meta-a@basic")
    engine.record_tracked_package_entry(selector)

    with pytest.raises(
        ValueError,
        match=(
            r"ambiguous implicit profile contexts for fixture:shared:\n"
            r"  fixture:shared@basic required by fixture:meta-a@basic\n"
            r"  fixture:shared@work required by fixture:meta-b@work"
        ),
    ):
        _repo, selector = engine.resolve_full_spec_selector_text("fixture:meta-b@work")
        engine.record_tracked_package_entry(selector)


def test_tracked_scope_fails_for_invalid_singleton_implicit_dependency_profile_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_profile_ambiguous_dependency_repo(repo_root)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("meta-a", "basic"), ("meta-b", "work")],
    )
    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(ValueError, match=r"ambiguous implicit profile contexts for fixture:shared"):
        engine.resolve_sync_scope()


def test_same_profile_singleton_implicit_dependency_dedupes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_profile_ambiguous_dependency_repo(repo_root)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    for selector_text in ("fixture:meta-a@basic", "fixture:meta-b@basic"):
        _repo, selector = engine.resolve_full_spec_selector_text(selector_text)
        engine.record_tracked_package_entry(selector)

    scope = engine.resolve_sync_scope()

    assert [(item.package_id, item.requested_profile) for item in scope.package_selections] == [
        ("shared", "basic"),
        ("meta-a", "basic"),
        ("meta-b", "basic"),
    ]


def test_multi_instance_implicit_dependency_allows_different_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_profile_ambiguous_dependency_repo(repo_root, shared_binding_mode="multi_instance")
    # Sync requires exclusive Primary Sources across instances, so give each profile its own source.
    shared_root = repo_root / "packages" / "shared"
    for profile_name in ("basic", "work"):
        (shared_root / "files" / f"{profile_name}.conf").write_text(f"profile={profile_name}\n", encoding="utf-8")
    manifest = shared_root / "package.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("files/shared.conf", "files/{{ profile }}.conf"),
        encoding="utf-8",
    )
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    for selector_text in ("fixture:meta-a@basic", "fixture:meta-b@work"):
        _repo, selector = engine.resolve_full_spec_selector_text(selector_text)
        engine.record_tracked_package_entry(selector)

    scope = engine.resolve_sync_scope()

    assert [(item.package_id, item.bound_profile, item.requested_profile) for item in scope.package_selections] == [
        ("shared", "basic", "basic"),
        ("meta-a", None, "basic"),
        ("shared", "work", "work"),
        ("meta-b", None, "work"),
    ]


def test_push_publishes_explicit_singleton_dependency_profile_over_conflicting_implicit_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_profile_ambiguous_dependency_repo(repo_root)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("shared", "work"), ("meta-a", "basic"), ("meta-b", "work")],
    )
    engine = DotmanEngine.from_config_path(config_path)

    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert session.execute().result.status == "completed"

    assert sorted(path.name for path in (home / ".config" / "shared").iterdir()) == ["work.conf"]
    assert (home / ".config" / "shared" / "work.conf").read_text(encoding="utf-8") == "profile=work\n"


def test_remove_binding_rejects_resulting_singleton_dependency_profile_ambiguity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_untrack_conflict_repo(repo_root)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    original_state = "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "shared"',
            'profile = "direct"',
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "stack-a"',
            'profile = "work"',
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "stack-b"',
            'profile = "personal"',
            "",
        ]
    )
    (state_dir / "tracked-packages.toml").write_text(original_state, encoding="utf-8")

    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(ValueError, match=r"ambiguous implicit profile contexts for fixture:shared"):
        untrack_entry(engine, "fixture:shared@direct")

    assert (state_dir / "tracked-packages.toml").read_text(encoding="utf-8") == original_state


def test_info_names_the_package_instance_in_live_path_type_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / ".config" / "app.conf").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "app" / "files").mkdir(parents=True)
    (repo_root / "packages" / "app" / "files" / "app.conf").write_text("x\n", encoding="utf-8")
    # Pull-only skips the push ownership check, so info's own target summary reports the mismatch.
    (repo_root / "packages" / "app" / "package.toml").write_text(
        'binding_mode = "multi_instance"\n[targets.cfg]\nsource = "files/app.conf"\n'
        'path = "~/.config/app.conf"\ntype = "file"\nsync_policy = "pull-only"\n',
        encoding="utf-8",
    )
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    with pytest.raises(ValueError, match=r"target 'fixture:app<default>\.cfg' declares type"):
        engine.describe_tracked_package("fixture:app")
