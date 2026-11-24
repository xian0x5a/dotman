"""The workset order follows dependencies and declaration order, not the order selectors are typed."""

import json

from dotman.cli import main
from tests.engine.test_probe_after import build, log_lines
from tests.helpers import write_sync_repository, write_tracked_packages_state


def push(config, capsys, *args):
    code = main(["--config", str(config), "--json", "--no-confirm", "push", *args])
    return code, json.loads(capsys.readouterr().out)


def units(document):
    return [unit["identity"] for unit in document["sync_units"]]


def test_no_deps_package_selectors_still_run_dependencies_first(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = write_sync_repository(
        tmp_path, [("own", "push-only", b"new", None, f'[targets.own.hooks]\npost_push = "echo app >> {log}"')],
        dependency_targets=[("shared", "push-only", b"new", None, "")],
        dependency_extra=f'[hooks]\npost_push = "echo base >> {log}"')
    code, _document = push(config, capsys, "--no-deps", "main:app", "main:base")
    assert code == 0
    assert log_lines(tmp_path) == ["base", "app"]


def test_targets_of_one_package_keep_declaration_order(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = write_sync_repository(tmp_path, [
        ("a", "push-only", b"new", None, ""), ("b", "push-only", b"new", None, "")])
    _code, document = push(config, capsys, "--dry-run", "main:app.b", "main:app.a")
    assert units(document) == ["main:app.a", "main:app.b"]


def test_unrelated_packages_keep_selector_order(tmp_path, monkeypatch, capsys):
    # Guard: may already pass. `base` sorts after `app`, so selector order is observable.
    config = build(tmp_path, monkeypatch, '[targets.x]\nsource = "x"\npath = "%s/live/x"\ntype = "file"\n'
                   'sync_policy = "push-only"\n' % tmp_path)
    (tmp_path / "repo/packages/app/x").write_bytes(b"x")
    # Track both packages so neither is only a dependency.
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default"), ("base", "default")])
    _code, document = push(config, capsys, "--dry-run", "main:base.conf", "main:app.x")
    assert units(document) == ["main:base.conf", "main:app.x"]


def test_selector_less_push_runs_a_dependency_with_its_own_tracked_profile_first(tmp_path, monkeypatch, capsys):
    # `app@p1` pulls in `base@p1` implicitly; the explicit `base@p2` replaces it and is tracked after `app`.
    from tests.engine.test_probe_after import app_manifest
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path))
    for profile in ("p1", "p2"):
        (tmp_path / "repo/profiles" / f"{profile}.toml").write_text("")
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "p1"), ("base", "p2")])
    code, _document = push(config, capsys)
    assert code == 0
    assert log_lines(tmp_path) == ["probe", "hook"]
