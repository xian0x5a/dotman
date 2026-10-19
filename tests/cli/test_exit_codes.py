"""Pin the CLI exit-code table (issue #156), independent of `--no-confirm`.

0   success
1   runtime failure: any error that is not a usage error, unattended decisions
    (InteractionRequiredError), Sync Base store errors, `doctor` unhealthy,
    malformed input documents and dotman-written state
2   usage/config error: argparse syntax, selector/scope/manifest/config validation
130 interrupt (covered by the interrupt tests)
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from dotman.cli import main
from dotman.sync_base_store import SyncBaseStore, SyncBaseStoreError
from tests.cli.test_sync_base_inspection import store_record
from tests.engine.test_sync_directory_observation import directory_engine, put
from tests.engine.test_sync_session import make_engine
from tests.helpers import write_manager_config, write_single_repo_config

# A builder prepares the environment and returns the `--config` path (None: use the default lookup).
Builder = Callable[[Path, pytest.MonkeyPatch], Path | None]


@dataclass(frozen=True)
class Case:
    id: str
    build: Builder
    argv: tuple[str, ...]
    expected: int
    # Some cases are only meaningful in one mode, e.g. Sync refuses to run
    # without a terminal unless `--no-confirm` is given.
    no_confirm_modes: tuple[bool, ...] = (False, True)
    # `--json` runs must still print their one JSON document when they fail.
    stdout_is_json_document: bool = False


def _example_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return write_manager_config(tmp_path)


def _sync_repo(extra: str = "") -> Builder:
    def build(tmp_path, monkeypatch):
        return make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", extra)]).config.config_path
    return build


def _invalid_manifest(tmp_path, monkeypatch):
    config_path = _sync_repo()(tmp_path, monkeypatch)
    (tmp_path / "repo/packages/app/package.toml").write_text('[targets.unit]\npath = "unterminated\n')
    return config_path


def _failing_ignore_command(tmp_path, monkeypatch):
    engine = directory_engine(tmp_path, monkeypatch, extra="[targets.tree.ignore]\ncommand = 'exit 3'")
    put(tmp_path / "repo/packages/app/tree", "a")
    put(tmp_path / "live/tree", "b")
    return engine.config.config_path


def _failing_store_read(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"repo", b"live", "")])
    store_record(engine)

    def failing_read(self, identity):
        # Stands in for an I/O failure on a store that opened successfully.
        raise SyncBaseStoreError("simulated read failure")

    monkeypatch.setattr(SyncBaseStore, "read", failing_read)
    return engine.config.config_path


def _unrenderable_target_path(tmp_path, monkeypatch):
    # Fails while Sync resolves its scope, before any session opens.
    config_path = _sync_repo()(tmp_path, monkeypatch)
    manifest = tmp_path / "repo/packages/app/package.toml"
    manifest.write_text(manifest.read_text().replace('path = "', 'path = "{{ undefined_var.x }}', 1))
    return config_path


def _malformed_transform_stdin(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{not json")))
    return None


def _unreadable_rewrite_stdin(tmp_path, monkeypatch):
    class UnreadableInput:
        def read(self):
            raise OSError("input/output error")

    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=UnreadableInput()))
    return None


def _corrupt_tracked_state(tmp_path, monkeypatch):
    # dotman-written state, unlike a manifest, is not the user's input to fix.
    config_path = _sync_repo()(tmp_path, monkeypatch)
    (tmp_path / "state/dotman/repos/main/tracked-packages.toml").write_text("not = [valid")
    return config_path


def _missing_repo_path(tmp_path, monkeypatch):
    return write_single_repo_config(tmp_path, repo_name="fixture", repo_path=tmp_path / "missing-repo")


def _config_path_is_directory(tmp_path, monkeypatch):
    (tmp_path / "config-dir").mkdir()
    return tmp_path / "config-dir"


def _no_default_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    return None


CASES = [
    Case("success", _example_repo, ("list", "tracked"), 0),
    Case("argparse-syntax-error", _example_repo, ("push", "--no-such-flag"), 2),
    Case("unknown-selector", _example_repo, ("info", "trackable", "doesnotexist"), 2),
    Case("invalid-manifest", _invalid_manifest, ("list", "tracked"), 2),
    Case("invalid-sync-scope", _sync_repo(), ("push", "--dry-run", "main:nothere"), 2, (True,)),
    Case("config-path-is-directory", _config_path_is_directory, ("doctor",), 2),
    Case("missing-default-config", _no_default_config, ("list", "tracked"), 2),
    Case(
        "json-preflight-runtime-failure", _unrenderable_target_path, ("--json", "push", "--dry-run"), 1, (True,),
        stdout_is_json_document=True,
    ),
    Case("malformed-transform-input", _malformed_transform_stdin, ("transform", "json", "-", "-", "--mode", "cleanup"), 1),
    Case("unreadable-rewrite-stdin", _unreadable_rewrite_stdin, ("rewrite", "home", "expand"), 1),
    Case("corrupt-dotman-written-state", _corrupt_tracked_state, ("list", "tracked"), 1),
    Case("doctor-unhealthy", _missing_repo_path, ("doctor",), 1),
    Case("failing-ignore-command", _failing_ignore_command, ("push", "--dry-run"), 1, (True,)),
    Case("failing-hook", _sync_repo('[targets.unit.hooks]\npre_push = "exit 4"'), ("push",), 1, (True,)),
    Case(
        "tty-hook-unavailable-with-no-confirm",
        _sync_repo('[targets.unit.hooks]\npre_push = {run = "true", io = "tty"}'),
        ("push",), 1, (True,),
    ),
    Case("unattended-decision-required", _example_repo, ("edit", "config"), 1, (True,)),
    Case("sync-base-store-error", _failing_store_read, ("info", "sync-base", "main:app.unit"), 1),
]


@pytest.mark.parametrize(
    ("case", "no_confirm"),
    [
        pytest.param(case, no_confirm, id=f"{case.id}-{'no-confirm' if no_confirm else 'default'}")
        for case in CASES
        for no_confirm in case.no_confirm_modes
    ],
)
def test_exit_code_table(case: Case, no_confirm: bool, tmp_path, monkeypatch, capsys) -> None:
    config_path = case.build(tmp_path, monkeypatch)
    argv = [
        *(["--config", str(config_path)] if config_path is not None else []),
        *(["--no-confirm"] if no_confirm else []),
        *case.argv,
    ]

    try:
        exit_code = main(argv)
    except SystemExit as exit_info:  # argparse exits instead of returning
        exit_code = exit_info.code

    captured = capsys.readouterr()
    assert exit_code == case.expected, captured.err
    if case.stdout_is_json_document:
        assert json.loads(captured.out)["status"] == "failed"
