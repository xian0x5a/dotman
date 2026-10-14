from __future__ import annotations

import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from dotman.command_runtime import CommandResult, MemoryCommandRuntime, ShellCommand
from dotman.engine import DotmanEngine
from dotman.progress import _TqdmSink, make_planning_sink
from dotman.sync_session import SessionOpenFailed
from tests.helpers import write_single_repo_config, write_tracked_packages_state


class FakeSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, int | None]] = []

    def start(self, total: int) -> None:
        self.events.append(("start", total))

    def update(self, n: int = 1) -> None:
        self.events.append(("update", n))

    def close(self) -> None:
        self.events.append(("close", None))


def _write_progress_fixture(tmp_path: Path, *, render_command: str | None = None) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    package_root = repo_root / "packages" / "app"
    (package_root / "files").mkdir(parents=True)
    (package_root / "files" / "config.txt").write_text("source\n", encoding="utf-8")
    package_lines = [
        "",
        "[targets.config]",
        'source = "files/config.txt"',
        'path = "~/.config/app/config.txt"',
    ]
    if render_command is not None:
        package_lines.append(f'render = "{render_command}"')
    (package_root / "package.toml").write_text("\n".join(package_lines) + "\n", encoding="utf-8")
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("app", "default")],
    )
    return write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)


def test_make_planning_sink_skips_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)

    assert make_planning_sink(json_output=True) is None


def test_make_planning_sink_skips_non_tty_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)

    assert make_planning_sink(json_output=False) is None


def test_tqdm_sink_closes_after_update() -> None:
    sink = _TqdmSink()

    sink.start(1)
    sink.update(1)
    sink.close()


def test_tqdm_sink_redraws_elapsed_without_progress_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTqdm:
        instances: list["FakeTqdm"] = []

        def __init__(self, **_kwargs) -> None:
            self.refreshed = Event()
            self.refresh_count = 0
            self.closed = False
            FakeTqdm.instances.append(self)

        def update(self, _n: int = 1) -> None:
            raise AssertionError("redraw timer must not advance progress")

        def refresh(self) -> None:
            self.refresh_count += 1
            self.refreshed.set()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setitem(sys.modules, "tqdm", SimpleNamespace(tqdm=FakeTqdm))
    sink = _TqdmSink(refresh_interval=0.01)

    sink.start(1)
    fake_pbar = FakeTqdm.instances[0]
    try:
        assert fake_pbar.refreshed.wait(timeout=1)
        assert fake_pbar.refresh_count >= 1
    finally:
        sink.close()

    assert fake_pbar.closed
    assert sink._refresh_thread is None


def test_push_session_open_reports_progress_after_target_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    runtime = MemoryCommandRuntime(
        [CommandResult(exit_code=0, stdout=b"rendered\n")]
    )
    engine = DotmanEngine.from_config_path(
        _write_progress_fixture(tmp_path, render_command="render-command"),
        command_runtime=runtime,
    )

    class ProjectionOrderingSink(FakeSink):
        def update(self, n: int = 1) -> None:
            assert [request.command for request in runtime.requests] == [
                ShellCommand("render-command")
            ]
            super().update(n)

    sink = ProjectionOrderingSink()

    with engine.open_push_session(engine.resolve_sync_scope(), preview=True, sink=sink):
        assert sink.events == [("start", 1), ("update", 1), ("close", None)]


def test_push_session_open_closes_progress_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Ctrl-C during projection aborts opening after progress has started.
    engine = DotmanEngine.from_config_path(
        _write_progress_fixture(tmp_path, render_command="render-command"),
        command_runtime=MemoryCommandRuntime([KeyboardInterrupt()]),
    )
    sink = FakeSink()

    opened = engine.open_push_session(engine.resolve_sync_scope(), preview=True, sink=sink)

    assert isinstance(opened, SessionOpenFailed), opened
    assert opened.diagnostic.code == "interrupted"
    assert sink.events == [("start", 1), ("close", None)]


def test_sync_session_open_reports_progress_per_observed_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    engine = DotmanEngine.from_config_path(_write_progress_fixture(tmp_path))
    live_path = home / ".config" / "app" / "config.txt"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("live\n", encoding="utf-8")
    sink = FakeSink()

    session = engine.open_sync_session(engine.resolve_sync_scope(), preview=True, sink=sink)

    with session:
        assert sink.events == [("start", 1), ("update", 1), ("close", None)]
