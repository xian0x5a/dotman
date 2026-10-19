from __future__ import annotations

from pathlib import Path

import pytest

from dotman.engine import DotmanEngine
from dotman.projection import directory_child_policy
from tests.helpers import write_single_repo_config

BASE_MANIFEST = """
[targets.t]
source = "files/d"
path = "~/d"
type = "directory"
editor = { run = "true", additional_sources = ["x"] }

[targets.t.path_rules.plain]
pattern = "*.plain"

[targets.t.path_rules.own_editor]
pattern = "*.own"
editor = { run = "myed" }

[targets.t.path_rules.own_sources]
pattern = "*.src"
editor = { run = "myed", additional_sources = ["s"] }
"""


def child_sources(tmp_path: Path, child_manifest: str, child_file: str) -> tuple[str, ...]:
    """Additional Sources that planning gives a directory child of the resolved `child` package."""
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    for package_id, manifest in {"base": BASE_MANIFEST, "child": 'extends = ["base"]\n' + child_manifest}.items():
        (repo_root / "packages" / package_id).mkdir(parents=True)
        (repo_root / "packages" / package_id / "package.toml").write_text(manifest, encoding="utf-8")
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))
    target = engine.get_repo("fixture").resolve_package("child").targets["t"]
    *_, editor, sources, _ = directory_child_policy(
        child_file, target.path_rules, default_render=None, default_capture=None, default_editor=target.editor
    )
    assert editor.additional_sources == sources
    return sources


@pytest.mark.parametrize(
    ("child_manifest", "child_file", "expected"),
    [
        # A rule that declares no editor can still extend or reset the sources it inherits.
        ('[append.targets.t.path_rules.plain.editor]\nadditional_sources = ["y"]\n', "f.plain", ("x", "y")),
        ('[append.targets.t.path_rules.plain.editor]\nadditional_sources = ["y"]\n', "f.other", ("x",)),
        ('remove = ["targets.t.path_rules.plain.editor.additional_sources"]\n', "f.plain", ("x",)),
        ('[targets.t.path_rules.plain]\neditor = { run = "true", additional_sources = [] }\n', "f.plain", ()),
        # Appending to a rule that declared its own sources extends them; removing them returns to the target's.
        ('[append.targets.t.path_rules.own_sources.editor]\nadditional_sources = ["y"]\n', "f.src", ("s", "y")),
        ('remove = ["targets.t.path_rules.own_sources.editor.additional_sources"]\n', "f.src", ("x",)),
        # A rule's editor that declares no sources follows the target's resolved sources.
        ('[targets.t]\neditor = { run = "true", additional_sources = ["y"] }\n', "f.own", ("y",)),
        ('remove = ["targets.t.editor"]\n', "f.own", ()),
        ('[append.targets.t.editor]\nadditional_sources = ["z"]\n', "f.own", ("x", "z")),
        # ...while sources the rule declared stay its own.
        ('[targets.t]\neditor = { run = "true", additional_sources = ["y"] }\n', "f.src", ("s",)),
    ],
)
def test_path_rule_additional_sources(tmp_path: Path, child_manifest: str, child_file: str, expected: tuple[str, ...]) -> None:
    assert child_sources(tmp_path, child_manifest, child_file) == expected


def test_sources_only_rule_keeps_the_editor_selected_by_a_lower_priority_rule(tmp_path: Path) -> None:
    # `own_editor` (priority 0) selects `myed` for *.own; a sources-only rule on top must not undo that.
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    manifests = {
        "base": BASE_MANIFEST + '\n[targets.t.path_rules.top]\npattern = "*.own"\npriority = 5\n',
        "child": 'extends = ["base"]\n[append.targets.t.path_rules.top.editor]\nadditional_sources = ["y"]\n',
    }
    for package_id, manifest in manifests.items():
        (repo_root / "packages" / package_id).mkdir(parents=True)
        (repo_root / "packages" / package_id / "package.toml").write_text(manifest, encoding="utf-8")
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))
    target = engine.get_repo("fixture").resolve_package("child").targets["t"]
    *_, editor, sources, _ = directory_child_policy(
        "f.own", target.path_rules, default_render=None, default_capture=None, default_editor=target.editor
    )
    assert (editor.run, sources) == ("myed", ("x", "y"))
