from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from dotman.cli import main
from dotman.engine import DotmanEngine
from dotman.sync_session import SessionOpenFailed
from tests.helpers import open_tracked_pull_session, initialize_git_repository
from tests.helpers import write_named_manager_config, write_single_repo_config, write_tracked_packages_state


def _write_profile(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True, exist_ok=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")


def _write_repo_guard(repo_root: Path, *, operation: str, command: str) -> None:
    (repo_root / "repo.toml").write_text(
        f"[hooks]\nguard_{operation} = {json.dumps(command)}\n",
        encoding="utf-8",
    )


def _write_file_package(
    repo_root: Path,
    *,
    package_id: str,
    targets: list[tuple[str, str, str | None, str | None]],
    operation: str = "push",
    package_guard: str | None = None,
    depends: tuple[str, ...] = (),
) -> None:
    package_root = repo_root / "packages" / package_id
    (package_root / "files").mkdir(parents=True, exist_ok=True)
    lines = []
    if depends:
        lines.append(f"depends = {json.dumps(list(depends))}")
    for target_name, live_path, target_guard, projection_command in targets:
        source_name = f"{target_name}.txt"
        (package_root / "files" / source_name).write_text(f"{package_id}:{target_name}\n", encoding="utf-8")
        lines.extend(
            [
                "",
                f"[targets.{target_name}]",
                f'source = "files/{source_name}"',
                f"path = {json.dumps(live_path)}",
            ]
        )
        if projection_command is not None:
            projection_key = "render" if operation == "push" else "capture"
            lines.append(f"{projection_key} = {json.dumps(projection_command)}")
        if target_guard is not None:
            lines.extend(
                [
                    "",
                    f"[targets.{target_name}.hooks]",
                    f"guard_{operation} = {json.dumps(target_guard)}",
                ]
            )
    if package_guard is not None:
        lines.extend(["", "[hooks]", f"guard_{operation} = {json.dumps(package_guard)}"])
    package_root.joinpath("package.toml").write_text("\n".join([*lines, ""]), encoding="utf-8")


def _engine(tmp_path: Path, repo_root: Path) -> DotmanEngine:
    return DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )


def _open_push(engine: DotmanEngine, tmp_path: Path, entries: list[tuple[str, str]] | None = None):
    if entries is not None:
        write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=entries)
    return engine.open_push_session(engine.resolve_sync_scope(), preview=True)


def _guard_skips(session) -> list:
    return [row.guard_skip for row in session.view.rows if getattr(row, "kind", None) == "guard-skip"]


def _observed(session) -> list[str]:
    return [unit.identity.canonical for unit in session.view.observations]


def test_repo_guard_skip_short_circuits_lower_planning_and_sibling_repo_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    marker = tmp_path / "order"
    quoted_marker = shlex.quote(str(marker))

    skipped_repo = tmp_path / "skipped-repo"
    _write_profile(skipped_repo)
    _write_repo_guard(
        skipped_repo,
        operation="push",
        command=f"printf 'repo-skipped\\n' >> {quoted_marker}; printf 'wrong host\\n'; exit 100",
    )
    _write_file_package(
        skipped_repo,
        package_id="app",
        package_guard=f"printf 'package-skipped\\n' >> {quoted_marker}",
        targets=[
            (
                "config",
                "~/.config/skipped/config.txt",
                f"printf 'target-skipped\\n' >> {quoted_marker}",
                f"printf 'projection-skipped\\n' >> {quoted_marker}; cat \"$DOTMAN_REPO_PATH\"",
            )
        ],
    )

    admitted_repo = tmp_path / "admitted-repo"
    _write_profile(admitted_repo)
    _write_repo_guard(admitted_repo, operation="push", command=f"printf 'repo-admitted\\n' >> {quoted_marker}")
    _write_file_package(
        admitted_repo,
        package_id="app",
        package_guard=f"printf 'package-admitted\\n' >> {quoted_marker}",
        targets=[
            (
                "config",
                "~/.config/admitted/config.txt",
                f"printf 'target-admitted\\n' >> {quoted_marker}",
                f"printf 'projection-admitted\\n' >> {quoted_marker}; cat \"$DOTMAN_REPO_PATH\"",
            )
        ],
    )

    config_path = write_named_manager_config(
        tmp_path,
        {"skipped": skipped_repo, "admitted": admitted_repo},
    )
    write_tracked_packages_state(tmp_path / "state", repo_name="skipped", entries=[("app", "default")])
    write_tracked_packages_state(tmp_path / "state", repo_name="admitted", entries=[("app", "default")])

    with _open_push(DotmanEngine.from_config_path(config_path), tmp_path) as session:
        assert _observed(session) == ["admitted:app.config"]
        assert [(skip.scope_kind, skip.scope_label, skip.reason) for skip in _guard_skips(session)] == [
            ("repo", "skipped", "wrong host")
        ]

    assert marker.read_text(encoding="utf-8").splitlines() == [
        "repo-skipped",
        "repo-admitted",
        "package-admitted",
        "target-admitted",
        "projection-admitted",
    ]


