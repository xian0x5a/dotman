from __future__ import annotations

import json
import subprocess

import pytest

from dotman.command_runtime import INTERRUPTED_EXIT_CODE, CommandResult, MemoryCommandRuntime, ProductionCommandRuntime
from dotman.config import load_manager_config
from dotman.engine import DotmanEngine
from dotman.models import DEFAULT_COMMIT_MESSAGE, ResolvedSyncTarget
from dotman.sync_commit import commit_sources, committable_paths, probe_work_tree, render_commit_message
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


@pytest.mark.parametrize("source,sibling", [("star*", "start"), ("st?r", "star"), ("[s]tar", "star")])
def test_pull_commit_treats_source_paths_literally(tmp_path, monkeypatch, source, sibling):
    make_engine(tmp_path, monkeypatch, [("glob", "both", b"repo", b"live", "")])
    repo = tmp_path / "repo"
    package = repo / "packages/app"
    # Rename the source to one with glob magic, next to a tracked file the glob would match.
    (package / "package.toml").write_text(
        (package / "package.toml").read_text().replace('source = "glob"', f'source = "{source}"'))
    (package / "glob").rename(package / source)
    (package / sibling).write_text("sibling")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "glob source")
    (package / sibling).write_text("unrelated unstaged work")
    engine = DotmanEngine.from_config_path(tmp_path / "config.toml")
    with engine.open_pull_session(engine.resolve_sync_scope(), commit=True) as session:
        result = session.execute().result
    assert result.status == "completed", result
    assert [(commit.repo, commit.status) for commit in result.commits] == [("main", "committed")]
    assert (package / source).read_bytes() == b"live"
    assert git(repo, "show", "--name-only", "--format=", "-z", "HEAD").split("\0")[:-1] == [
        f"packages/app/{source}",
    ]
    # Unrelated work keeps its exact index and worktree state.
    assert git(repo, "status", "--porcelain", "-z").split("\0")[:-1] == [f" M packages/app/{sibling}"]


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


@pytest.mark.parametrize("outcome,interruption", [
    (InterruptedError("command operation cancelled"), InterruptedError),
    (CommandResult(exit_code=INTERRUPTED_EXIT_CODE), KeyboardInterrupt),
], ids=["cancelled-before-launch", "rev-parse-interrupted"])
def test_probe_propagates_interruption(tmp_path, outcome, interruption):
    runtime = MemoryCommandRuntime([outcome, CommandResult(exit_code=0, stdout=b"main\n")])
    with pytest.raises(interruption):
        probe_work_tree(tmp_path, runtime)


@pytest.mark.parametrize("outcome", [
    FileNotFoundError("git"),
    CommandResult(exit_code=128, stderr=b"fatal: not a git repository"),
], ids=["no-git-executable", "not-a-repo"])
def test_probe_reports_no_work_tree_when_git_cannot_commit(tmp_path, outcome):
    assert probe_work_tree(tmp_path, MemoryCommandRuntime([outcome])) is None


def ignore_untracked(repo, *names):
    """Turn committed sources into untracked, git-ignored machine-local sources."""
    (repo / ".gitignore").write_text("".join(f"/packages/app/{name}\n" for name in names))
    git(repo, "rm", "-q", "--cached", *(f"packages/app/{name}" for name in names))
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore")


def test_commit_leaves_out_ignored_untracked_sources(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    repo = tmp_path / "repo"
    ignore_untracked(repo, "second")
    with engine.open_pull_session(engine.resolve_sync_scope(), commit=True) as session:
        result = session.execute().result
    assert result.status == "completed", result
    assert [(commit.repo, commit.status) for commit in result.commits] == [("main", "committed")]
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["packages/app/first"]
    # The message names only the target the commit records.
    subject = git(repo, "log", "-1", "--format=%s").strip()
    assert subject == "chore(dotman): pull app.first"
    assert result.commits[0].message.splitlines()[0] == subject
    assert (repo / "packages/app/second").read_bytes() == b"live second"
    # Nothing stays staged, and the ignored source stays untracked.
    assert git(repo, "status", "--porcelain").splitlines() == []


@pytest.mark.parametrize("ignore_rule", [
    pytest.param("/packages/app/first", id="source-file-ignored"),
    # `git add` refuses any path under an ignored directory, tracked or not (#260).
    pytest.param("/packages/app/", id="source-directory-ignored"),
])
def test_commit_keeps_tracked_sources_that_gitignore_matches(tmp_path, monkeypatch, ignore_rule):
    engine = make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    repo = tmp_path / "repo"
    # Git keeps tracking a file that .gitignore starts matching later.
    (repo / ".gitignore").write_text(f"{ignore_rule}\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore")
    with engine.open_pull_session(engine.resolve_sync_scope(), commit=True) as session:
        result = session.execute().result
    assert result.status == "completed", result
    assert sorted(git(repo, "show", "--name-only", "--format=", "HEAD").split()) == [
        "packages/app/first", "packages/app/second",
    ]


def test_commit_reports_no_changes_when_every_written_source_is_ignored(tmp_path, monkeypatch):
    engine = make_engine(tmp_path, monkeypatch, TWO_DRIFTED)
    repo = tmp_path / "repo"
    ignore_untracked(repo, "first", "second")
    head = git(repo, "rev-parse", "HEAD")
    with engine.open_pull_session(engine.resolve_sync_scope(), commit=True) as session:
        result = session.execute().result
    assert result.status == "completed", result
    assert [(commit.repo, commit.status) for commit in result.commits] == [("main", "no-changes")]
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain").splitlines() == []


def staged_work_tree(tmp_path, files, *, gitignore=""):
    """A git work tree whose HEAD holds `files` (force-added past `gitignore`)."""
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "test")
    (root / ".gitignore").write_text(gitignore)
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    git(root, "add", "--force", ".gitignore", *files)
    git(root, "commit", "-qm", "init")
    return root


