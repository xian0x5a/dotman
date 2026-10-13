from __future__ import annotations

import json
import subprocess

import pytest

from dotman.config import load_manager_config
from dotman.models import DEFAULT_COMMIT_MESSAGE, ResolvedSyncTarget
from dotman.sync_commit import render_commit_message
from dotman.sync_session import BatchSetApproval, Preview, SetApproval, SetCommit, SetResolutionIntent
from tests.engine.test_sync_session import make_engine
from tests.helpers import write_named_manager_config


def git(repo, *args) -> str:
    return subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True).stdout


def select_commit(session, repo, selected=True):
    view = session.view
    session.dispatch(SetCommit(view.session_id, view.revision, repo, selected))


TWO_DRIFTED = [("first", "both", b"repo first", b"live first", ""),
               ("second", "both", b"repo second", b"live second", "")]


@pytest.mark.parametrize("open_name", ["open_sync_session", "open_pull_session"])
def test_git_repo_offers_unselected_commit_work(tmp_path, monkeypatch, open_name):
    engine = make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    with getattr(engine, open_name)(engine.resolve_sync_scope()) as session:
        [option] = session.view.commit_options
        assert option.repo == "main"
        assert not option.selected
        assert option.branch == git(tmp_path / "repo", "branch", "--show-current").strip()


