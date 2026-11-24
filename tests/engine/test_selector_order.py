"""The workset order follows dependencies and declaration order, not the order selectors are typed."""

import json

from dotman.cli import main
from tests.helpers import write_sync_repository, write_tracked_packages_state


def hook(log, label):
    return f'[targets.NAME.hooks]\npost_push = "echo {label} >> {log}"'


def target(name, log, label, *, content=b"new"):
    """A drifted push-only file target whose post_push hook logs `label`."""
    return (name, "push-only", content, None, hook(log, label).replace("NAME", name))


def push(config, capsys, *args):
    code = main(["--config", str(config), "--json", "--no-confirm", "push", *args])
    return code, json.loads(capsys.readouterr().out)


def logged(tmp_path):
    return (tmp_path / "log").read_text().splitlines()


def units(document):
    return [unit["identity"] for unit in document["sync_units"]]


def two_packages(tmp_path, monkeypatch, **kwargs):
    """`app` depends on the untracked `base`; each has one hooked target."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    log = tmp_path / "log"
    return write_sync_repository(
        tmp_path, [target("x", log, "app")], dependency_targets=[target("y", log, "base")], **kwargs)


def test_no_deps_package_selectors_still_run_dependencies_first(tmp_path, monkeypatch, capsys):
    config = two_packages(tmp_path, monkeypatch)
    code, _document = push(config, capsys, "--no-deps", "main:app", "main:base")
    assert code == 0
    assert logged(tmp_path) == ["base", "app"]


def test_exact_targets_across_packages_run_dependencies_first(tmp_path, monkeypatch, capsys):
    config = two_packages(tmp_path, monkeypatch)
    code, document = push(config, capsys, "main:app.x", "main:base.y")
    assert code == 0
    assert logged(tmp_path) == ["base", "app"]
    assert units(document) == ["main:base.y", "main:app.x"]


def test_targets_of_one_package_keep_declaration_order(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = write_sync_repository(tmp_path, [
        ("a", "push-only", b"new", None, ""), ("b", "push-only", b"new", None, "")])
    _code, document = push(config, capsys, "--dry-run", "main:app.b", "main:app.a")
    assert units(document) == ["main:app.a", "main:app.b"]


def test_unrelated_packages_keep_selector_order(tmp_path, monkeypatch, capsys):
    # Guard: may already pass. `base` sorts after `app`, so selector order is observable.
    config = two_packages(tmp_path, monkeypatch)
    (tmp_path / "repo/packages/app/package.toml").write_text(
        f'[targets.x]\nsource = "x"\npath = "{tmp_path}/live/x"\ntype = "file"\nsync_policy = "push-only"\n')
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "default"), ("base", "default")])
    _code, document = push(config, capsys, "--dry-run", "main:base.y", "main:app.x")
    assert units(document) == ["main:base.y", "main:app.x"]


def test_selector_less_push_runs_a_dependency_with_its_own_tracked_profile_first(tmp_path, monkeypatch, capsys):
    # `app@p1` pulls in `base@p1` implicitly; the explicit `base@p2` replaces it and is tracked after `app`.
    config = two_packages(tmp_path, monkeypatch)
    for profile in ("p1", "p2"):
        (tmp_path / "repo/profiles" / f"{profile}.toml").write_text("")
    write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=[("app", "p1"), ("base", "p2")])
    code, _document = push(config, capsys)
    assert code == 0
    assert logged(tmp_path) == ["base", "app"]
