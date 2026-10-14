from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotman import projection
from dotman.engine import DotmanEngine
from tests.helpers import open_tracked_pull_session
from tests.helpers import write_single_repo_config, write_tracked_packages_state


def write_override_repo(
    repo_root: Path,
    *,
    operation: str,
    loser_marker: Path,
    probe_marker: Path | None = None,
) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "loser" / "files").mkdir(parents=True)
    (repo_root / "packages" / "loser-meta").mkdir(parents=True)
    (repo_root / "packages" / "winner" / "files").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "loser" / "files" / "shared.conf").write_text("loser\n", encoding="utf-8")
    (repo_root / "packages" / "winner" / "files" / "shared.conf").write_text("winner\n", encoding="utf-8")

    if operation == "push":
        loser_source = "files/shared.conf"
        winner_source = "files/shared.conf"
        loser_path = winner_path = "~/.config/shared.conf"
        side_effect_field = (
            "render = "
            + json.dumps(f'printf touched > {loser_marker}; cat "$DOTMAN_REPO_PATH"')
        )
        sync_policy = "push-only"
    else:
        loser_source = "files/shared.conf"
        winner_source = "../loser/files/shared.conf"
        loser_path = "~/.config/loser.conf"
        winner_path = "~/.config/winner.conf"
        side_effect_field = (
            "capture = "
            + json.dumps(f'printf touched > {loser_marker}; cat "$DOTMAN_LIVE_PATH"')
        )
        sync_policy = "pull-only"

    loser_lines = [
        "",
        "[targets.shared]",
        f'source = "{loser_source}"',
        f'path = "{loser_path}"',
        f'sync_policy = "{sync_policy}"',
        side_effect_field,
    ]
    if probe_marker is not None:
        loser_lines.extend(
            [
                "",
                "[targets.probe]",
                "probe = " + json.dumps(f"printf probe > {probe_marker}"),
                'sync_policy = "push-only"',
            ]
        )
    (repo_root / "packages" / "loser" / "package.toml").write_text(
        "\n".join([*loser_lines, ""]),
        encoding="utf-8",
    )
    (repo_root / "packages" / "loser-meta" / "package.toml").write_text(
        '\n'.join(['depends = ["loser"]', ""]),
        encoding="utf-8",
    )
    (repo_root / "packages" / "winner" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.shared]",
                f'source = "{winner_source}"',
                f'path = "{winner_path}"',
                f'sync_policy = "{sync_policy}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_conflicting_projection_repo(
    repo_root: Path,
    *,
    operation: str,
    markers: tuple[Path, Path],
) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "groups").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "groups" / "all.toml").write_text('members = ["alpha", "beta"]\n', encoding="utf-8")

    for package_id, marker in zip(("alpha", "beta"), markers, strict=True):
        package_root = repo_root / "packages" / package_id
        (package_root / "files").mkdir(parents=True)
        (package_root / "files" / "shared.conf").write_text(f"{package_id}\n", encoding="utf-8")
        if operation == "push":
            source = "files/shared.conf"
            live_path = "~/.config/shared.conf"
            projection_field = "render = " + json.dumps(
                f'printf touched > {marker}; cat "$DOTMAN_REPO_PATH"'
            )
            sync_policy = "push-only"
        else:
            source = "files/shared.conf" if package_id == "alpha" else "../alpha/files/shared.conf"
            live_path = f"~/.config/{package_id}.conf"
            projection_field = "capture = " + json.dumps(
                f'printf touched > {marker}; cat "$DOTMAN_LIVE_PATH"'
            )
            sync_policy = "pull-only"
        (package_root / "package.toml").write_text(
            "\n".join(
                [
                    "",
                    "[targets.shared]",
                    f'source = "{source}"',
                    f'path = "{live_path}"',
                    f'sync_policy = "{sync_policy}"',
                    projection_field,
                    "",
                ]
            ),
            encoding="utf-8",
        )


