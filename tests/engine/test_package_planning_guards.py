from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from dotman import sync_deck_command
from dotman.cli import main
from dotman.engine import DotmanEngine
from dotman.sync_session import SessionOpenFailed
from tests.helpers import write_single_repo_config, write_tracked_packages_state


def _write_profile(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True, exist_ok=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")


def _write_guarded_target_package(
    repo_root: Path,
    *,
    operation: str,
    guard_command: str,
    projection_marker: Path | None = None,
) -> None:
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True, exist_ok=True)
    (package_root / "files" / "config.txt").write_text("repo value\n", encoding="utf-8")
    projection_line = ""
    if projection_marker is not None:
        marker = shlex.quote(str(projection_marker))
        if operation == "push":
            projection_line = f'render = "printf projected > {marker}; cat \\"$DOTMAN_REPO_PATH\\""'
        else:
            projection_line = f'capture = "printf projected > {marker}; cat \\"$DOTMAN_LIVE_PATH\\""'
    lines = [
        "",
        "[targets.config]",
        'source = "files/config.txt"',
        'path = "~/.config/app/config.txt"',
        *( [projection_line] if projection_line else [] ),
        "",
        "[hooks]",
        f"guard_{operation} = {json.dumps(guard_command)}",
        f"pre_{operation} = \"printf pre\"",
        f"post_{operation} = \"printf post\"",
        "",
    ]
    (package_root / "package.toml").write_text("\n".join(lines), encoding="utf-8")


def _engine(tmp_path: Path, repo_root: Path) -> DotmanEngine:
    return DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )


def _open_push(engine: DotmanEngine, tmp_path: Path, entries: list[tuple[str, str]]):
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=entries)
    return engine.open_push_session(engine.resolve_sync_scope(), preview=True)


def _guard_skips(session) -> list:
    return [row.guard_skip for row in session.view.rows if getattr(row, "kind", None) == "guard-skip"]


@pytest.mark.parametrize("operation", ["push", "pull"])
def test_package_guard_exit_100_omits_package_before_host_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DOTMAN_NO_CONFIRM", "1")
    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("live value\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    projection_marker = tmp_path / "projected"
    _write_guarded_target_package(
        repo_root,
        operation=operation,
        guard_command="[ -z \"${DOTMAN_NO_CONFIRM+x}\" ] || exit 9; printf 'not for this host\\n' >&2; exit 100",
        projection_marker=projection_marker,
    )
    engine = _engine(tmp_path, repo_root)

    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])
    with getattr(engine, f"open_{operation}_session")(engine.resolve_sync_scope(), preview=True) as session:
        assert session.view.observations == ()
        skip, = session.view.rows
        assert (skip.kind, skip.scope, skip.guard_skip.scope_kind, skip.guard_skip.reason) == (
            "guard-skip", "fixture:app", "package", "not for this host",
        )
    assert not projection_marker.exists()


def test_package_guard_hard_failure_aborts_opening_with_typed_evidence_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    _write_guarded_target_package(
        repo_root,
        operation="push",
        guard_command="printf 'guard exploded\\n' >&2; exit 7",
    )
    failed = _open_push(_engine(tmp_path, repo_root), tmp_path, [("app", "default")])

    assert isinstance(failed, SessionOpenFailed)
    assert failed.diagnostic.code == "planning-failed"
    # Guard output may contain managed content, so it never reaches the diagnostic.
    assert failed.diagnostic.message == "fixture:app guard_push failed with exit 7"


