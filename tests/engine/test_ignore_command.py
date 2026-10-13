from __future__ import annotations

import pytest

from dotman.sync_session import SessionOpenFailed
from tests.engine.test_sync_directory_observation import directory_engine, put


def ignore_command_engine(tmp_path, monkeypatch, command: str):
    # Single quotes keep the command a TOML literal string.
    return directory_engine(tmp_path, monkeypatch, extra=f"[targets.tree.ignore]\ncommand = '{command}'")


def open_session(engine, operation: str, *, preview: bool = True):
    opener = {"push": engine.open_push_session, "pull": engine.open_pull_session, "sync": engine.open_sync_session}
    return opener[operation](engine.resolve_sync_scope(), preview=preview)


def roots(tmp_path):
    return tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"


@pytest.mark.parametrize("operation", ["push", "pull", "sync"])
def test_ignore_command_output_excludes_children_on_both_sides(tmp_path, monkeypatch, operation):
    engine = ignore_command_engine(tmp_path, monkeypatch, r'printf "/managed/\n*.cache\n"')
    repo, live = roots(tmp_path)
    for root in (repo, live):
        for name in ("visible", "managed/a", "deep/x.cache"):
            put(root, name, root.name.encode() + b"-" + operation.encode())
    put(live, "managed/live-only")

    with open_session(engine, operation) as session:
        assert {unit.identity.child_path for unit in session.view.observations} == {"visible"}


def test_push_preserves_live_children_excluded_by_ignore_command(tmp_path, monkeypatch):
    engine = ignore_command_engine(tmp_path, monkeypatch, "printf /managed/")
    repo, live = roots(tmp_path)
    put(repo, "visible", b"repo")
    put(repo, "managed/a", b"repo copy")
    put(live, "managed/a", b"live copy")
    put(live, "managed/live-only", b"live")

    with open_session(engine, "push", preview=False) as session:
        assert session.execute().result.status == "completed"

    assert (live / "visible").read_bytes() == b"repo"
    assert (live / "managed/a").read_bytes() == b"live copy"
    assert (live / "managed/live-only").read_bytes() == b"live"


def test_pull_preserves_repository_children_excluded_by_ignore_command(tmp_path, monkeypatch):
    engine = ignore_command_engine(tmp_path, monkeypatch, "printf /managed/")
    repo, live = roots(tmp_path)
    put(live, "visible", b"live")
    put(repo, "managed/a", b"repo copy")
    put(live, "managed/a", b"live copy")
    put(live, "managed/live-only", b"live")

    with open_session(engine, "pull", preview=False) as session:
        assert session.execute().result.status == "completed"

    assert (repo / "visible").read_bytes() == b"live"
    assert (repo / "managed/a").read_bytes() == b"repo copy"
    assert not (repo / "managed/live-only").exists()


def test_ignore_command_runs_in_declaring_package_directory(tmp_path, monkeypatch):
    engine = ignore_command_engine(tmp_path, monkeypatch, "cat ignored.txt")
    (tmp_path / "repo/packages/app/ignored.txt").write_text("/managed/\n")
    _repo, live = roots(tmp_path)
    put(live, "visible")
    put(live, "managed/a")

    with open_session(engine, "pull") as session:
        assert {unit.identity.child_path for unit in session.view.observations} == {"visible"}


@pytest.mark.parametrize("operation", ["push", "pull", "sync"])
@pytest.mark.parametrize(
    ("command", "evidence"),
    [
        ("echo lock missing >&2; exit 3", "lock missing"),
        (r'printf "/managed/\n!keep\n"', "!keep"),
    ],
)
def test_ignore_command_failure_aborts_session_open(tmp_path, monkeypatch, operation, command, evidence):
    engine = ignore_command_engine(tmp_path, monkeypatch, command)
    put(roots(tmp_path)[1], "visible")

    failed = open_session(engine, operation)

    assert isinstance(failed, SessionOpenFailed)
    assert failed.diagnostic.code == "planning-failed"
    assert "app.tree" in failed.diagnostic.message
    assert evidence in f"{failed.diagnostic.message} {failed.output_line}"


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ('[targets.tree.ignore]\ncommand = ""', "command must not be empty"),
        ("[targets.tree.ignore]\ncommand = 1", "command must be a string"),
        ('[targets.probe]\nprobe = "true"\n[targets.probe.ignore]\ncommand = "true"', "must not define: ignore"),
    ],
)
def test_ignore_command_manifest_validation(tmp_path, monkeypatch, target, error):
    with pytest.raises(ValueError, match=error):
        directory_engine(tmp_path, monkeypatch, extra=target).resolve_sync_scope()


@pytest.mark.parametrize(
    ("operation", "guards"),
    [
        ("push", 'guard_push = "exit 100"'),
        ("pull", 'guard_pull = "exit 100"'),
        ("sync", 'guard_push = "exit 100"\nguard_pull = "exit 100"'),
    ],
)
def test_guarded_out_target_never_runs_its_ignore_command(tmp_path, monkeypatch, operation, guards):
    engine = directory_engine(tmp_path, monkeypatch, extra=(
        "[targets.tree.ignore]\ncommand = 'echo lock missing >&2; exit 3'\n"
        f"[targets.tree.hooks]\n{guards}\n"
    ))
    put(roots(tmp_path)[1], "visible")

    opened = open_session(engine, operation)

    assert not isinstance(opened, SessionOpenFailed), opened
    opened.abort()