def write_nested_collision_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "groups").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "groups" / "all.toml").write_text('members = ["parent", "child"]\n', encoding="utf-8")
    parent_source = repo_root / "packages" / "parent" / "files" / "shared"
    child_source = repo_root / "packages" / "child" / "files"
    parent_source.mkdir(parents=True)
    child_source.mkdir(parents=True)
    (parent_source / "parent.conf").write_text("parent\n", encoding="utf-8")
    (child_source / "child.conf").write_text("child\n", encoding="utf-8")
    (repo_root / "packages" / "parent" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.shared]",
                'source = "files/shared"',
                'path = "~/.config/shared"',
                'type = "directory"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "packages" / "child" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.child]",
                'source = "files/child.conf"',
                'path = "~/.config/shared/child.conf"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_same_package_conflict_repo(repo_root: Path, *, operation: str) -> None:
    (repo_root / "profiles").mkdir(parents=True)
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (package_root / "files" / "a.conf").write_text("a\n", encoding="utf-8")
    (package_root / "files" / "b.conf").write_text("b\n", encoding="utf-8")
    if operation == "push":
        first_source, second_source = "files/a.conf", "files/b.conf"
        first_path = second_path = "~/.config/shared.conf"
        sync_policy = "push-only"
    else:
        first_source = second_source = "files/a.conf"
        first_path, second_path = "~/.config/a.conf", "~/.config/b.conf"
        sync_policy = "pull-only"
    (package_root / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.a]",
                f'source = "{first_source}"',
                f'path = "{first_path}"',
                f'sync_policy = "{sync_policy}"',
                "",
                "[targets.b]",
                f'source = "{second_source}"',
                f'path = "{second_path}"',
                f'sync_policy = "{sync_policy}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def open_tracked_session(engine: DotmanEngine, operation: str):
    return getattr(engine, f"open_{operation}_session")(engine.resolve_sync_scope(), preview=True)


@pytest.mark.parametrize("operation", ["push", "pull"])
def test_tracked_session_skips_overridden_target_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "loser.conf").write_text("live loser\n", encoding="utf-8")
    (home / ".config" / "winner.conf").write_text("live winner\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    loser_marker = tmp_path / "loser-projection-ran"
    probe_marker = tmp_path / "probe-ran" if operation == "push" else None
    repo_root = tmp_path / "repo"
    write_override_repo(
        repo_root,
        operation=operation,
        loser_marker=loser_marker,
        probe_marker=probe_marker,
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("loser-meta", "default"), ("winner", "default")],
    )
    engine = DotmanEngine.from_config_path(config_path)

    if operation == "pull":
        with open_tracked_pull_session(engine, tmp_path) as session:
            assert [(unit.identity.package_id, unit.identity.target_name) for unit in session.view.observations] == [("winner", "shared")]
        assert not loser_marker.exists()
        return

    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert session.execute().result.status == "completed"

    assert (home / ".config" / "shared.conf").read_text(encoding="utf-8") == "winner\n"
    # A non-path Probe target survives ownership even when its package lost every file target.
    assert probe_marker is not None and probe_marker.read_text(encoding="utf-8") == "probe"
    assert not loser_marker.exists()


def test_push_runs_hooks_only_for_packages_with_winning_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    write_override_repo(repo_root, operation="push", loser_marker=tmp_path / "loser-projection-ran")
    hook_log = tmp_path / "hooks.log"
    for package_id in ("loser", "loser-meta", "winner"):
        manifest = repo_root / "packages" / package_id / "package.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f'\n[hooks]\npre_push = "echo {package_id} >> {hook_log}"\n',
            encoding="utf-8",
        )
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("loser-meta", "default"), ("winner", "default")],
    )
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))

    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert session.execute().result.status == "completed"

    assert hook_log.read_text(encoding="utf-8").splitlines() == ["winner"]


