import os
from pathlib import Path
import pytest
from dotman.engine import DotmanEngine
from dotman.sync_base_store import FilePresent
from tests.engine.test_sync_session import make_engine
from tests.helpers import initialize_git_repository, resolved_child_metadata, write_single_repo_config, write_tracked_packages_state

def repo(tmp_path, target_lines, directory=False):
    root=tmp_path/"repo"; (root/"packages"/"app"/"files").mkdir(parents=True); (root/"profiles").mkdir()
    if directory:
        (root/"packages"/"app"/"files"/"x").mkdir()
        (root/"packages"/"app"/"files"/"x"/"a.md").write_text("hello")
    else:
        (root/"packages"/"app"/"files"/"x").write_text("hello")
    (root/"profiles"/"default.toml").write_text("")
    (root/"packages"/"app"/"package.toml").write_text("\n".join(["[targets.x]","source='files/x'","path='~/.x'","type='directory'" if directory else ""]+target_lines))
    return root

def engine(tmp_path, root, **options):
    return DotmanEngine.from_config_path(write_single_repo_config(tmp_path,repo_name="r",repo_path=root), **options)

def packages_repo(tmp_path, manifests):
    root = tmp_path/"repo"; (root/"profiles").mkdir(parents=True); (root/"profiles"/"default.toml").write_text("")
    for package_id, lines in manifests.items():
        (root/"packages"/package_id).mkdir(parents=True)
        (root/"packages"/package_id/"package.toml").write_text("\n".join(lines))
    return root

def tracked_engine(tmp_path, root, **options):
    write_tracked_packages_state(tmp_path/"state", repo_name="r", entries=[("app","default")])
    return engine(tmp_path, root, **options)

def push(engine_obj):
    with engine_obj.open_push_session(engine_obj.resolve_sync_scope()) as session:
        assert session.execute().result.status == "completed"

def test_flat_defaults_and_nested_rules(tmp_path):
    e=engine(tmp_path,repo(tmp_path,["[targets.x.path_rules.docs]","pattern='*.md'","priority=2"],directory=True))
    spec=e._tracked_state_context.repositories["r"].packages["app"].targets["x"]
    assert spec.render=="raw" and spec.capture=="raw"
    assert spec.compare_repo=="raw" and spec.compare_live=="capture"
    assert spec.editor.type=="default"
    assert spec.path_rules[0].name=="docs" and spec.path_rules[0].priority==2

def test_reject_removed_schema(tmp_path):
    root=repo(tmp_path,['reconcile="jinja"'])
    with pytest.raises(ValueError,match="unsupported keys: reconcile"):
        engine(tmp_path,root)

def test_command_objects_and_patch_validation(tmp_path):
    root=repo(tmp_path,['render={run="cat"}','capture="patch"','compare={repo="render",live="raw"}','editor={run="vim",io="pipe",elevation="root",additional_sources=["inc"]}'])
    e=engine(tmp_path,root); spec=e._tracked_state_context.repositories["r"].packages["app"].targets["x"]
    assert spec.render=="cat" and spec.capture=="patch"
    assert spec.editor.run=="vim" and spec.editor.io=="pipe"
    assert spec.compare_repo=="render" and spec.compare_live=="raw"

def test_flat_fields_inherit_and_override_by_name(tmp_path):
    from dotman.manifest import build_target_spec, merge_target_specs
    base = build_target_spec(target_name="x", manifest_path=tmp_path/"package.toml",
        target_payload={"source":"x","path":"~/.x","render":"jinja","compare":{"repo":"render","live":"raw"},
                        "editor":{"run":"vim","additional_sources":["a"]},
                        "path_rules":{"docs":{"pattern":"*.md","priority":4,"render":"jinja"}}})
    child = build_target_spec(target_name="x", manifest_path=tmp_path/"package.toml",
        target_payload={"source":"x","path":"~/.x","path_rules":{"docs":{"pattern":"*.md","capture":"patch"}}})
    merged=merge_target_specs(base,child)
    assert merged.render=="jinja" and merged.compare_repo=="render" and merged.editor.run=="vim"
    assert merged.path_rules[0].render=="jinja" and merged.path_rules[0].capture=="patch" and merged.path_rules[0].priority==4

def test_patch_capture_requires_flat_comparison_contract(tmp_path):
    root=repo(tmp_path,['render="jinja"','capture="patch"'])
    with pytest.raises(ValueError, match="compare.repo"):
        tracked_engine(tmp_path,root).resolve_sync_scope()


