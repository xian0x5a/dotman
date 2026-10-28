from __future__ import annotations

import json
from pathlib import Path

import pytest

import dotman.add as add_module
import dotman.cli_interaction as cli
from dotman.cli import main
from dotman.command_runtime import ArgvCommand, CommandRequest, CommandResult
from dotman.interaction import ScriptedInteraction
from tests.helpers import write_named_manager_config


@pytest.mark.parametrize("setting", ["GIT_EDITOR", "core.editor"])
def test_edit_add_and_doctor_agree_on_an_editor_only_git_knows(tmp_path: Path, monkeypatch, capsys, setting: str) -> None:
    editor = tmp_path / "git-only-editor"
    editor.write_text("#!/bin/sh\n")
    editor.chmod(0o755)
    if setting == "GIT_EDITOR":
        monkeypatch.setenv("GIT_EDITOR", str(editor))
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.editor")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(editor))
    editor_runs: list[tuple[str, ...]] = []

    class EditorRecordingRuntime:
        def run(self, request: CommandRequest) -> CommandResult:
            assert isinstance(request.command, ArgvCommand)
            editor_runs.append(request.command.arguments)
            return CommandResult(exit_code=0)

    # Only editor launches are recorded; the git lookup still runs for real.
    monkeypatch.setattr(cli, "current_command_runtime", EditorRecordingRuntime)
    monkeypatch.setattr(add_module, "current_command_runtime", EditorRecordingRuntime)
    live_path = Path.home() / ".config" / "foo.conf"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("value\n", encoding="utf-8")
    repo_root = tmp_path / "repo"
    (repo_root / "packages" / "git").mkdir(parents=True)
    (repo_root / "packages" / "git" / "package.toml").write_text("", encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    assert main(["--config", str(config_path), "--json", "doctor"]) in {0, 1}
    editor_check = next(check for check in json.loads(capsys.readouterr().out)["checks"] if check["key"] == "editor")
    assert (editor_check["status"], editor_check["path"]) == ("ok", str(editor))

    assert main(["--config", str(config_path), "edit", "config"]) == 0
    assert editor_runs.pop() == (str(editor), str(config_path.resolve()))

    interaction = ScriptedInteraction(confirmations=[False])
    assert main(["--config", str(config_path), "add", str(live_path), "fixture:git"], interaction=interaction) == 0
    add_editor, review_path, _editable_manifest = editor_runs.pop()
    assert (add_editor, Path(review_path).name) == (str(editor), "add-review.md")