@pytest.mark.parametrize("operation", ["push", "pull"])
def test_tracked_scope_rejects_static_conflict_before_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "alpha.conf").write_text("live alpha\n", encoding="utf-8")
    (home / ".config" / "beta.conf").write_text("live beta\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    markers = (tmp_path / "alpha-projection-ran", tmp_path / "beta-projection-ran")
    repo_root = tmp_path / "repo"
    write_conflicting_projection_repo(repo_root, operation=operation, markers=markers)
    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("alpha", "default"), ("beta", "default")])

    with pytest.raises(ValueError, match="conflicting explicit tracked targets"):
        open_tracked_session(engine, operation)

    assert not any(marker.exists() for marker in markers)


@pytest.mark.parametrize("operation", ["push", "pull"])
def test_tracked_scope_rejects_same_package_target_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "a.conf").write_text("live a\n", encoding="utf-8")
    (home / ".config" / "b.conf").write_text("live b\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    write_same_package_conflict_repo(repo_root, operation=operation)
    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])

    with pytest.raises(
        ValueError,
        match=r"fixture:app@default -> fixture:app\.a, fixture:app@default -> fixture:app\.b",
    ):
        open_tracked_session(engine, operation)


def test_push_does_not_scan_overridden_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    loser_source = repo_root / "packages" / "loser" / "files" / "shared"
    winner_source = repo_root / "packages" / "winner" / "files" / "shared"
    loser_source.mkdir(parents=True)
    winner_source.mkdir(parents=True)
    (winner_source / "winner.conf").write_text("winner\n", encoding="utf-8")
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (external_dir / "hidden.conf").write_text("hidden\n", encoding="utf-8")
    (loser_source / "nested-link").symlink_to(external_dir, target_is_directory=True)
    (repo_root / "packages" / "loser-meta").mkdir(parents=True)

    for package_id in ("loser", "winner"):
        (repo_root / "packages" / package_id / "package.toml").write_text(
            "\n".join(
                [
                    "",
                    "[targets.shared]",
                    'source = "files/shared"',
                    'path = "~/.config/shared"',
                    'type = "directory"',
                    'sync_policy = "push-only"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
    (repo_root / "packages" / "loser-meta" / "package.toml").write_text(
        '\n'.join(['depends = ["loser"]', ""]),
        encoding="utf-8",
    )

    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("loser-meta", "default"), ("winner", "default")],
    )
    engine = DotmanEngine.from_config_path(config_path)

    with engine.open_push_session(engine.resolve_sync_scope(), preview=True) as session:
        assert [unit.identity.canonical for unit in session.view.observations] == ["fixture:winner.shared/winner.conf"]


def test_tracked_scope_rejects_nested_collision_without_disabled_git_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    write_nested_collision_repo(repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("parent", "default"), ("child", "default")])
    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    def fail_gitignore_scan(*args, **kwargs):
        raise AssertionError("disabled Git controls must not be read")

    monkeypatch.setattr(projection, "collect_gitignore_chain", fail_gitignore_scan)

    with pytest.raises(ValueError, match="incompatible nested targets"):
        engine.resolve_sync_scope()


@pytest.mark.parametrize("operation", ["push", "pull"])
def test_tracked_scope_normalizes_static_paths_without_resolving_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "loser.conf").write_text("live loser\n", encoding="utf-8")
    (home / ".config" / "winner.conf").write_text("live winner\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    write_override_repo(
        repo_root,
        operation=operation,
        loser_marker=tmp_path / "loser-projection-ran",
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("loser-meta", "default"), ("winner", "default")],
    )
    engine = DotmanEngine.from_config_path(config_path)

    def fail_resolve(_path: Path, *_args, **_kwargs) -> Path:
        raise AssertionError("Path.resolve made target ownership depend on filesystem state")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    # Scope ownership is static; opening a session subsequently accesses endpoints.
    assert engine.resolve_sync_scope().targets