def test_matching_path_rules_compose_each_field_without_resetting_lower_priority(tmp_path):
    root = repo(tmp_path, [
        "[targets.x.path_rules.base]",
        'pattern="*.md"',
        'render="jinja"',
        'compare={repo="render",live="raw"}',
        "[targets.x.path_rules.high]",
        'pattern="a.md"',
        "priority=5",
        'capture="printf captured"',
    ], directory=True)
    e = tracked_engine(tmp_path, root)
    with e.open_push_session(e.resolve_sync_scope(), preview=True) as session:
        child, = session.view.observations
    resolved = resolved_child_metadata(e, child.identity.child_path)
    assert (resolved.render_command, resolved.capture_command) == ("jinja", "printf captured")
    assert (child.compare_repo, child.compare_live) == ("render", "raw")


def test_preset_comparison_sides_merge_independently(tmp_path):
    from dotman.manifest import build_target_spec
    spec = build_target_spec(
        target_name="x",
        manifest_path=tmp_path / "package.toml",
        target_payload={"source": "x", "path": "~/.x", "preset": "jinja-editor", "compare": {"live": "capture"}},
    )
    assert spec.compare_repo == "render"
    assert spec.compare_live == "capture"


def test_editor_additional_sources_inherit_independently(tmp_path):
    from dotman.manifest import build_target_spec, merge_target_specs
    base = build_target_spec(target_name="x", manifest_path=tmp_path/"package.toml",
        target_payload={"source":"x","path":"~/.x", "editor":{"run":"vim","additional_sources":["base"]}})
    child = build_target_spec(target_name="x", manifest_path=tmp_path/"package.toml",
        target_payload={"source":"x","path":"~/.x", "editor":{"run":"nvim"}})
    merged = merge_target_specs(base, child)
    assert merged.editor.run == "nvim"
    assert merged.additional_sources == ("base",)


def test_builtin_name_command_object_remains_command_in_plan_and_serialization(tmp_path):
    from dotman.manifest import build_target_spec
    spec = build_target_spec(
        target_name="x",
        manifest_path=tmp_path / "package.toml",
        target_payload={"source": "x", "path": "~/.x", "render": {"run": "jinja"}},
    )
    assert spec.render.startswith("__dotman_command__:")
    from dotman.models import TargetPlan
    plan = TargetPlan(
        package_id="app", target_name="x", repo_path=tmp_path/"x",
        live_path=tmp_path/"live", action="noop", target_kind="file",
        projection_kind="command", render=spec.render,
    )
    assert plan.to_dict()["render"] == {"run": "jinja"}



def test_path_rule_preset_compare_sides_merge_independently(tmp_path):
    from dotman.manifest import build_target_spec
    spec = build_target_spec(
        target_name="config",
        manifest_path=tmp_path / "package.toml",
        target_payload={
            "source": "files/config",
            "path": "~/.config",
            "path_rules": {
                "templates": {
                    "pattern": "*.tmpl",
                    "preset": "jinja-editor",
                    "compare": {"live": "capture"},
                }
            },
        },
    )
    rule = spec.path_rules[0]
    assert rule.compare_repo == "render"
    assert rule.compare_live == "capture"


def test_multi_parent_named_path_rule_inheritance_preserves_and_overrides_explicit_fields(tmp_path):
    from dotman.manifest import build_target_spec, merge_target_specs
    from dotman.models import PackageSpec

    def target(payload):
        return build_target_spec(
            target_name="config",
            manifest_path=tmp_path / "package.toml",
            target_payload={"source": "files/config", "path": "~/.config", "path_rules": {"docs": payload}},
        )

    parent_a = PackageSpec(
        id="a", package_root=tmp_path,
        targets={"config": target({"pattern": "*.md", "priority": 2, "render": "jinja"})},
    )
    parent_b = PackageSpec(
        id="b", package_root=tmp_path,
        targets={"config": target({"capture": "patch"})},
    )
    merged = merge_target_specs(parent_a.targets["config"], parent_b.targets["config"])
    rule = merged.path_rules[0]
    assert rule.pattern == "*.md"
    assert rule.priority == 2
    assert rule.render == "jinja"
    assert rule.capture == "patch"

    explicit = target({"pattern": "*.txt", "priority": 9})
    merged_explicit = merge_target_specs(merged, explicit)
    assert merged_explicit.path_rules[0].pattern == "*.txt"
    assert merged_explicit.path_rules[0].priority == 9


@pytest.mark.parametrize("directory", [False, True])
def test_push_forced_builtin_render_executes_as_command(tmp_path, monkeypatch, directory):
    root, live, _marker = forced_builtin_repo(tmp_path, monkeypatch, directory)
    push(tracked_engine(tmp_path, root))
    # The built-in Jinja provider would publish the raw template "hello".
    assert live.read_bytes() == b"rendered"


