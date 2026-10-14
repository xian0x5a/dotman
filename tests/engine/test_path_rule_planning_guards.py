from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from dotman.cli import main
from dotman.engine import DotmanEngine
from dotman.models import HookCommandSpec
from dotman.sync_session import SessionOpenFailed
from tests.helpers import write_single_repo_config, write_tracked_packages_state


def _write_directory_package(
    repo_root: Path,
    *,
    path_rule_blocks: list[list[str]],
    target_lines: list[str] | None = None,
) -> Path:
    package_root = repo_root / "packages" / "app"
    source_root = package_root / "files" / "config"
    source_root.mkdir(parents=True)
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")

    lines = [
        "",
        "[targets.config]",
        'source = "files/config"',
        'path = "~/.config/app"',
        *(target_lines or []),
    ]
    for index, block in enumerate(path_rule_blocks, start=1):
        lines.extend(["", f"[targets.config.path_rules.rule{index}]", *block])
    package_root.joinpath("package.toml").write_text("\n".join([*lines, ""]), encoding="utf-8")
    return source_root


def _engine(tmp_path: Path, repo_root: Path) -> DotmanEngine:
    return DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )


def _guard_rule(*, pattern: str, operation: str, command: str, extra: list[str] | None = None) -> list[str]:
    return [
        f"pattern = {json.dumps(pattern)}",
        *(extra or []),
        f"hooks = {{ guard_{operation} = {json.dumps(command)} }}",
    ]


def _open(engine: DotmanEngine, tmp_path: Path, operation: str = "push"):
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])
    return getattr(engine, f"open_{operation}_session")(engine.resolve_sync_scope(), preview=True)


def _guard_skips(session) -> list:
    return [row.guard_skip for row in session.view.rows if getattr(row, "kind", None) == "guard-skip"]


