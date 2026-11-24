"""`additional_triggers`: a probe target that is also active when listed inputs have work in the run."""

import json
import re

import pytest

from dotman.cli import main
from dotman.engine import DotmanEngine
from dotman.errors import UsageError
from tests.helpers import write_sync_repository, write_tracked_packages_state

# Base writes `live/conf` on push; IN_SYNC leaves it with no drift.
BASE_FILE = ("conf", "push-only", b"new", None, "")
IN_SYNC = [("conf", "push-only", b"same", b"same", "")]


def build(tmp_path, monkeypatch, app_toml, *, base_files=(BASE_FILE,), base_extra="", base_toml=None,
          extra_packages=None, tracked=None):
    """A tracked `app` that depends on the untracked `base`; `app_toml` replaces app's manifest."""
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
    if tracked:
        write_tracked_packages_state(tmp_path / "state", repo_name="main", entries=tracked)
    return config


def consumer(tmp_path, *, probe_exit=100, triggers='["base.conf"]', hook=None, depends='["base"]',
             package_hooks="", name="check"):
    """A probe target that is active on its probe alone (exit 0) or through its triggers."""
    hook = hook if hook is not None else f"echo hook >> {tmp_path / 'log'}"
    trigger_line = f"additional_triggers = {triggers}" if triggers is not None else ""
    return f'''depends = {depends}
{package_hooks}
[targets.{name}]
sync_policy = "push-only"
probe = "echo probe >> {tmp_path / 'probe.log'}; exit {probe_exit}"
{trigger_line}
[targets.{name}.hooks]
post_push = "{hook}"
'''


def run_cli(config, capsys, *args):
    code = main(["--config", str(config), "--json", "--no-confirm", *args])
    return code, json.loads(capsys.readouterr().out)


def log_lines(tmp_path, name="log"):
    log = tmp_path / name
    return log.read_text().splitlines() if log.exists() else []


def probe_row(document, identity="main:app.check"):
    return next((work for work in document["probe_work"] if work["identity"] == identity), None)


# --- activation ----------------------------------------------------------------------

def test_trigger_drift_activates_the_target_although_its_probe_exits_100(tmp_path, monkeypatch, capsys):
    config = build(tmp_path, monkeypatch, consumer(tmp_path))
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert probe_row(preview)["triggered_by"] == ["main:base.conf"]
    assert log_lines(tmp_path, "probe.log") == ["probe"]
    code, _document = run_cli(config, capsys, "push")
    assert code == 0
    assert log_lines(tmp_path) == ["hook"]
    assert log_lines(tmp_path, "probe.log") == ["probe", "probe"]  # once per run: nothing re-runs at execution


@pytest.mark.parametrize("probe_exit, active", [(100, False), (0, True)])
def test_without_trigger_work_the_probe_alone_decides(tmp_path, monkeypatch, capsys, probe_exit, active):
    config = build(tmp_path, monkeypatch, consumer(tmp_path, probe_exit=probe_exit), base_files=IN_SYNC)
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    row = probe_row(preview)
    assert (row is not None) is active
    if active:
        assert row["triggered_by"] == []
    run_cli(config, capsys, "push")
    assert log_lines(tmp_path) == (["hook"] if active else [])


def test_whole_package_entry_matches_work_on_any_of_its_targets(tmp_path, monkeypatch, capsys):
    base_files = [*IN_SYNC, ("other", "push-only", b"new", None, "")]
    config = build(tmp_path, monkeypatch, consumer(tmp_path, triggers='["base"]'), base_files=base_files)
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert probe_row(preview)["triggered_by"] == ["main:base"]


def test_trigger_activates_the_target_only_when_its_entry_has_work(tmp_path, monkeypatch, capsys):
    # Drift in an unlisted target of the same package does not count for a target entry.
    base_files = [*IN_SYNC, ("other", "push-only", b"new", None, "")]
    config = build(tmp_path, monkeypatch, consumer(tmp_path), base_files=base_files)
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert probe_row(preview) is None


# --- ordering and scope --------------------------------------------------------------

def test_reversed_exact_selectors_still_run_the_hook_after_the_write(tmp_path, monkeypatch, capsys):
    hook = f'echo \\"$(cat {tmp_path / "live/conf"})\\" >> {tmp_path / "log"}'
    config = build(tmp_path, monkeypatch, consumer(tmp_path, hook=hook))
    code, _document = run_cli(config, capsys, "push", "main:app.check", "main:base.conf")
    assert code == 0
    assert log_lines(tmp_path) == ["new"]