@pytest.mark.parametrize("policy_lines", [
    ['sync_policy="push-only-delete"'],
    ["[targets.x.path_rules.cleanup]", 'pattern="*"', 'sync_policy="push-only-delete"'],
])
def test_push_only_delete_removes_live_children_without_repository_changes(tmp_path, policy_lines):
    root = repo(tmp_path, policy_lines, directory=True)
    source = root / "packages/app/files/x"
    (source / "new.md").write_text("new repo file")
    live = Path.home() / ".x"
    live.mkdir()
    (live / "a.md").write_text("live version")
    (live / "stale.md").write_text("stale")
    push(tracked_engine(tmp_path, root))
    assert sorted(path.name for path in live.iterdir()) == []
    assert sorted(path.name for path in source.iterdir()) == ["a.md", "new.md"]


def test_resolve_package_merges_partial_named_path_rule_across_actual_parent_manifests(tmp_path):
    root = tmp_path / "repo"
    for package_id in ("parent-a", "parent-b", "child"):
        (root / "packages" / package_id).mkdir(parents=True)
    (root / "profiles").mkdir()
    (root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (root / "packages" / "parent-a" / "package.toml").write_text("""

[targets.config.path_rules.docs]
pattern = "*.md"
""", encoding="utf-8")
    (root / "packages" / "parent-b" / "package.toml").write_text("""

[targets.config.path_rules.docs]
priority = 7
render = "jinja"
""", encoding="utf-8")
    (root / "packages" / "child" / "package.toml").write_text("""
extends = ["parent-a", "parent-b"]

[targets.config]
source = "files/config"
path = "~/.config/app"
type = "directory"
""", encoding="utf-8")

    engine_obj = engine(tmp_path, root)
    resolved = engine_obj.repos["r"].resolve_package("child")
    rule = resolved.targets["config"].path_rules[0]

    assert rule.pattern == "*.md"
    assert rule.priority == 7
    assert rule.render == "jinja"


def forced_builtin_repo(tmp_path, monkeypatch, directory):
    """Shadow built-in provider names with PATH commands that log their invocation."""
    import os
    import shlex

    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    marker = tmp_path / "commands"
    for name, output in [("jinja", "rendered"), ("render", "repo-view"),
                         ("capture", "live-view"), ("patch", "captured")]:
        command = bin_root / name
        command.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {name} >> {shlex.quote(str(marker))}\nprintf {output}\n"
        )
        command.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_root}:{os.environ['PATH']}")
    lines = [
        'render={run="jinja"}', 'capture={run="patch"}',
        'compare={repo={run="render"},live={run="capture"}}',
    ]
    root = repo(tmp_path, (
        ['[targets.x.path_rules.docs]', 'pattern="*.md"', *lines] if directory else lines
    ), directory=directory)
    live = Path.home() / ".x"
    if directory:
        live.mkdir(parents=True)
        live = live / "a.md"
    live.write_bytes(b"live")
    initialize_git_repository(root)
    return root, live, marker


@pytest.mark.parametrize("directory", [False, True])
def test_pull_forced_builtin_commands_keep_command_identity(tmp_path, monkeypatch, directory):
    from dotman.sync_base_store import DirectoryChildPresent, FilePresent
    from tests.helpers import open_tracked_pull_session

    root, live, marker = forced_builtin_repo(tmp_path, monkeypatch, directory)
    with open_tracked_pull_session(engine(tmp_path, root), tmp_path, repo_name="r",
                                   entries=[("app", "default")]) as session:
        row = session.view.rows[0]
        assert row.approved
        expected = DirectoryChildPresent(b"captured", False) if directory else FilePresent(b"captured")
        assert row.proposal.repository == expected
        # Capture materializes first; the optional checkpoint check then uses
        # the forced command Render, not the built-in Jinja provider.
        assert marker.read_text().splitlines() == ["render", "capture", "patch", "jinja"]
        assert not row.proposal.checkpoint_qualified
    assert live.read_bytes() == b"live"


@pytest.mark.parametrize("rule_lines", [[], ["[targets.x.path_rules.docs]", 'pattern="*.md"']])
@pytest.mark.parametrize("side,builtin", [("live", "render"), ("repo", "capture")])
def test_compare_side_rejects_other_sides_builtin_at_load(tmp_path, rule_lines, side, builtin):
    # Render projects the repository and Capture projects live; accepting the
    # other side's builtin used to compare repository bytes with themselves.
    root = repo(tmp_path, [*rule_lines, f'compare.{side}="{builtin}"'], directory=bool(rule_lines))
    with pytest.raises(ValueError, match=rf'(?s)compare\.{side}.*{builtin}.*run'):
        engine(tmp_path, root)