def test_path_rule_guards_normalize_supported_command_forms_and_elevation(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _write_directory_package(
        repo_root,
        path_rule_blocks=[
            [
                'pattern = "*.txt"',
                "hooks = { guard_push = \"printf string\", guard_pull = [",
                '  { run = "printf object", io = "pipe", elevation = "intercept" },',
                '  "printf ordered",',
                "] }",
            ]
        ],
    )

    rule = _engine(tmp_path, repo_root).get_repo("fixture").resolve_package("app").targets["config"].path_rules[0]

    assert rule.hooks is not None
    assert rule.hooks["guard_push"].commands == (HookCommandSpec(run="printf string"),)
    assert rule.hooks["guard_pull"].commands == (
        HookCommandSpec(run="printf object", elevation="intercept"),
        HookCommandSpec(run="printf ordered"),
    )


@pytest.mark.parametrize(
    ("hooks_payload", "error_match"),
    [
        ('{ pre_push = "printf invalid" }', "unsupported hook names: pre_push"),
        ('{ guard_push = { run = "printf invalid", io = "tty" } }', "guard_push.*io.*pipe"),
        ('{ guard_push = { run = "printf invalid", run_noop = true } }', "guard_push.*run_noop"),
        ('{ guard_push = { commands = ["printf invalid"], run_noop = true } }', "guard_push.*run_noop"),
    ],
)
def test_path_rule_hooks_reject_pre_post_interactive_io_and_run_noop(
    tmp_path: Path,
    hooks_payload: str,
    error_match: str,
) -> None:
    repo_root = tmp_path / "repo"
    _write_directory_package(
        repo_root,
        path_rule_blocks=[
            [
                'pattern = "*.txt"',
                f"hooks = {hooks_payload}",
            ]
        ],
    )

    with pytest.raises(ValueError, match=error_match):
        _engine(tmp_path, repo_root).get_repo("fixture").resolve_package("app")


@pytest.mark.parametrize("operation", ["push", "pull"])
def test_path_rule_guards_activate_once_for_repo_live_shared_and_noop_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    marker = tmp_path / "guard-order"
    command = f'printf "%s\\n" "$DOTMAN_PATH_RULE_PATTERN" >> {shlex.quote(str(marker))}'
    repo_root = tmp_path / "repo"
    patterns = ["*.txt", "repo-only.txt", "live-only.txt", "shared-changed.txt", "shared-noop.txt", "missing.txt"]
    source_root = _write_directory_package(
        repo_root,
        path_rule_blocks=[
            _guard_rule(pattern=pattern, operation=operation, command=command)
            for pattern in patterns
        ],
    )
    (source_root / "repo-only.txt").write_text("repo only\n", encoding="utf-8")
    (source_root / "shared-changed.txt").write_text("repo changed\n", encoding="utf-8")
    (source_root / "shared-noop.txt").write_text("same\n", encoding="utf-8")
    live_root = home / ".config" / "app"
    live_root.mkdir(parents=True)
    (live_root / "live-only.txt").write_text("live only\n", encoding="utf-8")
    (live_root / "shared-changed.txt").write_text("live changed\n", encoding="utf-8")
    (live_root / "shared-noop.txt").write_text("same\n", encoding="utf-8")

    with _open(_engine(tmp_path, repo_root), tmp_path, operation) as session:
        assert len(session.view.observations) == 4
        assert _guard_skips(session) == []
    assert marker.read_text(encoding="utf-8").splitlines() == patterns[:-1]


def test_path_rule_activation_excludes_ignored_control_and_skip_marker_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    marker = tmp_path / "guard-order"
    command = f'printf "%s\\n" "$DOTMAN_PATH_RULE_PATTERN" >> {shlex.quote(str(marker))}'
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_root.joinpath("repo.toml").write_text(
        '[ignore]\ngitignore = true\nskip_markers = [".dotman-skip"]\n',
        encoding="utf-8",
    )
    source_root = _write_directory_package(
        repo_root,
        target_lines=["[targets.config.ignore]", 'patterns = ["ignored.txt"]'],
        path_rule_blocks=[
            _guard_rule(pattern=pattern, operation="push", command=command)
            for pattern in ("keep.txt", "ignored.txt", ".gitignore", "skipped/hidden.txt")
        ],
    )
    (source_root / "keep.txt").write_text("keep\n", encoding="utf-8")
    (source_root / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (source_root / ".gitignore").write_text("", encoding="utf-8")
    (source_root / "skipped").mkdir()
    (source_root / "skipped" / ".dotman-skip").write_text("", encoding="utf-8")
    (source_root / "skipped" / "hidden.txt").write_text("hidden\n", encoding="utf-8")

    with _open(_engine(tmp_path, repo_root), tmp_path) as session:
        assert [unit.identity.child_path for unit in session.view.observations] == ["keep.txt"]

    assert marker.read_text(encoding="utf-8").splitlines() == ["keep.txt"]


def test_overlapping_path_rule_guards_run_in_order_prune_work_and_keep_scalar_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    marker = tmp_path / "guard-order"
    quoted_marker = shlex.quote(str(marker))
    repo_root = tmp_path / "repo"
    source_root = _write_directory_package(
        repo_root,
        path_rule_blocks=[
            _guard_rule(
                pattern="a.txt",
                operation="push",
                command=f"printf 'skip-a\\n' >> {quoted_marker}; printf 'a disabled\\n'; exit 100",
            ),
            _guard_rule(
                pattern="*.txt",
                operation="push",
                command=f"printf 'broad\\n' >> {quoted_marker}",
                extra=['chmod = "600"'],
            ),
            _guard_rule(
                pattern="b.txt",
                operation="push",
                command=f"printf 'specific-b\\n' >> {quoted_marker}",
                extra=['chmod = "640"'],
            ),
            _guard_rule(
                pattern="a.txt",
                operation="push",
                command=f"printf 'dead-a\\n' >> {quoted_marker}",
            ),
        ],
    )
    for name in ("a.txt", "b.txt"):
        (source_root / name).write_text(f"repo {name}\n", encoding="utf-8")
    live_root = home / ".config" / "app"
    live_root.mkdir(parents=True)
    for name in ("a.txt", "b.txt"):
        (live_root / name).write_text(f"live {name}\n", encoding="utf-8")

    with _open(_engine(tmp_path, repo_root), tmp_path) as session:
        assert [(unit.identity.child_path, unit.chmod) for unit in session.view.observations] == [("b.txt", "640")]
        assert [
            (skip.scope_kind, skip.scope_label, skip.path_rule_pattern, skip.reason) for skip in _guard_skips(session)
        ] == [("path_rule", "fixture:app.config", "a.txt", "a disabled")]

    assert marker.read_text(encoding="utf-8").splitlines() == ["skip-a", "broad", "specific-b"]


def test_path_rule_guard_environment_uses_target_roots_and_pattern_without_child_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    marker = tmp_path / "guard-env"
    quoted_marker = shlex.quote(str(marker))
    repo_root = tmp_path / "repo"
    source_root = _write_directory_package(
        repo_root,
        path_rule_blocks=[
            _guard_rule(
                pattern="*.txt",
                operation="push",
                command=(
                    f'printf "%s|%s|%s|%s\\n" "$DOTMAN_PATH_RULE_PATTERN" "$DOTMAN_REPO_PATH" '
                    f'"$DOTMAN_LIVE_PATH" "${{DOTMAN_CHILD_PATH-unset}}" > {quoted_marker}; '
                    "printf 'host mismatch\\n'; exit 100"
                ),
            )
        ],
    )
    (source_root / "one.txt").write_text("one\n", encoding="utf-8")

    with _open(_engine(tmp_path, repo_root), tmp_path) as session:
        assert [skip.reason for skip in _guard_skips(session)] == ["host mismatch"]

    assert marker.read_text(encoding="utf-8").strip() == "|".join(
        ["*.txt", str(source_root), str(home / ".config" / "app"), "unset"]
    )


def test_path_rule_guard_hard_failure_names_target_and_pattern_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    source_root = _write_directory_package(
        repo_root,
        path_rule_blocks=[
            _guard_rule(
                pattern="*.txt",
                operation="push",
                command="printf 'rule exploded\\n' >&2; exit 7",
            )
        ],
    )
    (source_root / "one.txt").write_text("one\n", encoding="utf-8")

    failed = _open(_engine(tmp_path, repo_root), tmp_path)

    assert isinstance(failed, SessionOpenFailed)
    assert failed.diagnostic.code == "planning-failed"
    # Guard output may contain managed content, so it never reaches the diagnostic.
    assert failed.diagnostic.message == "fixture:app.config (path rule: *.txt) guard_push failed with exit 7"


def test_all_path_rule_work_skipped_cli_reports_pattern_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo_root = tmp_path / "repo"
    source_root = _write_directory_package(
        repo_root,
        path_rule_blocks=[
            _guard_rule(
                pattern="*.txt",
                operation="push",
                command="printf 'directory disabled\\n'; exit 100",
            )
        ],
    )
    (source_root / "one.txt").write_text("one\n", encoding="utf-8")
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])
    assert main(["--config", str(config_path), "--no-confirm", "push"]) == 0
    human_output = capsys.readouterr().out
    assert "[skipped] fixture:app.config (guard_push) (path rule: *.txt)" in human_output
    assert "Guard skipped: directory disabled" in human_output
    assert not (home / ".config/app/one.txt").exists()

    assert main(["--config", str(config_path), "--json", "--no-confirm", "push", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sync_units"] == []
    assert payload["guard_skips"] == [
        {
            "identity": "fixture:app.config",
            "direction": "push",
            "scope_kind": "path_rule",
            "path_rule_pattern": "*.txt",
            "reason": "directory disabled",
        }
    ]