def test_commit_option_preselects_commit_work(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    with engine.open_pull_session(engine.resolve_sync_scope(), commit=True) as session:
        assert session.view.commit_options[0].selected


def test_push_and_non_git_repos_offer_no_commit_work(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    with engine.open_push_session(engine.resolve_sync_scope(), commit=True) as session:
        assert session.view.commit_options == ()
    (tmp_path / "repo/.git").rename(tmp_path / "moved-git")
    with engine.open_sync_session(engine.resolve_sync_scope(), commit=True) as session:
        assert session.view.commit_options == ()


@pytest.mark.parametrize("selected", [True, False])
def test_batch_selection_never_changes_commit_work(tmp_path, monkeypatch, selected):
    engine = make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    with engine.open_sync_session(engine.resolve_sync_scope(), commit=not selected) as session:
        view = session.view
        session.dispatch(BatchSetApproval(view.session_id, view.revision, selected,
                                          tuple(row.row_id for row in view.rows)))
        assert session.view.commit_options[0].selected is not selected


def test_pull_commits_only_written_sources(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    repo = tmp_path / "repo"
    (repo / "notes").write_text("unrelated staged work")
    git(repo, "add", "notes")
    (repo / "profiles/default.toml").write_text("# unrelated unstaged work")
    head = git(repo, "rev-parse", "HEAD").strip()
    with engine.open_pull_session(engine.resolve_sync_scope(), commit=True) as session:
        result = session.execute().result
    assert result.status == "completed"
    [commit] = result.commits
    assert (commit.repo, commit.status) == ("main", "committed")
    assert git(repo, "rev-parse", "--short", "HEAD").strip() == commit.commit
    assert git(repo, "rev-parse", "HEAD~1").strip() == head
    assert git(repo, "log", "-1", "--format=%B").strip() == "chore(dotman): pull app (2 targets)\n\napp"
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == [
        "packages/app/first", "packages/app/second",
    ]
    # Unrelated work keeps its exact index and worktree state.
    assert git(repo, "status", "--porcelain").splitlines() == ["A  notes", " M profiles/default.toml"]


def test_sync_commit_names_single_target_and_skips_unselected(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [("first", "both", b"repo", b"live", "")])
    repo = tmp_path / "repo"
    with engine.open_sync_session(engine.resolve_sync_scope()) as session:
        view = session.view
        # Use live writes the repository, so there is something to commit.
        session.dispatch(SetResolutionIntent(view.session_id, view.revision, "main:app.first", "use-live"))
        view = session.view
        session.dispatch(SetApproval(view.session_id, view.revision, "main:app.first", True))
        select_commit(session, "main")
        result = session.execute().result
    assert result.status == "completed", result
    assert git(repo, "log", "-1", "--format=%s").strip() == "chore(dotman): sync app.first"

    head = git(repo, "rev-parse", "HEAD")
    (tmp_path / "live/first").write_bytes(b"changed again")
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert session.execute().result.commits == ()
    assert git(repo, "rev-parse", "HEAD") == head


def test_failed_run_skips_commit(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, [
        ("first", "both", b"repo", b"live", '[targets.first.hooks]\npost_pull = "exit 9"'),
    ])
    repo = tmp_path / "repo"
    head = git(repo, "rev-parse", "HEAD")
    with engine.open_pull_session(engine.resolve_sync_scope(), commit=True) as session:
        result = session.execute().result
    assert result.status == "failed"
    assert [(commit.repo, commit.status) for commit in result.commits] == [("main", "skipped")]
    assert git(repo, "rev-parse", "HEAD") == head


def test_preview_never_commits(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    head = git(tmp_path / "repo", "rev-parse", "HEAD")
    with engine.open_pull_session(engine.resolve_sync_scope(), preview=True, commit=True) as session:
        view = session.view
        assert session.dispatch(Preview(view.session_id, view.revision)).result.commits == ()
    assert git(tmp_path / "repo", "rev-parse", "HEAD") == head


def targets(*specs):
    return tuple(ResolvedSyncTarget("main", package, target, profile) for package, target, profile in specs)


@pytest.mark.parametrize("specs,summary,packages", [
    ([("zsh", "zshrc", None)], "pull zsh.zshrc", "zsh"),
    ([("git", "config", "work"), ("git", "ignore", "work")], "pull git<work> (2 targets)", "git<work>"),
    ([("zsh", "zshrc", None), ("nvim", "init", None), ("nvim", "lazy", None)],
     "pull 3 targets in 2 packages", "nvim\nzsh"),
])
def test_default_message_names_narrowest_scope(specs, summary, packages):
    assert render_commit_message(DEFAULT_COMMIT_MESSAGE, operation="pull", repo="main",
                                 targets=targets(*specs)) == f"chore(dotman): {summary}\n\n{packages}"


def test_custom_message_template_and_validation(tmp_path):
    config = write_named_manager_config(tmp_path, {"main": tmp_path})
    config.write_text(config.read_text() + '\n[git]\ncommit_message = "dots({repo}): {operation} {count}"\n')
    template = load_manager_config(config).git.commit_message
    assert render_commit_message(template, operation="sync", repo="main",
                                 targets=targets(("zsh", "zshrc", None))) == "dots(main): sync 1"
    config.write_text(config.read_text().replace("{count}", "{targets}"))
    with pytest.raises(ValueError, match=r"git.commit_message.*\{targets\}"):
        load_manager_config(config)


def write_commit_message_config(tmp_path, template):
    config = write_named_manager_config(tmp_path, {"main": tmp_path})
    # A JSON string literal is a valid TOML basic string, so escapes like \n survive.
    config.write_text(config.read_text() + f"\n[git]\ncommit_message = {json.dumps(template)}\n")
    return config


@pytest.mark.parametrize("template", [
    "{count:s}", "{operation:d}", "{repo!x}", "{summary:{bogus}}",
])
def test_unrenderable_message_template_fails_config_loading(tmp_path, template):
    config = write_commit_message_config(tmp_path, template)
    with pytest.raises(ValueError, match=r"git\.commit_message"):
        load_manager_config(config)


@pytest.mark.parametrize("template", [
    "{summary}", "dotman: {summary}\n\n{packages}", "{count:>3} {repo}",
])
def test_valid_message_template_loads_and_renders(tmp_path, template):
    config = write_commit_message_config(tmp_path, template)
    loaded = load_manager_config(config).git.commit_message
    assert loaded == template
    render_commit_message(loaded, operation="sync", repo="main", targets=targets(("zsh", "zshrc", None)))
