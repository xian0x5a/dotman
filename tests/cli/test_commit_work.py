from __future__ import annotations

import json

import pytest

from dotman.cli import main
from tests.engine.test_sync_commit import TWO_DRIFTED, git
from tests.engine.test_sync_session import make_engine


@pytest.fixture
def pull_repo(tmp_path, monkeypatch):
    make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    return tmp_path


def test_no_confirm_pull_commit_reports_the_commit(pull_repo, capsys):
    assert main(["--config", str(pull_repo / "config.toml"), "--json", "--no-confirm", "pull", "--commit"]) == 0

    payload = json.loads(capsys.readouterr().out)
    head = git(pull_repo / "repo", "rev-parse", "--short", "HEAD").strip()
    assert payload["commit_work"] == [{
        "repo": "main", "branch": git(pull_repo / "repo", "branch", "--show-current").strip(),
        "selected": True, "result": "committed", "commit": head,
        "message": "chore(dotman): pull app (2 targets)\n\napp", "error": None,
    }]
    assert payload["summary"]["commits"] == 1


def test_pull_without_commit_leaves_history_alone(pull_repo, capsys):
    head = git(pull_repo / "repo", "rev-parse", "HEAD")
    assert main(["--config", str(pull_repo / "config.toml"), "--json", "--no-confirm", "pull"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [(item["selected"], item["result"]) for item in payload["commit_work"]] == [(False, None)]
    assert git(pull_repo / "repo", "rev-parse", "HEAD") == head


def test_deck_opens_with_commit_work_selected(pull_repo, monkeypatch):
    import sys

    from dotman import sync_deck

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    seen = []

    def confirm(session, *, use_color):
        seen.extend(option.selected for option in session.view.commit_options)
        return True

    monkeypatch.setattr(sync_deck, "run_command_deck", confirm)
    head = git(pull_repo / "repo", "rev-parse", "HEAD")
    assert main(["--config", str(pull_repo / "config.toml"), "pull"]) == 0

    assert seen == [True]
    assert git(pull_repo / "repo", "rev-parse", "HEAD") != head


def test_deck_uncheck_keeps_history(pull_repo, monkeypatch):
    import sys

    from dotman import sync_deck
    from dotman.sync_session import SetCommit

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def uncheck(session, *, use_color):
        view = session.view
        session.dispatch(SetCommit(view.session_id, view.revision, "main", False))
        return True

    monkeypatch.setattr(sync_deck, "run_command_deck", uncheck)
    head = git(pull_repo / "repo", "rev-parse", "HEAD")
    assert main(["--config", str(pull_repo / "config.toml"), "pull"]) == 0

    assert git(pull_repo / "repo", "rev-parse", "HEAD") == head


def test_human_timeline_shows_commit_step(pull_repo, capsys):
    assert main(["--config", str(pull_repo / "config.toml"), "--no-confirm", "pull", "--commit"]) == 0

    output = capsys.readouterr().out
    assert "  main\n    [1/1] commit      chore(dotman): pull app (2 targets)\n      ok\n" in output
    assert "commits: 1" in output.splitlines()[-1]


def test_human_recap_names_a_commit_skipped_by_failure(tmp_path, monkeypatch, capsys):
    make_engine(tmp_path, monkeypatch, [
        ("first", "both", b"repo", b"live", '[targets.first.hooks]\npost_pull = "exit 9"'),
    ])
    assert main(["--config", str(tmp_path / "config.toml"), "--no-confirm", "pull", "--commit"]) == 1

    assert "  [skipped] main commit (earlier work failed)" in capsys.readouterr().out


def test_push_has_no_commit_flag(pull_repo):
    with pytest.raises(SystemExit):
        main(["--config", str(pull_repo / "config.toml"), "push", "--commit"])

