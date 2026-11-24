"""`probe_after`: a probe that reads what other targets write in the same run."""

import json
import re

import pytest

from dotman.cli import main
from dotman.engine import DotmanEngine
from dotman.errors import UsageError
from tests.helpers import write_sync_repository

# Base writes `live/conf` on push; base_toml adds more targets to the package.
BASE_FILE = ("conf", "push-only", b"new", None, "")


def probe_command(tmp_path, *, exit_when_present=0, exit_when_absent=100, watched="conf"):
    """Log each run, then report on whether the watched live file exists."""
    return (f"echo probe >> {tmp_path / 'log'}; "
            f"if [ -f {tmp_path / 'live' / watched} ]; then exit {exit_when_present}; "
            f"else exit {exit_when_absent}; fi")


def build(tmp_path, monkeypatch, app_toml, *, base_files=(BASE_FILE,), base_extra="", base_toml=None,
          extra_packages=None):
    """A tracked `app` that depends on `base`; `app_toml` replaces app's manifest after `depends`."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = write_sync_repository(
        tmp_path, [("x", "both", None, None, "")], dependency_targets=list(base_files),
        dependency_extra=base_extra)
    packages = tmp_path / "repo/packages"
    if base_toml is not None:
        (packages / "base/package.toml").write_text(base_toml)
    for name, toml in (extra_packages or {}).items():
        (packages / name).mkdir()
        (packages / name / "package.toml").write_text(toml)
    (packages / "app/package.toml").write_text(app_toml)
    return config


def app_manifest(tmp_path, *, probe_after='["base.conf"]', command=None, hooks=None, depends='["base"]',
                 package_hooks="", extra=""):
    command = command or probe_command(tmp_path)
    after = f"probe_after = {probe_after}" if probe_after is not None else ""
    hook_lines = hooks if hooks is not None else f'post_push = "echo hook >> {tmp_path / "log"}"'
    return f'''depends = {depends}
{package_hooks}
[targets.check]
sync_policy = "push-only"
probe = "{command}"
{after}
[targets.check.hooks]
{hook_lines}
{extra}
'''


def push(config, capsys, *args):
    code = main(["--config", str(config), "--json", "--no-confirm", "push", *args])
    return code, json.loads(capsys.readouterr().out)


def log_lines(tmp_path):
    log = tmp_path / "log"
    return log.read_text().splitlines() if log.exists() else []


def probe_row(document, identity="main:app.check"):
    return next((work for work in document["probe_work"] if work["identity"] == identity), None)


def step_kinds(document, identity="main:app.check"):
    return [(step["kind"], step["action"], step["status"], step["skip_reason"])
            for step in document["stages"] if step["scope_identity"] == identity]


# --- 1. configuration validation -------------------------------------------------

def target_with(after):
    return f'''depends = ["base"]
[targets.check]
probe = "true"
probe_after = {after}
'''


@pytest.mark.parametrize("app_toml, message", [
    ('[targets.check]\nsync_policy = "push-only"\nprobe_after = ["app"]\n', "must define probe"),
    *[(target_with(f'["{entry}"]'), "must be 'package' or 'package.target'")
      for entry in ("a..b", ".x", "a.", "a.b.c")],
])
def test_probe_after_shape_errors_surface_at_package_resolution(tmp_path, monkeypatch, app_toml, message):
    config = build(tmp_path, monkeypatch, app_toml)
    with pytest.raises(UsageError, match=message):
        DotmanEngine.from_config_path(config).resolve_sync_scope()


@pytest.mark.parametrize("entry, message", [
    ("outside", "depends closure"),
    ("base.missing", "names no target"),
    ("base.off", "names no target"),
])
def test_probe_after_entries_outside_the_graph_fail_when_planning(tmp_path, monkeypatch, entry, message):
    base = '''[targets.conf]
source = "conf"
path = "%s/live/conf"
type = "file"
sync_policy = "push-only"
[targets.off]
disabled = true
source = "conf"
path = "%s/live/off"
type = "file"
''' % (tmp_path, tmp_path)
    config = build(tmp_path, monkeypatch, target_with(f'["{entry}"]'), base_toml=base,
                   extra_packages={"outside": '[targets.t]\nprobe = "true"\n'})
    engine = DotmanEngine.from_config_path(config)
    with pytest.raises(UsageError, match=message):
        engine.open_sync_session(engine.resolve_sync_scope(), preview=True)


@pytest.mark.parametrize("entry, depends, packages", [
    ("app", '["base"]', {}),
    ("base", '["base"]', {}),
    ("base.conf", '["base"]', {}),
    ("base.conf", '["mid"]', {"mid": 'depends = ["base"]\n'}),
    ("base", '["mid"]', {"mid": 'depends = ["base"]\n'}),
])
def test_valid_probe_after_entries_plan(tmp_path, monkeypatch, entry, depends, packages):
    app = app_manifest(tmp_path, probe_after=f'["{entry}"]', depends=depends, command="true")
    config = build(tmp_path, monkeypatch, app, extra_packages=packages)
    engine = DotmanEngine.from_config_path(config)
    session = engine.open_sync_session(engine.resolve_sync_scope(), preview=True)
    assert "main:app.check" in [row.row_id for row in session.view.rows]


# --- 2-3. conditional path --------------------------------------------------------

def test_conditional_probe_is_unprobed_at_planning_and_runs_once_before_its_hooks(tmp_path, monkeypatch, capsys):
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path))
    code, preview = push(config, capsys, "--dry-run")
    assert code == 0
    assert probe_row(preview)["conditional"] is True
    assert preview["stages"] == []
    assert log_lines(tmp_path) == []
    main(["--config", str(config), "--no-confirm", "push", "--dry-run"])
    assert re.search(r"\[would-check\] main:app\.check", capsys.readouterr().out)
    assert log_lines(tmp_path) == []

    code, document = push(config, capsys)
    assert code == 0
    assert (tmp_path / "live/conf").read_bytes() == b"new"
    assert log_lines(tmp_path) == ["probe", "hook"]
    assert probe_row(document)["conditional"] is True
    assert step_kinds(document) == [("probe", "probe", "ok", None), ("hook", "post_push", "ok", None)]


def test_inactive_recheck_skips_probe_and_hooks(tmp_path, monkeypatch, capsys):
    # The probe sees the new file at its turn but reports nothing to do.
    command = probe_command(tmp_path, exit_when_present=100)
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path, command=command))
    code, document = push(config, capsys)
    assert code == 0
    assert log_lines(tmp_path) == ["probe"]
    assert step_kinds(document) == [("probe", "probe", "skipped", "probe-inactive"),
                                    ("hook", "post_push", "skipped", "probe-inactive")]


def test_probe_failure_at_recheck_fails_the_stage_and_leaves_later_steps_unattempted(tmp_path, monkeypatch, capsys):
    command = probe_command(tmp_path, exit_when_present=3)
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path, command=command))
    code, document = push(config, capsys)
    assert code != 0
    assert document["status"] != "completed"
    assert log_lines(tmp_path) == ["probe"]
    assert step_kinds(document) == [("probe", "probe", "failed", None),
                                    ("hook", "post_push", "unattempted", "earlier-failure")]


# --- 5. no named-entry work: planning probes once --------------------------------

@pytest.mark.parametrize("exit_code, row_expected", [(0, True), (100, False)])
@pytest.mark.parametrize("unrelated", ["none", "unnamed-target", "other-package"])
def test_without_named_work_the_probe_runs_at_planning(tmp_path, monkeypatch, capsys, exit_code, row_expected, unrelated):
    # `conf` is in sync; unrelated drifted work elsewhere must not make the row conditional.
    base_files = [("conf", "push-only", b"same", b"same", "")]
    if unrelated == "other-package":
        base_files.append(("noise", "push-only", b"new", None, ""))
    elif unrelated == "unnamed-target":
        base_files = [("conf", "push-only", b"same", b"same", ""), ("noise", "push-only", b"new", None, "")]
    app = app_manifest(tmp_path, command=probe_command(tmp_path, exit_when_present=exit_code,
                                                      exit_when_absent=exit_code))
    if unrelated == "unnamed-target":
        # Same package as the probe: an unnamed sibling target with drift.
        app += f'''
[targets.sibling]
source = "sibling"
path = "{tmp_path}/live/sibling"
type = "file"
sync_policy = "push-only"
'''
    config = build(tmp_path, monkeypatch, app, base_files=base_files)
    if unrelated == "unnamed-target":
        (tmp_path / "repo/packages/app/sibling").write_bytes(b"s")
    _code, preview = push(config, capsys, "--dry-run")
    assert log_lines(tmp_path) == ["probe"]
    row = probe_row(preview)
    assert (row is not None) is row_expected
    if row_expected:
        assert row["conditional"] is False
    if unrelated != "none":
        assert preview["sync_units"], "unrelated drift is still planned"

    code, document = push(config, capsys)
    assert code == 0
    assert log_lines(tmp_path) == ["probe", "probe"] + (["hook"] if row_expected else [])
    assert all(kind != "probe" for kind, *_ in step_kinds(document))
    assert [step[:3] for step in step_kinds(document)] == ([("hook", "post_push", "ok")] if row_expected else [])


# --- 6. ordering inside the package ------------------------------------------------

def test_probe_after_target_declared_first_still_runs_after_its_packages_write(tmp_path, monkeypatch, capsys):
    app = f'''[targets.check]
sync_policy = "push-only"
probe = "{probe_command(tmp_path, watched="late")}"
probe_after = ["app.late"]
[targets.check.hooks]
post_push = "echo hook >> {tmp_path / 'log'}"

[targets.late]
source = "late"
path = "{tmp_path}/live/late"
type = "file"
sync_policy = "push-only"
'''
    config = build(tmp_path, monkeypatch, app, base_files=())
    (tmp_path / "repo/packages/app/late").write_bytes(b"late")
    code, document = push(config, capsys)
    assert code == 0
    assert log_lines(tmp_path) == ["probe", "hook"]
    assert [(step["scope_identity"], step["action"]) for step in document["stages"]] == [
        ("main:app.late", "write"), ("main:app.check", "probe"), ("main:app.check", "post_push")]


# --- 7. only the target's own hooks ----------------------------------------------

@pytest.mark.parametrize("named_work", [True, False])
def test_probe_after_target_activates_only_its_own_hooks(tmp_path, monkeypatch, capsys, named_work):
    log = tmp_path / "log"
    package_hooks = f'[hooks]\npre_push = "echo package-pre >> {log}"\npost_push = "echo package-post >> {log}"\n'
    base_files = [BASE_FILE] if named_work else [("conf", "push-only", b"same", b"same", "")]
    config = build(tmp_path, monkeypatch, app_manifest(
        tmp_path, command=probe_command(tmp_path, exit_when_absent=0), package_hooks=package_hooks,
        extra=""), base_files=base_files)
    (tmp_path / "repo/repo.toml").write_text(
        f'[hooks]\npre_push = "echo repo-pre >> {log}"\npost_push = "echo repo-post >> {log}"\n')
    code, _document = push(config, capsys)
    assert code == 0
    lines = log_lines(tmp_path)
    # Package hooks never fire; repo hooks fire only for the named input's own file work.
    assert not {"package-pre", "package-post"} & set(lines)
    assert [line for line in lines if line.startswith("repo")] == (
        ["repo-pre", "repo-post"] if named_work else [])
    assert [line for line in lines if not line.startswith("repo")] == ["probe", "hook"]


# --- 8. upstream auxiliary work counts -------------------------------------------

def test_active_probe_work_in_the_named_package_makes_the_row_conditional(tmp_path, monkeypatch, capsys):
    base = f'''[targets.tool]
sync_policy = "push-only"
probe = "exit 0"
'''
    app = app_manifest(tmp_path, probe_after='["base"]', command=probe_command(tmp_path, exit_when_absent=0))
    config = build(tmp_path, monkeypatch, app, base_toml=base)
    _code, preview = push(config, capsys, "--dry-run")
    assert probe_row(preview, "main:base.tool")["conditional"] is False
    assert probe_row(preview)["conditional"] is True
    assert log_lines(tmp_path) == []


# --- regression tests for review findings ------------------------------------------

def run_cli(config, capsys, *args, json_output=True):
    code = main(["--config", str(config), *(["--json"] if json_output else []), "--no-confirm", *args])
    output = capsys.readouterr().out
    return code, json.loads(output) if json_output else output


@pytest.mark.parametrize("variant", ["run-noop-flag", "manifest-run-noop"])
def test_noop_eligible_hooks_run_without_waiting_for_the_recheck(tmp_path, monkeypatch, capsys, variant):
    # Selected Hook Work runs them whatever the probe reports: nothing waits for a
    # recheck, so the probe runs once at planning and no probe step gates the hook.
    log = tmp_path / "log"
    command = probe_command(tmp_path, exit_when_present=100)
    if variant == "manifest-run-noop":
        hooks = f'post_push = {{run = "echo hook >> {log}", run_noop = true}}'
        args = ("push",)
    else:
        hooks = None
        args = ("push", "--run-noop")
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path, command=command, hooks=hooks))
    code, document = run_cli(config, capsys, *args)
    assert code == 0
    assert log_lines(tmp_path) == ["probe", "hook"]
    assert step_kinds(document) == [("hook", "post_push", "ok", None)]


PARENT_MANIFEST = '''[targets.check]
sync_policy = "push-only"
probe = "%(probe)s"
probe_after = ["parent.late"]
[targets.check.hooks]
post_push = "echo hook >> %(log)s"

[targets.late]
source = "late"
path = "%(root)s/live/late"
type = "file"
sync_policy = "push-only"
'''


def test_inherited_probe_after_naming_the_parent_refers_to_the_inheriting_package(tmp_path, monkeypatch, capsys):
    parent = PARENT_MANIFEST % dict(probe=probe_command(tmp_path, watched="late"), log=tmp_path / "log", root=tmp_path)
    config = build(tmp_path, monkeypatch, 'extends = ["parent"]\n', base_files=(),
                   extra_packages={"parent": parent})
    (tmp_path / "repo/packages/app/late").write_bytes(b"late")
    (tmp_path / "repo/packages/parent/late").write_bytes(b"late")
    # Planning must not reject the inherited entry.
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert probe_row(preview)["conditional"] is True
    code, document = run_cli(config, capsys, "push")
    assert code == 0
    assert log_lines(tmp_path) == ["probe", "hook"]
    assert [(step["scope_identity"], step["action"]) for step in document["stages"]] == [
        ("main:app.late", "write"), ("main:app.check", "probe"), ("main:app.check", "post_push")]


@pytest.mark.parametrize("entry", ["base.conf", "base"])
def test_probe_after_naming_a_package_that_depends_back_is_rejected(tmp_path, monkeypatch, capsys, entry):
    base = f'''depends = ["app"]
[targets.conf]
source = "conf"
path = "{tmp_path}/live/conf"
type = "file"
sync_policy = "push-only"
'''
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path, probe_after=f'["{entry}"]'), base_toml=base)
    engine = DotmanEngine.from_config_path(config)
    with pytest.raises(UsageError, match="depends"):
        engine.open_sync_session(engine.resolve_sync_scope(), preview=True)


def test_sync_runs_the_probe_step_only_in_the_stage_that_gates_hooks(tmp_path, monkeypatch, capsys):
    app = app_manifest(tmp_path).replace('sync_policy = "push-only"', 'sync_policy = "both"')
    config = build(tmp_path, monkeypatch, app)
    code, document = run_cli(config, capsys, "sync")
    assert code == 0
    assert [(step["stage"], step["status"]) for step in document["stages"] if step["kind"] == "probe"] == [
        ("live-publication", "ok")]
    assert log_lines(tmp_path) == ["probe", "hook"]


def test_recheck_failure_keeps_exit_code_in_json_and_output_in_human_text(tmp_path, monkeypatch, capsys):
    command = probe_command(tmp_path, exit_when_present=3).replace(
        "exit 3", "echo 'probe exploded' >&2; exit 3")
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path, command=command))
    code, document = run_cli(config, capsys, "push")
    assert code != 0
    step = next(step for step in document["stages"] if step["kind"] == "probe")
    assert step["exit_code"] == 3
    assert "probe exploded" not in (step["error"] or "")

    (tmp_path / "live/conf").unlink()
    code, output = run_cli(config, capsys, "push", json_output=False)
    assert code != 0
    assert "probe exploded" in output


def test_empty_probe_after_is_rejected_at_package_resolution(tmp_path, monkeypatch):
    config = build(tmp_path, monkeypatch, target_with("[]"))
    with pytest.raises(UsageError, match="probe_after"):
        DotmanEngine.from_config_path(config).resolve_sync_scope()


def test_pull_recheck_probe_sees_the_repository_write_before_its_hook(tmp_path, monkeypatch, capsys):
    # Coverage of existing behavior: Pull rechecks in `repository-apply`.
    repo_file = tmp_path / "repo/packages/base/conf"
    command = (f"echo probe >> {tmp_path / 'log'}; "
               f"if grep -q live-new {repo_file}; then exit 0; else exit 100; fi")
    app = app_manifest(tmp_path, command=command, hooks=f'post_pull = "echo hook >> {tmp_path / "log"}"'
                       ).replace("push-only", "pull-only")
    config = build(tmp_path, monkeypatch, app, base_files=[("conf", "pull-only", b"repo-old", b"live-new", "")])
    _code, preview = run_cli(config, capsys, "pull", "--dry-run")
    assert probe_row(preview)["conditional"] is True
    assert log_lines(tmp_path) == []
    code, document = run_cli(config, capsys, "pull")
    assert code == 0
    assert log_lines(tmp_path) == ["probe", "hook"]
    assert [(step["stage"], step["kind"], step["status"]) for step in document["stages"]
            if step["scope_identity"] == "main:app.check"] == [
        ("repository-apply", "probe", "ok"), ("repository-apply", "hook", "ok")]


@pytest.mark.parametrize("variant", ["run-noop-flag", "manifest-run-noop"])
def test_row_is_not_conditional_when_no_hook_waits_for_the_recheck(tmp_path, monkeypatch, capsys, variant):
    # Every hook is noop-eligible, so nothing waits for the recheck: the probe runs at planning.
    command = probe_command(tmp_path, exit_when_absent=0)
    if variant == "manifest-run-noop":
        hooks = f'post_push = {{run = "echo hook >> {tmp_path / "log"}", run_noop = true}}'
        args = ("push", "--dry-run")
    else:
        hooks = None
        args = ("push", "--dry-run", "--run-noop")
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path, command=command, hooks=hooks))
    _code, preview = run_cli(config, capsys, *args)
    assert preview["sync_units"], "the named entry has drift"
    assert probe_row(preview)["conditional"] is False
    assert log_lines(tmp_path) == ["probe"]
    _code, output = run_cli(config, capsys, *args[:1], *args[1:], json_output=False)
    assert "would-check" not in output


# --- chaining: probe_after targets decided in execution order ---------------------

def mid_manifest(tmp_path, *, after='["base.conf"]'):
    made = tmp_path / "live/made"
    log = tmp_path / "log"
    return f'''depends = ["base"]
[targets.link]
sync_policy = "push-only"
probe = "echo midprobe >> {log}; test ! -f {made}"
probe_after = {after}
[targets.link.hooks]
post_push = "echo midhook >> {log}; mkdir -p {tmp_path / 'live'}; touch {made}"
'''


def chain_app(tmp_path, after):
    command = (f"echo probe >> {tmp_path / 'log'}; "
               f"if [ -f {tmp_path / 'live/made'} ]; then exit 0; else exit 100; fi")
    return app_manifest(tmp_path, probe_after=after, command=command, depends='["mid"]')


IN_SYNC = [("conf", "push-only", b"same", b"same", "")]
CHAIN_STEPS = [("main:app.check", "probe", "ok"), ("main:app.check", "post_push", "ok")]


def chain_steps(document):
    return [(step["scope_identity"], step["action"], step["status"]) for step in document["stages"]
            if step["kind"] in ("probe", "hook")]


@pytest.mark.parametrize("after", ['["mid"]', '["mid.link"]'])
def test_planning_active_probe_after_row_makes_a_later_probe_after_target_conditional(
        tmp_path, monkeypatch, capsys, after):
    config = build(tmp_path, monkeypatch, chain_app(tmp_path, after), base_files=IN_SYNC,
                   extra_packages={"mid": mid_manifest(tmp_path)})
    code, document = run_cli(config, capsys, "push")
    assert code == 0
    assert probe_row(document, "main:mid.link")["conditional"] is False
    assert probe_row(document)["conditional"] is True
    assert chain_steps(document) == [("main:mid.link", "post_push", "ok"), *CHAIN_STEPS]
    assert log_lines(tmp_path) == ["midprobe", "midhook", "probe", "hook"]


def test_conditional_upstream_probe_after_row_makes_a_later_one_conditional(tmp_path, monkeypatch, capsys):
    config = build(tmp_path, monkeypatch, chain_app(tmp_path, '["mid.link"]'),
                   extra_packages={"mid": mid_manifest(tmp_path)})
    code, document = run_cli(config, capsys, "push")
    assert code == 0
    assert probe_row(document, "main:mid.link")["conditional"] is True
    assert probe_row(document)["conditional"] is True
    assert chain_steps(document) == [
        ("main:mid.link", "probe", "ok"), ("main:mid.link", "post_push", "ok"), *CHAIN_STEPS]
    assert log_lines(tmp_path) == ["midprobe", "midhook", "probe", "hook"]


def same_package_manifest(tmp_path, first_after, second_after):
    # Each target needs a hook: a row is conditional only when a hook waits for its probe.
    def target(name, after):
        return f'''[targets.{name}]
sync_policy = "push-only"
probe = "true"
probe_after = {after}
[targets.{name}.hooks]
post_push = "true"
'''
    return 'depends = ["base"]\n' + target("first", first_after) + target("second", second_after)


def test_probe_after_target_may_name_an_earlier_one_in_its_package(tmp_path, monkeypatch, capsys):
    app = same_package_manifest(tmp_path, '["base.conf"]', '["app.first"]')
    config = build(tmp_path, monkeypatch, app)
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert probe_row(preview, "main:app.first")["conditional"] is True
    assert probe_row(preview, "main:app.second")["conditional"] is True


def test_probe_after_target_naming_a_later_one_in_its_package_is_rejected(tmp_path, monkeypatch):
    app = same_package_manifest(tmp_path, '["app.second"]', '["base.conf"]')
    config = build(tmp_path, monkeypatch, app)
    engine = DotmanEngine.from_config_path(config)
    with pytest.raises(UsageError, match=r"\bafter\b"):
        engine.open_sync_session(engine.resolve_sync_scope(), preview=True)


# --- selector order does not change execution order --------------------------------

def test_exact_selectors_in_reverse_order_still_write_before_the_probe_reads(tmp_path, monkeypatch, capsys):
    config = build(tmp_path, monkeypatch, app_manifest(tmp_path))
    code, document = run_cli(config, capsys, "push", "main:app.check", "main:base.conf")
    assert code == 0
    assert log_lines(tmp_path) == ["probe", "hook"]
    assert step_kinds(document) == [("probe", "probe", "ok", None), ("hook", "post_push", "ok", None)]


def test_chained_probe_after_targets_run_in_declaration_order_whatever_the_selector_order(
        tmp_path, monkeypatch, capsys):
    made = tmp_path / "live/made"
    log = tmp_path / "log"
    app = f'''[targets.first]
sync_policy = "push-only"
probe = "true"
probe_after = ["app.nothing"]
[targets.first.hooks]
post_push = "mkdir -p {tmp_path / 'live'}; touch {made}"

[targets.second]
sync_policy = "push-only"
probe = "if [ -f {made} ]; then exit 0; else exit 100; fi"
probe_after = ["app.first"]
[targets.second.hooks]
post_push = "echo second >> {log}"

[targets.nothing]
source = "nothing"
path = "{tmp_path}/live/nothing"
type = "file"
sync_policy = "push-only"
'''
    config = build(tmp_path, monkeypatch, app, base_files=())
    (tmp_path / "repo/packages/app/nothing").write_bytes(b"x")
    code, _document = run_cli(config, capsys, "push", "main:app.second", "main:app.first", "main:app.nothing")
    assert code == 0
    assert log_lines(tmp_path) == ["second"]


def test_probe_after_entry_naming_the_target_itself_is_rejected_as_such(tmp_path, monkeypatch):
    app = '''[targets.first]
sync_policy = "push-only"
probe = "true"
probe_after = ["app.first"]
'''
    config = build(tmp_path, monkeypatch, app, base_files=())
    engine = DotmanEngine.from_config_path(config)
    with pytest.raises(UsageError, match="itself"):
        engine.open_sync_session(engine.resolve_sync_scope(), preview=True)


def test_unchained_probe_after_targets_probe_concurrently_at_planning(tmp_path, monkeypatch, capsys):
    # Each probe leaves a marker and waits for the other's; a serial planner times the first one out.
    markers = tmp_path / "markers"
    markers.mkdir()

    def target(name, other):
        wait = (f"touch {markers / name}; for i in $(seq 100); do "
                f"[ -f {markers / other} ] && exit 0; sleep 0.05; done; exit 100")
        return f'''[targets.{name}]
sync_policy = "push-only"
probe = "{wait}"
probe_after = ["base.conf"]
[targets.{name}.hooks]
post_push = "echo {name} >> {tmp_path / 'log'}"
'''
    app = 'depends = ["base"]\n' + target("one", "two") + target("two", "one")
    config = build(tmp_path, monkeypatch, app, base_files=IN_SYNC)
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert {probe_row(preview, f"main:app.{name}")["conditional"] for name in ("one", "two")} == {False}


def test_probe_after_target_does_not_wait_for_one_declared_after_it(tmp_path, monkeypatch, capsys):
    # `second` is conditional (drifted base.conf, a hook waits), but it runs after `first`,
    # so `first`'s whole-package entry must not see it. Both need hooks: without one,
    # no row is ever conditional and the guard could not fail.
    hook = f'[targets.{{name}}.hooks]\npost_push = "echo {{name}} >> {tmp_path / 'log'}"\n'
    app = '''depends = ["base"]
[targets.first]
sync_policy = "push-only"
probe = "true"
probe_after = ["app"]
''' + hook.format(name="first") + '''[targets.second]
sync_policy = "push-only"
probe = "true"
probe_after = ["base.conf"]
''' + hook.format(name="second")
    config = build(tmp_path, monkeypatch, app)
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert probe_row(preview, "main:app.second")["conditional"] is True
    assert probe_row(preview, "main:app.first")["conditional"] is False