@pytest.mark.parametrize("compare,side", [
    ('{repo="raw", live={run="render"}}', "comparison_live"),
    ('{repo={run="capture"}, live="raw"}', "comparison_repository"),
])
def test_command_named_after_other_sides_builtin_runs_as_command(tmp_path, monkeypatch, compare, side):
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    for name in ("render", "capture"):
        (bin_root / name).write_text("#!/bin/sh\nprintf command-view\n")
        (bin_root / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_root}:{os.environ['PATH']}")
    sync_engine = make_engine(tmp_path, monkeypatch, [("unit", "both", b"same", b"drifted", f"compare = {compare}")])
    with sync_engine.open_sync_session(sync_engine.resolve_sync_scope(), preview=True) as session:
        observation = session.view.observations[0]
        assert getattr(observation, side) == FilePresent(b"command-view")
        assert observation.state != "directly-in-sync"


@pytest.mark.parametrize("manifests", [
    pytest.param({
        "base": ["[targets.x]", "source='files/x'", "path='~/.x'", "preset='jinja-patch'"],
        "app": ["extends=['base']", "[targets.x]", "render='raw'"],
    }, id="child-render-raw-over-inherited-patch-capture"),
])
def test_patch_capture_validates_effective_settings(tmp_path, manifests):
    # Extends merges are resolved per tracked package, so resolving the scope is
    # the earliest point the effective settings exist; Pull must not be reached.
    with pytest.raises(ValueError, match='capture = "patch" requires'):
        tracked_engine(tmp_path, packages_repo(tmp_path, manifests)).resolve_sync_scope()


def test_patch_capture_settings_may_combine_across_extends(tmp_path):
    root = packages_repo(tmp_path, {
        "base": ["[targets.x]", "source='files/x'", "path='~/.x'", "render='jinja'",
                 "compare={repo='render',live='raw'}"],
        "app": ["extends=['base']", "[targets.x]", "capture='patch'"],
    })

    scope = tracked_engine(tmp_path, root).resolve_sync_scope()

    assert scope.targets


@pytest.mark.parametrize("rules", [
    pytest.param(["[targets.x.path_rules.docs]", "pattern='*.md'", "capture='patch'"],
                 id="path-rule-patch-capture-without-render"),
    pytest.param(["preset='jinja-patch'", "[targets.x.path_rules.docs]", "pattern='*.md'", "render='raw'"],
                 id="path-rule-raw-render-under-patch-capture-target"),
])
def test_patch_capture_validates_effective_child_settings(tmp_path, rules):
    from dotman.sync_session import SessionOpenFailed

    # Overlapping Path Rules fold per child, so a matching child is the first
    # point where the effective settings exist.
    engine_obj = tracked_engine(tmp_path, repo(tmp_path, rules, directory=True))
    opened = engine_obj.open_pull_session(engine_obj.resolve_sync_scope())

    assert isinstance(opened, SessionOpenFailed)
    assert 'r:app.x/a.md" capture = "patch" requires' in opened.diagnostic.message


def test_patch_capture_child_settings_may_come_from_overlapping_rules(tmp_path):
    from dotman.sync_session import SessionOpenFailed

    rules = ["[targets.x.path_rules.templates]", "pattern='*'", "render='jinja'",
             "compare={repo='render',live='raw'}",
             "[targets.x.path_rules.docs]", "pattern='*.md'", "priority=1", "capture='patch'"]
    engine_obj = tracked_engine(tmp_path, repo(tmp_path, rules, directory=True))
    opened = engine_obj.open_pull_session(engine_obj.resolve_sync_scope())

    assert not isinstance(opened, SessionOpenFailed), opened
    with opened:
        pass


def test_push_ignores_patch_capture_of_children_it_never_plans(tmp_path):
    from dotman.sync_session import SessionOpenFailed

    # A pull-only child takes no part in a Push, so its Patch Capture rules cannot block one.
    rules = ["[targets.x.path_rules.docs]", "pattern='*.md'", "capture='patch'", "sync_policy='pull-only'"]
    engine_obj = tracked_engine(tmp_path, repo(tmp_path, rules, directory=True))
    opened = engine_obj.open_push_session(engine_obj.resolve_sync_scope())

    assert not isinstance(opened, SessionOpenFailed), opened
    with opened:
        pass
