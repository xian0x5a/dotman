import json
import shlex

import pytest

from dotman.sync_base_store import FilePresent
from dotman.sync_session import CommandAccepted, EditProposal
from tests.engine.test_sync_session import make_engine

from dotman.engine import DotmanEngine
from tests.helpers import write_single_repo_config


def edit_first(session):
    view = session.view
    result = session.dispatch(EditProposal(view.session_id, view.revision, view.rows[0].row_id))
    assert isinstance(result, CommandAccepted), result
    return result


def test_pull_default_editor_gets_frozen_primary_additional_and_review_environment(tmp_path, monkeypatch):
    editor = tmp_path / "editor"
    marker = tmp_path / "editor-inputs"
    editor.write_text(
        "#!/bin/sh\n"
        f'cat "$1" "$2" > {shlex.quote(str(marker))}\n'
        'test "$1" = "$DOTMAN_REPO_PATH" || exit 7\n'
        'test -f "$DOTMAN_EDITOR_REVIEW_PATH" || exit 8\n'
        'test "$#" = 2 || exit 9\n'
        'printf edited > "$1"\n'
    )
    editor.chmod(0o755)
    monkeypatch.setenv("VISUAL", str(editor))
    engine = make_engine(tmp_path, monkeypatch, [(
        "unit", "both", b"repo", b"live",
        'editor = {type = "default", io = "pipe", additional_sources = ["include"]}',
    )])
    primary = tmp_path / "repo/packages/app/unit"
    additional = primary.with_name("include")
    additional.write_bytes(b"include")
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        additional.write_bytes(b"external")
        saved = edit_first(session)
        assert saved.result.status == "saved"
        assert marker.read_bytes() == b"liveinclude"
        assert primary.read_bytes() == b"repo"
        assert saved.view.rows[0].proposal.repository == FilePresent(b"edited")
        assert session.execute().result.status == "completed"
    assert primary.read_bytes() == b"edited"
    assert additional.read_bytes() == b"external"


@pytest.mark.parametrize("exit_code,status", [(7, "command-failed"), (130, "cancelled")])
def test_pull_editor_failure_or_cancel_never_leaks_staged_changes(tmp_path, monkeypatch, exit_code, status):
    command = json.dumps(f'printf changed > "$DOTMAN_SOURCE"; printf extra > "$DOTMAN_EDITOR_ADDITIONAL_SOURCE_PATHS"; exit {exit_code}')
    engine = make_engine(tmp_path, monkeypatch, [(
        "unit", "both", b"repo", b"live",
        f'editor = {{run = {command}, io = "pipe", additional_sources = ["include"]}}',
    )])
    primary = tmp_path / "repo/packages/app/unit"
    additional = primary.with_name("include")
    additional.write_bytes(b"include")
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        previous = session.view.rows[0].proposal
        result = edit_first(session)
        assert result.result.status == status
        assert result.view.rows[0].approved
        assert result.view.rows[0].proposal == previous
        assert len(result.view.rows) == 1
    assert primary.read_bytes() == b"repo"
    assert additional.read_bytes() == b"include"


def test_pull_jinja_editor_discovers_nested_dependencies_after_configured_sources(tmp_path, monkeypatch):
    editor = tmp_path / "editor"
    marker = tmp_path / "editor-inputs"
    editor.write_text(
        '#!/bin/sh\n'
        'test "$#" = 4 || exit 9\n'
        f'cat "$2" "$3" "$4" > {shlex.quote(str(marker))}\n'
    )
    editor.chmod(0o755)
    monkeypatch.setenv("VISUAL", str(editor))
    engine = make_engine(tmp_path, monkeypatch, [(
        "unit", "both", b"{% include 'nested.j2' %}", b"live",
        'editor = {type = "jinja", io = "pipe", additional_sources = ["include"]}',
    )])
    package = tmp_path / "repo/packages/app"
    (package / "include").write_bytes(b"configured")
    (package / "nested.j2").write_bytes(b"{% include 'leaf.j2' %}")
    (package / "leaf.j2").write_bytes(b"leaf")
    with engine.open_pull_session(engine.resolve_sync_scope(), preview=True) as session:
        assert edit_first(session).result.status == "unchanged"
    assert marker.read_bytes() == b"configured{% include 'leaf.j2' %}leaf"


@pytest.mark.parametrize("discovery", ["VISUAL", "EDITOR", "GIT_EDITOR", "git"])
def test_pull_default_editor_discovery(tmp_path, monkeypatch, discovery):
    editor = tmp_path / "editor"
    editor.write_text('#!/bin/sh\nprintf edited > "$1"\n')
    editor.chmod(0o755)
    engine = make_engine(tmp_path, monkeypatch, [(
        "unit", "both", b"repo", b"live", 'editor = {type = "default", io = "pipe"}',
    )])
    if discovery == "git":
        # Editor lookup runs inside the isolated repository workspace.
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.editor")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(editor))
    else:
        monkeypatch.setenv(discovery, str(editor))
    with engine.open_pull_session(engine.resolve_sync_scope(), preview=True) as session:
        assert edit_first(session).result.status == "saved"
        assert session.view.rows[0].proposal.repository == FilePresent(b"edited")


def test_pull_custom_editor_runs_in_isolated_package_root(tmp_path, monkeypatch):
    command = json.dumps('test "$PWD" = "$DOTMAN_PACKAGE_ROOT" && cat context > "$DOTMAN_SOURCE"')
    engine = make_engine(tmp_path, monkeypatch, [(
        "unit", "both", b"repo", b"live",
        f'editor = {{run = {command}, io = "pipe", additional_sources = ["context"]}}',
    )])
    (tmp_path / "repo/packages/app/context").write_bytes(b"context")
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert edit_first(session).result.status == "saved"
        assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"repo"
        assert session.execute().result.status == "completed"
    assert (tmp_path / "repo/packages/app/unit").read_bytes() == b"context"


@pytest.mark.parametrize("append", [False, True])
def test_pull_inherited_editor_sources_keep_each_declaring_package_root(tmp_path, monkeypatch, append):
    from tests.helpers import open_tracked_pull_session, initialize_git_repository

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    parent = repo / "packages/parent"
    child = repo / "packages/child"
    parent.mkdir(parents=True)
    child.mkdir()
    (repo / "profiles").mkdir()
    (repo / "profiles/default.toml").write_text("")
    (parent / "context").write_bytes(b"parent")
    (child / "context").write_bytes(b"child")
    (child / "unit").write_bytes(b"repo")
    marker = tmp_path / "source-order"
    command = json.dumps(f'cat "$2" "$3" > {marker}' if append else f'cat "$2" > {marker}')
    (parent / "package.toml").write_text(
        '[targets.unit]\nsource = "unit"\npath = "~/unit"\n'
        f'editor = {{run = {command}, io = "pipe", additional_sources = ["context"]}}\n'
    )
    (child / "package.toml").write_text(
        'extends = ["parent"]\n[targets.unit]\nsource = "unit"\npath = "~/unit"\n'
        + ('[append.targets.unit.editor]\nadditional_sources = ["context"]\n' if append else "")
    )
    (tmp_path / "home").mkdir()
    (tmp_path / "home/unit").write_bytes(b"live")
    initialize_git_repository(repo)
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo))
    with open_tracked_pull_session(engine, tmp_path, entries=[("child", "default")]) as session:
        assert edit_first(session).result.status == "unchanged"
    assert marker.read_bytes() == (b"parentchild" if append else b"parent")
    assert (parent / "context").read_bytes() == b"parent"
    assert (child / "context").read_bytes() == b"child"
