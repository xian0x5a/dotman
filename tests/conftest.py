from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import signal
import sys
from typing import Iterator

import pytest

from dotman.command_runtime import (
    ArgvCommand,
    CommandRequest,
    CommandResult,
    ProductionCommandRuntime,
    command_runtime_session,
)


@pytest.fixture(autouse=True, scope="session")
def interactive_sigint() -> Iterator[None]:
    # A shell without job control starts `&` jobs with SIGINT ignored. That
    # survives exec, and CPython then keeps it instead of installing its own
    # handler, so every child ignores SIGINT too. Interrupt tests assume a
    # terminal's default SIGINT (#116).
    if signal.getsignal(signal.SIGINT) is not signal.SIG_IGN:
        yield
        return
    signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    signal.signal(signal.SIGINT, signal.SIG_IGN)


@pytest.fixture(autouse=True)
def isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLI execution tests resolve real managed paths such as ~/.gitconfig.
    # Isolate HOME suite-wide so a missed per-test override cannot write user files.
    home = tmp_path / "isolated-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
def isolate_xdg_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg_config_home = tmp_path / "xdg-config"
    xdg_config_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))


@pytest.fixture(autouse=True)
def isolate_xdg_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Snapshot retention must never inspect or prune the user's snapshot store.
    data_home = tmp_path / "xdg-data"
    data_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))


@pytest.fixture(autouse=True)
def isolate_xdg_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg_state_home = tmp_path / "state"
    xdg_state_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))


@pytest.fixture(autouse=True)
def isolate_sudo_askpass(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a terminal, a set SUDO_ASKPASS adds `-A` to dotman's sudo calls.
    # Keep the user's helper from changing expected argv in agent or CI runs.
    monkeypatch.delenv("SUDO_ASKPASS", raising=False)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Deck starts with Commit Work on, so any interactive run over a git repo
    # commits; commits need an identity and HOME is isolated from the user's.
    for key, value in (("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "test@example.test"),
                       ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "test@example.test")):
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def disable_colors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Output stays plain even under `pytest -s` in a real terminal. Tests that
    # check styling unset NO_COLOR and patch sys.stdout.isatty in the test body:
    # capsys swaps sys.stdout between setup and call, so a fixture cannot.
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture(autouse=True)
def mock_sudo_for_tests() -> Iterator[None]:
    production = ProductionCommandRuntime()

    class TestCommandRuntime:
        def request_cancel(self) -> None:
            production.request_cancel()

        def check_cancelled(self) -> None:
            production.check_cancelled()

        def run(self, request: CommandRequest) -> CommandResult:
            self.check_cancelled()
            command = request.command
            if not isinstance(command, ArgvCommand) or not command.arguments or command.arguments[0] != "sudo":
                return production.run(request)
            arguments = command.arguments
            if arguments[1:] in {("-v",), ("-n", "-v")} or arguments[1:3] == ("-n", "true"):
                return CommandResult(exit_code=0)
            if arguments[1:3] == ("-n", "/bin/cat") and len(arguments) >= 4:
                return CommandResult(exit_code=0, stdout=Path(arguments[3]).read_bytes())
            if len(arguments) >= 4 and arguments[1] == "-n" and arguments[2] == sys.executable:
                return production.run(replace(request, command=ArgvCommand(arguments[2:])))
            prefix_length = 2 if len(arguments) > 1 and arguments[1] == "-n" else 1
            return production.run(replace(request, command=ArgvCommand(arguments[prefix_length:])))

    with command_runtime_session(TestCommandRuntime()):
        yield