def test_package_guard_commands_preserve_order_stop_on_skip_and_support_elevation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "config.txt").write_text("repo value\n", encoding="utf-8")
    marker = tmp_path / "guard-order"
    package_root.joinpath("package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.config]",
                'source = "files/config.txt"',
                'path = "~/.config/app/config.txt"',
                "",
                "[hooks]",
                "guard_push = [",
                f"  {{ run = {json.dumps(f'printf first >> {marker}')} }},",
                "  { run = \"printf 'elevated skip\\n' >&2; exit 100\", elevation = \"intercept\" },",
                f"  {{ run = {json.dumps(f'printf third >> {marker}')} }},",
                "]",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with _open_push(_engine(tmp_path, repo_root), tmp_path, [("app", "default")]) as session:
        assert [skip.reason for skip in _guard_skips(session)] == ["elevated skip"]
    assert marker.read_text(encoding="utf-8") == "first"


def test_package_guard_runs_once_per_instance_per_session_open_and_never_enters_execution(
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
    marker = tmp_path / "guard-runs"
    _write_guarded_target_package(
        repo_root,
        operation="push",
        guard_command=f"printf run >> {shlex.quote(str(marker))}",
    )
    for package_id in ("meta-a", "meta-b"):
        package_root = repo_root / "packages" / package_id
        package_root.mkdir(parents=True)
        (package_root / "package.toml").write_text(
            'depends = ["app"]\n',
            encoding="utf-8",
        )
    engine = _engine(tmp_path, repo_root)
    entries = [("meta-a", "default"), ("meta-b", "default")]

    with _open_push(engine, tmp_path, entries) as session:
        assert _guard_skips(session) == []
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert session.execute().result.status == "completed"

    assert marker.read_text(encoding="utf-8") == "runrun"
    assert (home / ".config/app/config.txt").read_text(encoding="utf-8") == "repo value\n"


def test_run_noop_admits_package_hooks_and_guard_never_runs_as_a_hook(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    package_root = repo_root / "packages" / "app"
    package_root.mkdir(parents=True)
    marker = tmp_path / "guard-runs"
    hook_output = tmp_path / "hook-output"
    package_root.joinpath("package.toml").write_text(
        "\n".join(
            [
                "",
                "[hooks]",
                f'guard_push = "printf guard >> {marker}"',
                f'pre_push = "printf pre >> {hook_output}"',
                f'post_push = "printf post >> {hook_output}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    engine = _engine(tmp_path, repo_root)

    with _open_push(engine, tmp_path, [("app", "default")]) as session:
        assert session.view.rows == ()
    with engine.open_push_session(engine.resolve_sync_scope(), run_noop=True) as session:
        assert [(row.kind, row.scope) for row in session.view.rows] == [("hook", "fixture:app")]
        assert session.execute().result.status == "completed"

    assert marker.read_text(encoding="utf-8") == "guard"
    assert hook_output.read_text(encoding="utf-8") == "prepost"


@pytest.mark.parametrize(
    "guard_manifest, error_match",
    [
        ('guard_push = { run = "printf guard", io = "tty" }', "guard_push.*io.*pipe"),
        ('guard_push = { run = "printf guard", run_noop = true }', "guard_push.*run_noop"),
        ("[hooks.guard_push]\ncommands = [\"printf guard\"]\nrun_noop = true", "guard_push.*run_noop"),
    ],
)
def test_package_guard_manifest_rejects_interactive_io_and_run_noop(
    tmp_path: Path,
    guard_manifest: str,
    error_match: str,
) -> None:
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    package_root = repo_root / "packages" / "app"
    package_root.mkdir(parents=True)
    package_root.joinpath("package.toml").write_text(
        f'[hooks]\n{guard_manifest}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=error_match):
        _engine(tmp_path, repo_root).get_repo("fixture").resolve_package("app")


def test_all_guard_skipped_cli_reports_before_ui_and_returns_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    _write_guarded_target_package(
        repo_root,
        operation="push",
        guard_command="printf 'host mismatch\\n'; exit 100",
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])

    class RecordingSink:
        closed = False

        def start(self, _total: int) -> None:
            pass

        def update(self, _count: int = 1) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    sink = RecordingSink()
    monkeypatch.setattr(sync_deck_command, "make_planning_sink", lambda *, json_output, unit: sink)

    exit_code = main(["--config", str(config_path), "--no-confirm", "push"])

    assert exit_code == 0
    assert sink.closed is True
    output = capsys.readouterr().out
    assert "[skipped] fixture:app (guard_push)" in output
    assert "Guard skipped: host mismatch" in output
    assert "[ok]" not in output
    assert not (home / ".config/app/config.txt").exists()


def test_guard_skip_json_is_structured_and_omits_command_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    _write_profile(repo_root)
    secret_command = "printf 'json reason\\n' >&2; exit 100"
    _write_guarded_target_package(
        repo_root,
        operation="push",
        guard_command=secret_command,
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])

    exit_code = main(["--config", str(config_path), "--json", "--no-confirm", "push", "--dry-run"])

    assert exit_code == 0
    raw_output = capsys.readouterr().out
    payload = json.loads(raw_output)
    assert payload["guard_skips"] == [
        {
            "identity": "fixture:app",
            "direction": "push",
            "scope_kind": "package",
            "path_rule_pattern": None,
            "reason": "json reason",
        }
    ]
    assert payload["sync_units"] == []
    assert payload["stages"] == []
    assert secret_command not in raw_output
