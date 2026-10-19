"""Snapshot capture and restore reach protected live paths through sudo, like push does (#169)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dotman.command_runtime import ArgvCommand, CommandRequest, CommandResult, MemoryCommandRuntime, command_runtime_session
from dotman.file_access import sudo_session
from dotman.models import SnapshotConfig, TargetPlan
from dotman.operation_runner import run_restore_operation
from dotman.snapshot import RestoreAction, SnapshotRecord, build_restore_actions, create_push_snapshot
from tests.helpers import make_package_plan

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses the permission bits these tests rely on")

PROTECTED_BYTES = b"protected value\n"
# Upper bound on fake sudo calls per test; an exhausted queue fails loudly instead of hanging.
MAX_FAKE_SUDO_CALLS = 16


def _granting_sudo_runtime() -> MemoryCommandRuntime:
    """Fake sudo that grants every request; `sudo -n /bin/cat` returns PROTECTED_BYTES."""

    def grant(request: CommandRequest) -> CommandResult:
        arguments = request.command.arguments
        assert arguments[0] == "sudo", f"unexpected non-sudo command: {arguments}"
        if arguments[1:3] == ("-n", "/bin/cat"):
            return CommandResult(exit_code=0, stdout=PROTECTED_BYTES)
        return CommandResult(exit_code=0)

    return MemoryCommandRuntime([grant] * MAX_FAKE_SUDO_CALLS)


def _sudo_argvs(runtime: MemoryCommandRuntime) -> list[tuple[str, ...]]:
    return [request.command.arguments for request in runtime.requests if isinstance(request.command, ArgvCommand)]


def _privileged_helper_calls(runtime: MemoryCommandRuntime, operation: str) -> list[CommandRequest]:
    return [
        request for request in runtime.requests
        if isinstance(request.command, ArgvCommand) and operation in request.command.arguments
    ]


@pytest.fixture
def read_only_directory(tmp_path: Path):
    directory = tmp_path / "protected"
    directory.mkdir()
    yield directory
    # Restore write permission so pytest can clean up tmp_path.
    directory.chmod(0o755)


@pytest.fixture
def unreadable_live_file(tmp_path: Path):
    live_path = tmp_path / "live" / "secret.conf"
    live_path.parent.mkdir()
    live_path.write_bytes(PROTECTED_BYTES)
    yield live_path
    live_path.chmod(0o644)


def _restore_snapshot(tmp_path: Path) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id="snapshot-1",
        created_at="2026-10-18T00:00:00Z",
        status="applied",
        root=tmp_path / "snapshots" / "snapshot-1",
        entries=(),
    )


def _push_plan(live_path: Path):
    return make_package_plan(
        operation="push",
        repo_name="fixture",
        package_id="app",
        requested_profile="default",
        target_plans=[
            TargetPlan(
                package_id="app",
                target_name="secret",
                repo_path=Path("/repo/app/secret.conf"),
                live_path=live_path,
                action="update",
                target_kind="file",
                projection_kind="raw",
            )
        ],
    )


@pytest.mark.parametrize(
    ("restore_action", "live_file_exists", "privileged_operation"),
    [
        ("create", False, "write-bytes-atomic"),
        ("delete", True, "delete-path-and-prune-empty-parents"),
    ],
)
def test_restore_escalates_to_sudo_when_live_parent_is_not_writable(
    tmp_path: Path, read_only_directory: Path, restore_action: str, live_file_exists: bool, privileged_operation: str,
) -> None:
    live_path = read_only_directory / "hosts"
    if live_file_exists:
        live_path.write_bytes(b"pushed value\n")
    read_only_directory.chmod(0o555)
    snapshot = _restore_snapshot(tmp_path)
    action = RestoreAction(
        live_path=live_path,
        snapshot_path=snapshot.root / "restore" / "hosts",
        action=restore_action,
        before_bytes=b"pushed value\n" if live_file_exists else b"",
        after_bytes=b"" if restore_action == "delete" else b"restored value\n",
        desired_mode=None if restore_action == "delete" else 0o644,
    )
    runtime = _granting_sudo_runtime()

    with command_runtime_session(runtime):
        result = run_restore_operation(snapshot=snapshot, actions=[action])

    assert [(action_result.status, action_result.error) for action_result in result.actions] == [("ok", None)]
    assert ("sudo", "-n", "-v") in _sudo_argvs(runtime)
    helper_calls = _privileged_helper_calls(runtime, privileged_operation)
    assert len(helper_calls) == 1
    assert str(live_path) in helper_calls[0].command.arguments
    if restore_action == "create":
        assert helper_calls[0].input == b"restored value\n"


def test_snapshot_capture_reads_unreadable_live_file_through_sudo(tmp_path: Path, unreadable_live_file: Path) -> None:
    unreadable_live_file.chmod(0o000)
    runtime = _granting_sudo_runtime()

    with command_runtime_session(runtime), sudo_session():
        snapshot = create_push_snapshot(
            [_push_plan(unreadable_live_file)],
            SnapshotConfig(enabled=True, path=tmp_path / "snapshots", max_generations=5),
        )

    assert snapshot is not None
    [entry] = snapshot.entries
    assert entry.existed_before
    assert (snapshot.root / entry.content_path).read_bytes() == PROTECTED_BYTES
    assert ("sudo", "-n", "/bin/cat", str(unreadable_live_file)) in _sudo_argvs(runtime)


def test_restore_planning_reads_unreadable_live_file_through_sudo(tmp_path: Path, unreadable_live_file: Path) -> None:
    unreadable_live_file.chmod(0o600)
    snapshot = create_push_snapshot(
        [_push_plan(unreadable_live_file)],
        SnapshotConfig(enabled=True, path=tmp_path / "snapshots", max_generations=5),
    )
    assert snapshot is not None
    unreadable_live_file.chmod(0o000)
    runtime = _granting_sudo_runtime()

    with command_runtime_session(runtime), sudo_session():
        [action] = build_restore_actions(snapshot)

    assert action.before_bytes == PROTECTED_BYTES
    assert ("sudo", "-n", "/bin/cat", str(unreadable_live_file)) in _sudo_argvs(runtime)
