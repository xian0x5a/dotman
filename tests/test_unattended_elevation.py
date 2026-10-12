from __future__ import annotations

import pytest

from dotman import interaction_policy
from dotman.command_runtime import ArgvCommand, CommandResult, MemoryCommandRuntime
from dotman.file_access import _SudoLease


# Unattended skips dotman's decisions, not sudo authentication.
@pytest.mark.parametrize(
    ("ticket_exit_code", "expected_requests"),
    [
        (0, [(("sudo", "-n", "-v"), "pipe")]),
        (1, [(("sudo", "-n", "-v"), "pipe"), (("sudo", "-v"), "tty")]),
    ],
    ids=["valid-ticket-no-prompt", "missing-ticket-prompts"],
)
def test_unattended_elevation_authenticates_only_without_ticket(monkeypatch, ticket_exit_code, expected_requests) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    runtime = MemoryCommandRuntime([CommandResult(exit_code=ticket_exit_code), CommandResult(exit_code=0)])
    lease = _SudoLease(runtime)
    try:
        with interaction_policy.interaction_scope(unattended=True):
            lease.request("publish files")
    finally:
        lease.close()
    assert [(request.command, request.io) for request in runtime.requests] == [
        (ArgvCommand(command), io) for command, io in expected_requests
    ]