def test_package_dependency_guard_skip_is_local_to_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    _write_file_package(
        repo_root,
        package_id="dependency",
        package_guard="printf 'dependency unavailable\\n'; exit 100",
        targets=[("dependency", "~/.config/dependency.txt", None, None)],
    )
    _write_file_package(
        repo_root,
        package_id="app",
        depends=("dependency",),
        targets=[("app", "~/.config/app.txt", None, None)],
    )

    with _open_push(_engine(tmp_path, repo_root), tmp_path, [("app", "default")]) as session:
        assert _observed(session) == ["fixture:app.app"]
        assert [skip.scope_label for skip in _guard_skips(session)] == ["fixture:dependency"]


def test_target_guard_skip_prevents_projection_and_directory_children_but_keeps_sibling_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    marker = tmp_path / "projection"
    _write_file_package(
        repo_root,
        package_id="app",
        targets=[
            (
                "skipped",
                "~/.config/skipped.txt",
                "printf 'target unavailable\\n'; exit 100",
                f"printf projected > {shlex.quote(str(marker))}; cat \"$DOTMAN_REPO_PATH\"",
            ),
            ("kept", "~/.config/kept.txt", None, None),
        ],
    )

    with _open_push(_engine(tmp_path, repo_root), tmp_path, [("app", "default")]) as session:
        assert _observed(session) == ["fixture:app.kept"]
        assert [skip.scope_label for skip in _guard_skips(session)] == ["fixture:app.skipped"]
    assert not marker.exists()

    directory_root = tmp_path / "directory-repo"
    _write_profile(directory_root)
    package_root = directory_root / "packages" / "app"
    (package_root / "files" / "tree").mkdir(parents=True)
    (package_root / "files" / "tree" / "item.txt").write_text("value\n", encoding="utf-8")
    package_root.joinpath("package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.tree]",
                'source = "files/tree"',
                'path = "~/.config/tree"',
                'type = "directory"',
                "",
                "[targets.tree.hooks]",
                'guard_push = "exit 100"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    directory_config_root = tmp_path / "directory-config"
    directory_config_root.mkdir()
    directory_engine = DotmanEngine.from_config_path(
        write_single_repo_config(directory_config_root, repo_name="fixture", repo_path=directory_root)
    )
    # Sync censuses directory children before Guards on purpose (ineligibility
    # cleanup must survive a Guard failure), so only child observation is omitted.
    with _open_push(directory_engine, directory_config_root, [("app", "default")]) as session:
        assert session.view.observations == ()
        assert [skip.scope_label for skip in _guard_skips(session)] == ["fixture:app.tree"]


def test_probe_target_guard_runs_before_probe_and_distinguishes_guard_skip_from_probe_noop(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    package_root = repo_root / "packages" / "app"
    package_root.mkdir(parents=True)
    guard_marker = tmp_path / "guard"
    skipped_probe_marker = tmp_path / "skipped-probe"
    noop_probe_marker = tmp_path / "noop-probe"
    package_root.joinpath("package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.skipped]",
                f'probe = "printf probe > {skipped_probe_marker}; exit 0"',
                "",
                "[targets.skipped.hooks]",
                f'guard_push = "printf guard > {guard_marker}; exit 100"',
                "",
                "[targets.noop]",
                f'probe = "printf probe > {noop_probe_marker}; exit 100"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    with _open_push(_engine(tmp_path, repo_root), tmp_path, [("app", "default")]) as session:
        # A probe no-op leaves no row; only the Guard skip explains omitted work.
        assert [(row.kind, row.scope) for row in session.view.rows] == [("guard-skip", "fixture:app.skipped")]

    assert guard_marker.read_text(encoding="utf-8") == "guard"
    assert not skipped_probe_marker.exists()
    assert noop_probe_marker.read_text(encoding="utf-8") == "probe"


def test_repo_and_target_pull_guards_use_same_hierarchy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    live_path = home / ".config" / "app.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("live\n", encoding="utf-8")
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    marker = tmp_path / "pull-order"
    quoted_marker = shlex.quote(str(marker))
    _write_repo_guard(repo_root, operation="pull", command=f"printf 'repo\\n' >> {quoted_marker}")
    _write_file_package(
        repo_root,
        package_id="app",
        operation="pull",
        targets=[
            (
                "config",
                "~/.config/app.txt",
                f"printf 'target\\n' >> {quoted_marker}; exit 100",
                None,
            )
        ],
    )

    with open_tracked_pull_session(_engine(tmp_path, repo_root), tmp_path, entries=[("app", "default")]) as session:
        assert session.view.observations == ()
    assert marker.read_text(encoding="utf-8").splitlines() == ["repo", "target"]


def test_repo_package_and_target_guards_are_deduplicated_per_session_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    (repo_root / "groups").mkdir()
    (repo_root / "groups" / "all.toml").write_text('members = ["meta-a", "meta-b"]\n', encoding="utf-8")
    marker = tmp_path / "runs"
    quoted_marker = shlex.quote(str(marker))
    _write_repo_guard(repo_root, operation="push", command=f"printf 'repo\\n' >> {quoted_marker}")
    _write_file_package(
        repo_root,
        package_id="app",
        package_guard=f"printf 'package\\n' >> {quoted_marker}",
        targets=[
            (
                "config",
                "~/.config/app.txt",
                f"printf 'target\\n' >> {quoted_marker}",
                None,
            )
        ],
    )
    for package_id in ("meta-a", "meta-b"):
        package_root = repo_root / "packages" / package_id
        package_root.mkdir(parents=True)
        package_root.joinpath("package.toml").write_text(
            'depends = ["app"]\n',
            encoding="utf-8",
        )
    engine = _engine(tmp_path, repo_root)

    for _ in range(2):
        with _open_push(engine, tmp_path, [("meta-a", "default"), ("meta-b", "default")]) as session:
            assert _observed(session) == ["fixture:app.config"]

    assert marker.read_text(encoding="utf-8").splitlines() == [
        "repo",
        "package",
        "target",
        "repo",
        "package",
        "target",
    ]


def test_static_ownership_conflict_is_reported_before_repo_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    (repo_root / "groups").mkdir()
    (repo_root / "groups" / "all.toml").write_text('members = ["alpha", "beta"]\n', encoding="utf-8")
    marker = tmp_path / "repo-guard"
    _write_repo_guard(repo_root, operation="push", command=f"printf guard > {marker}; exit 100")
    for package_id in ("alpha", "beta"):
        _write_file_package(
            repo_root,
            package_id=package_id,
            targets=[("config", "~/.config/shared.txt", None, None)],
        )

    with pytest.raises(ValueError, match="conflicting explicit tracked targets"):
        _open_push(_engine(tmp_path, repo_root), tmp_path, [("alpha", "default"), ("beta", "default")])

    assert not marker.exists()


@pytest.mark.parametrize("scope", ["repo", "target"])
def test_repo_and_target_guard_hard_failures_abort_opening_with_typed_evidence_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    if scope == "repo":
        _write_repo_guard(
            repo_root,
            operation="push",
            command="printf 'repo guard exploded\\n' >&2; exit 7",
        )
        target_guard = None
    else:
        target_guard = "printf 'target guard exploded\\n' >&2; exit 8"
    _write_file_package(
        repo_root,
        package_id="app",
        targets=[("config", "~/.config/app.txt", target_guard, None)],
    )

    failed = _open_push(_engine(tmp_path, repo_root), tmp_path, [("app", "default")])

    assert isinstance(failed, SessionOpenFailed)
    assert failed.diagnostic.code == "planning-failed"
    # Guard output may contain managed content, so it never reaches the diagnostic.
    assert failed.diagnostic.message == (
        "fixture guard_push failed with exit 7" if scope == "repo" else "fixture:app.config guard_push failed with exit 8"
    )


def test_target_guard_diagnostic_uses_package_instance_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    package_root = repo_root / "packages" / "profiled"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "config.txt").write_text("value\n", encoding="utf-8")
    package_root.joinpath("package.toml").write_text(
        "\n".join(
            [
                'binding_mode = "multi_instance"',
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                'path = "~/.config/{{ profile }}/config.txt"',
                "",
                "[targets.config.hooks]",
                'guard_push = "exit 100"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")

    with _open_push(_engine(tmp_path, repo_root), tmp_path, [("profiled", "work")]) as session:
        assert [row.scope for row in session.view.rows] == ["fixture:profiled<work>.config"]


def test_target_guard_hard_failure_fails_push_for_package_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "profiled"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "config.txt").write_text("value\n", encoding="utf-8")
    package_root.joinpath("package.toml").write_text(
        "\n".join(
            [
                'binding_mode = "multi_instance"',
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                'path = "~/.config/{{ profile }}/config.txt"',
                "",
                "[targets.config.hooks]",
                'guard_push = "printf failed >&2; exit 8"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("profiled", "work")])

    assert main(["--config", str(config_path), "--json", "--no-confirm", "push"]) == 1

    diagnostic, = json.loads(capsys.readouterr().out)["summary"]["diagnostics"]
    assert diagnostic["code"] == "planning-failed"
    # Guard output may contain managed content, so only typed evidence is reported.
    assert diagnostic["message"] == "fixture:profiled<work>.config guard_push failed with exit 8"
    assert not (home / ".config/work/config.txt").exists()


def test_target_skip_can_leave_noop_eligible_package_and_repo_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    hook_output = shlex.quote(str(tmp_path / "hook-output"))
    repo_root.joinpath("repo.toml").write_text(
        f"[hooks.pre_push]\ncommands = [\"printf repo >> {hook_output}\"]\nrun_noop = true\n",
        encoding="utf-8",
    )
    _write_file_package(
        repo_root,
        package_id="app",
        targets=[("config", "~/.config/app.txt", "exit 100", None)],
    )
    package_path = repo_root / "packages" / "app" / "package.toml"
    package_path.write_text(
        package_path.read_text(encoding="utf-8")
        + f"\n[hooks.pre_push]\ncommands = [\"printf package >> {hook_output}\"]\nrun_noop = true\n",
        encoding="utf-8",
    )

    engine = _engine(tmp_path, repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])

    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert [(row.kind, row.scope) for row in session.view.rows] == [
            ("hook", "fixture"), ("hook", "fixture:app"), ("guard-skip", "fixture:app.config"),
        ]
        assert session.execute().result.status == "completed"

    assert (tmp_path / "hook-output").read_text(encoding="utf-8") == "repopackage"


@pytest.mark.parametrize("scope", ["repo", "target"])
@pytest.mark.parametrize(
    ("guard_payload", "error_match"),
    [
        ('{ run = "printf guard", io = "tty" }', "guard_push.*io.*pipe"),
        ('{ run = "printf guard", run_noop = true }', "guard_push.*run_noop"),
        ('{ commands = ["printf guard"], run_noop = true }', "guard_push.*run_noop"),
    ],
)
def test_repo_and_target_guard_manifests_reject_interactive_io_and_run_noop(
    tmp_path: Path,
    scope: str,
    guard_payload: str,
    error_match: str,
) -> None:
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    if scope == "repo":
        repo_root.joinpath("repo.toml").write_text(
            f"[hooks]\nguard_push = {guard_payload}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=error_match):
            _engine(tmp_path, repo_root)
        return

    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "config.txt").write_text("value\n", encoding="utf-8")
    package_root.joinpath("package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                'path = "~/.config/app.txt"',
                "",
                "[targets.config.hooks]",
                f"guard_push = {guard_payload}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=error_match):
        _engine(tmp_path, repo_root).get_repo("fixture").resolve_package("app")


def test_capture_exit_100_is_a_visible_unapproved_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    live_path = home / ".config" / "app.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("live\n", encoding="utf-8")
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    _write_file_package(
        repo_root,
        package_id="app",
        operation="pull",
        targets=[("config", "~/.config/app.txt", None, "printf unavailable >&2; exit 100")],
    )
    package_path = repo_root / "packages" / "app" / "package.toml"
    package_text = package_path.read_text(encoding="utf-8").replace(
        'capture = "printf unavailable >&2; exit 100"',
        'capture = "printf unavailable >&2; exit 100"',
    )
    package_path.write_text(package_text, encoding="utf-8")

    initialize_git_repository(repo_root)
    with open_tracked_pull_session(_engine(tmp_path, repo_root), tmp_path, entries=[("app", "default")]) as session:
        row = session.view.rows[0]
        assert not row.approved
        diagnostics = row.observation.diagnostics or row.diagnostics
        assert "exit 100" in str(diagnostics)
        assert "unavailable" not in str(diagnostics)


def test_cli_renders_repo_and_target_guard_diagnostics_in_human_and_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_skipped_root = tmp_path / "repo-skipped"
    _write_profile(repo_skipped_root)
    _write_repo_guard(
        repo_skipped_root,
        operation="push",
        command="printf 'repo mismatch\\n'; exit 100",
    )
    _write_file_package(
        repo_skipped_root,
        package_id="app",
        targets=[("config", "~/.config/repo-skipped.txt", None, None)],
    )

    target_skipped_root = tmp_path / "target-skipped"
    _write_profile(target_skipped_root)
    _write_file_package(
        target_skipped_root,
        package_id="app",
        targets=[
            (
                "config",
                "~/.config/target-skipped.txt",
                "printf 'target mismatch\\n'; exit 100",
                None,
            )
        ],
    )

    config_path = write_named_manager_config(
        tmp_path,
        {"repo-skip": repo_skipped_root, "target-skip": target_skipped_root},
    )
    for repo_name in ("repo-skip", "target-skip"):
        write_tracked_packages_state(tmp_path / "state", repo_name=repo_name, entries=[("app", "default")])

    assert main(["--config", str(config_path), "--no-confirm", "push", "--dry-run"]) == 0
    human_output = capsys.readouterr().out
    assert "[skipped] repo-skip (guard_push)" in human_output
    assert "Guard skipped: repo mismatch" in human_output
    assert "[skipped] target-skip:app.config (guard_push)" in human_output
    assert "Guard skipped: target mismatch" in human_output

    assert main(["--config", str(config_path), "--json", "--no-confirm", "push", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["guard_skips"] == [
        {
            "identity": "repo-skip",
            "direction": "push",
            "scope_kind": "repo",
            "path_rule_pattern": None,
            "reason": "repo mismatch",
        },
        {
            "identity": "target-skip:app.config",
            "direction": "push",
            "scope_kind": "target",
            "path_rule_pattern": None,
            "reason": "target mismatch",
        },
    ]
    assert payload["sync_units"] == []


def test_all_target_guard_skipped_cli_returns_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    _write_file_package(
        repo_root,
        package_id="app",
        targets=[("config", "~/.config/app.txt", "exit 100", None)],
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])
    assert main(["--config", str(config_path), "--json", "--no-confirm", "push"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [skip["identity"] for skip in payload["guard_skips"]] == ["fixture:app.config"]
    assert payload["sync_units"] == []
    assert payload["stages"] == []
    assert not (home / ".config/app.txt").exists()