def test_consumer_out_of_scope_does_not_run_when_only_its_trigger_is_selected(tmp_path, monkeypatch, capsys):
    config = build(tmp_path, monkeypatch, consumer(tmp_path))
    code, document = run_cli(config, capsys, "push", "main:base.conf")
    assert code == 0
    assert probe_row(document) is None
    assert (tmp_path / "live/conf").read_bytes() == b"new"
    assert log_lines(tmp_path) == []


# --- kinds of work -------------------------------------------------------------------

def test_triggered_targets_chain_across_packages(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log"
    leaf = f'''depends = ["app"]
[targets.x]
sync_policy = "push-only"
probe = "exit 100"
additional_triggers = ["app.check"]
[targets.x.hooks]
post_push = "echo leaf >> {log}"
'''
    config = build(tmp_path, monkeypatch, consumer(tmp_path, hook=f"echo app >> {log}"),
                   extra_packages={"leaf": leaf}, tracked=[("leaf", "default")])
    code, document = run_cli(config, capsys, "push")
    assert code == 0
    assert probe_row(document, "main:leaf.x")["triggered_by"] == ["main:app.check"]
    assert log_lines(tmp_path) == ["app", "leaf"]


def test_active_probe_work_of_an_input_target_counts(tmp_path, monkeypatch, capsys):
    base = '[targets.tool]\nsync_policy = "push-only"\nprobe = "exit 0"\n'
    config = build(tmp_path, monkeypatch, consumer(tmp_path, triggers='["base.tool"]'),
                   base_files=IN_SYNC, base_toml=base)
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert probe_row(preview)["triggered_by"] == ["main:base.tool"]


def test_noop_hook_work_of_an_input_package_counts(tmp_path, monkeypatch, capsys):
    base_extra = f'[hooks]\npost_push = {{run = "echo basehook >> {tmp_path / "log"}", run_noop = true}}'
    config = build(tmp_path, monkeypatch, consumer(tmp_path, triggers='["base"]'),
                   base_files=IN_SYNC, base_extra=base_extra)
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert probe_row(preview)["triggered_by"] == ["main:base"]
    run_cli(config, capsys, "push")
    assert sorted(log_lines(tmp_path)) == ["basehook", "hook"]


def test_package_hooks_of_a_triggered_target_activate_like_any_active_probe_target(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log"
    app = consumer(tmp_path, hook=f"echo target >> {log}", package_hooks=f'[hooks]\npost_push = "echo package >> {log}"')
    config = build(tmp_path, monkeypatch, app)
    code, _document = run_cli(config, capsys, "push")
    assert code == 0
    assert log_lines(tmp_path) == ["target", "package"]


# --- validation ----------------------------------------------------------------------

FILE_TARGET = '[targets.f]\nsource = "f"\npath = "/tmp/never-used"\ntype = "file"\nadditional_triggers = ["base"]\n'
CYCLIC_BASE = '''depends = ["app"]
[targets.conf]
source = "conf"
path = "%(root)s/live/conf"
type = "file"
sync_policy = "push-only"
[targets.off]
disabled = true
source = "conf"
path = "%(root)s/live/off"
type = "file"
'''


def assert_rejected_as_additional_triggers(config, reason):
    """Loading, resolving or planning rejects it for `reason`, not as an unsupported key."""
    with pytest.raises(UsageError, match="additional_triggers") as caught:
        engine = DotmanEngine.from_config_path(config)
        engine.open_sync_session(engine.resolve_sync_scope(), preview=True)
    assert "unsupported keys" not in str(caught.value)
    assert re.search(reason, str(caught.value)), str(caught.value)


SHAPE = "must be 'package' or 'package.target'"
DIRECT = "depends on directly"


@pytest.mark.parametrize("app_toml, kwargs, reason", [
    # shape
    (lambda p: consumer(p, triggers="[]"), {}, "at least one entry"),
    (lambda p: consumer(p, triggers="[1]"), {}, "must be a string"),
    (lambda p: consumer(p, triggers='["a.b.c"]'), {}, SHAPE),
    (lambda p: consumer(p, triggers='[".x"]'), {}, SHAPE),
    (lambda p: 'depends = ["base"]\n' + FILE_TARGET, {}, "must define probe"),
    (lambda p: 'depends = ["base"]\n[targets.t]\nadditional_triggers = ["base"]\n', {}, "must define probe"),
    # graph
    (lambda p: consumer(p, triggers='["app"]'), {}, DIRECT),
    (lambda p: consumer(p, triggers='["unknown"]'), {}, DIRECT),
    (lambda p: consumer(p, triggers='["base"]', depends='["mid"]'), {"extra_packages": {"mid": 'depends = ["base"]\n'}}, DIRECT),
    (lambda p: consumer(p, triggers='["base.conf"]'), {"cyclic": True}, "depends\\s+back"),
    (lambda p: consumer(p, triggers='["base.missing"]'), {"cyclic_free_targets": True}, "names no target"),
    (lambda p: consumer(p, triggers='["base.off"]'), {"cyclic_free_targets": True}, "names no target"),
], ids=["empty", "non-string", "three-parts", "empty-package", "file-target", "no-probe",
        "own-package", "unknown-package", "transitive-only", "cycle", "missing-target", "disabled-target"])
def test_invalid_additional_triggers_are_usage_errors(tmp_path, monkeypatch, app_toml, kwargs, reason):
    kwargs = dict(kwargs)
    base_toml = None
    if kwargs.pop("cyclic", False):
        base_toml = CYCLIC_BASE % {"root": tmp_path}
    if kwargs.pop("cyclic_free_targets", False):
        base_toml = (CYCLIC_BASE % {"root": tmp_path}).replace('depends = ["app"]\n', "")
    config = build(tmp_path, monkeypatch, app_toml(tmp_path), base_toml=base_toml, **kwargs)
    assert_rejected_as_additional_triggers(config, reason)


@pytest.mark.parametrize("triggers", ['["base"]', '["base.conf"]', '["base", "base.conf"]'])
def test_valid_additional_triggers_plan(tmp_path, monkeypatch, triggers):
    config = build(tmp_path, monkeypatch, consumer(tmp_path, triggers=triggers))
    engine = DotmanEngine.from_config_path(config)
    session = engine.open_sync_session(engine.resolve_sync_scope(), preview=True)
    assert "main:app.check" in [row.row_id for row in session.view.rows]


# --- extends -------------------------------------------------------------------------

PARENT = '''[targets.check]
sync_policy = "push-only"
probe = "exit 100"
additional_triggers = ["base.conf"]
[targets.check.hooks]
post_push = "echo hook >> %(log)s"
'''


def test_inherited_triggers_are_validated_against_the_childs_depends(tmp_path, monkeypatch, capsys):
    parent = PARENT % {"log": tmp_path / "log"}
    config = build(tmp_path, monkeypatch, 'extends = ["parent"]\ndepends = ["base"]\n',
                   extra_packages={"parent": parent})
    code, document = run_cli(config, capsys, "push")
    assert code == 0
    assert probe_row(document)["triggered_by"] == ["main:base.conf"]
    assert log_lines(tmp_path) == ["hook"]


def test_inherited_triggers_the_child_cannot_satisfy_are_rejected(tmp_path, monkeypatch):
    parent = PARENT % {"log": tmp_path / "log"}
    config = build(tmp_path, monkeypatch, 'extends = ["parent"]\n', extra_packages={"parent": parent})
    assert_rejected_as_additional_triggers(config, "depends on directly")


@pytest.mark.parametrize("triggers", ['["base"]', '["base.conf"]'])
def test_entries_may_name_a_member_of_a_group_in_depends(tmp_path, monkeypatch, capsys, triggers):
    config = build(tmp_path, monkeypatch, consumer(tmp_path, triggers=triggers, depends='["bundle"]'))
    (tmp_path / "repo/groups").mkdir()
    (tmp_path / "repo/groups/bundle.toml").write_text('members = ["base"]\n')
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert probe_row(preview)["triggered_by"] == [f"main:{triggers[2:-2]}"]


def test_child_appends_triggers_to_the_inherited_ones(tmp_path, monkeypatch, capsys):
    other = f'''[targets.file]
source = "file"
path = "{tmp_path}/live/other-file"
type = "file"
sync_policy = "push-only"
'''
    parent = PARENT.replace('additional_triggers = ["base.conf"]', 'additional_triggers = ["base"]') % {
        "log": tmp_path / "log"}
    child = 'extends = ["parent"]\n[append]\ndepends = ["base", "other"]\n[append.targets.check]\nadditional_triggers = ["other"]\n'
    config = build(tmp_path, monkeypatch, child, extra_packages={"parent": parent, "other": other})
    (tmp_path / "repo/packages/other/file").write_bytes(b"x")
    _code, preview = run_cli(config, capsys, "push", "--dry-run")
    assert sorted(probe_row(preview)["triggered_by"]) == ["main:base", "main:other"]