def commit_written_sources(root, sources):
    runtime = ProductionCommandRuntime()
    paths = committable_paths(root, tuple(root / source for source in sources), runtime)
    # Sync calls commit_sources only when committable_paths found changes.
    return commit_sources(root, paths, "commit sources", runtime) if paths else None


def test_commit_keeps_a_staged_deletion_inside_a_directory_source(tmp_path):
    root = staged_work_tree(tmp_path, {"src/written": "old", "src/removed": "old"})
    git(root, "rm", "-q", "src/removed")
    (root / "src/written").write_text("new")

    assert commit_written_sources(root, ["src"]) is not None
    assert git(root, "show", "--name-status", "--format=", "HEAD").splitlines() == [
        "D\tsrc/removed", "M\tsrc/written",
    ]


@pytest.mark.parametrize("gitignore,files,untracked,written,sources,committed", [
    pytest.param("/cache/\n", {"cache/kept": "old"}, "cache/kept", "cache/kept", ["cache/kept"], None,
                 id="file-source"),
    pytest.param("*.log\n", {"src/a.log": "old", "src/b": "old"}, "src/a.log", "src/b", ["src"], ["M\tsrc/b"],
                 id="directory-source"),
])
def test_commit_keeps_an_ignored_source_the_user_untracked_out_of_history(
    tmp_path, gitignore, files, untracked, written, sources, committed,
):
    root = staged_work_tree(tmp_path, files, gitignore=gitignore)
    # `git rm --cached` keeps the ignored file on disk but stops tracking it.
    git(root, "rm", "-q", "--cached", untracked)
    (root / written).write_text("new")
    head = git(root, "rev-parse", "HEAD")

    commit = commit_written_sources(root, sources)

    if committed is None:
        assert commit is None
        assert git(root, "rev-parse", "HEAD") == head
    else:
        assert git(root, "show", "--name-status", "--format=", "HEAD").splitlines() == committed
    # The user's staged deletion is left as it was.
    assert git(root, "status", "--porcelain", "--", untracked).splitlines() == [f"D  {untracked}"]


@pytest.mark.parametrize("nested_has_commit", [
    # `git add` cannot index a repository with nothing checked out (#281).
    pytest.param(False, id="nested-repo-without-commits"),
    # `git add` would record a gitlink, which restores as an empty directory.
    pytest.param(True, id="nested-repo-with-commit"),
])
def test_commit_leaves_a_nested_repository_out_of_a_directory_source(tmp_path, nested_has_commit):
    root = staged_work_tree(tmp_path, {"src/a": "old"})
    nested = root / "src/sub"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "f").write_text("nested")
    if nested_has_commit:
        git(nested, "add", "f")
        git(nested, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "nested")
    (root / "src/a").write_text("new")

    assert commit_written_sources(root, ["src"]) is not None
    assert git(root, "show", "--name-status", "--format=", "HEAD").splitlines() == ["M\tsrc/a"]
    # The nested repository stays as it was: untracked, not a gitlink.
    assert git(root, "status", "--porcelain", "--", "src").splitlines() == ["?? src/sub/"]


def test_commit_finds_nothing_when_only_a_nested_repository_changed(tmp_path):
    root = staged_work_tree(tmp_path, {"src/a": "same"})
    (root / "src/sub").mkdir()
    git(root / "src/sub", "init", "-q")
    (root / "src/sub/f").write_text("nested")
    head = git(root, "rev-parse", "HEAD")

    assert commit_written_sources(root, ["src"]) is None
    assert git(root, "rev-parse", "HEAD") == head
