from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from dotman.engine import DotmanEngine
from dotman.models import PackageSpec
from tests.helpers import write_single_repo_config

BASE_MANIFEST = """
[ignore]
patterns = ["base-package"]

[targets.t]
source = "files/a"
path = "~/a"
type = "file"
render = "jinja"
compare = { live = "raw" }

[targets.t.ignore]
patterns = ["base"]
command = "echo ignored"

[targets.t.path_rules.a]
pattern = "*.a"

[targets.t.path_rules.b]
pattern = "*.b"
priority = 1
render = "raw"
"""


def resolve_child(tmp_path: Path, child_manifest: str) -> PackageSpec:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    for package_id, manifest in {"base": BASE_MANIFEST, "child": 'extends = ["base"]\n' + child_manifest}.items():
        (repo_root / "packages" / package_id).mkdir(parents=True)
        (repo_root / "packages" / package_id / "package.toml").write_text(manifest, encoding="utf-8")
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))
    return engine.get_repo("fixture").resolve_package("child")


def rule(package: PackageSpec, name: str) -> Any:
    return next(rule for rule in package.targets["t"].path_rules if rule.name == name)


# Each removal leaves the field as if no manifest in the chain had set it.
@pytest.mark.parametrize(
    ("remove_path", "observe", "expected"),
    [
        ("targets.t.type", lambda p: p.targets["t"].target_type, None),
        ("targets.t.render", lambda p: p.targets["t"].render, "raw"),
        ("targets.t.compare.live", lambda p: p.targets["t"].compare_live, "capture"),
        ("targets.t.compare", lambda p: p.targets["t"].compare_live, "capture"),
        ("targets.t.ignore.patterns", lambda p: (p.targets["t"].ignore_patterns, p.targets["t"].ignore_command), (None, "echo ignored")),
        ("targets.t.ignore", lambda p: (p.targets["t"].ignore_patterns, p.targets["t"].ignore_command), (None, None)),
        ("targets.t.path_rules.a", lambda p: [r.name for r in p.targets["t"].path_rules], ["b"]),
        # A path rule without its own render inherits the target's render.
        ("targets.t.path_rules.b.render", lambda p: rule(p, "b").render, "jinja"),
        ("ignore.patterns", lambda p: p.ignore_patterns, None),
    ],
)
def test_remove_accepts_manifest_keys(
    tmp_path: Path, remove_path: str, observe: Callable[[PackageSpec], Any], expected: Any
) -> None:
    package = resolve_child(tmp_path, f'remove = ["{remove_path}"]\n')

    assert observe(package) == expected


@pytest.mark.parametrize(
    ("append_manifest", "observe", "expected"),
    [
        ('[append.targets.t.ignore]\npatterns = ["child"]\n', lambda p: p.targets["t"].ignore_patterns, ("base", "child")),
        ('[append.ignore]\npatterns = ["child-package"]\n', lambda p: p.ignore_patterns, ("base-package", "child-package")),
    ],
)
def test_append_accepts_manifest_keys(
    tmp_path: Path, append_manifest: str, observe: Callable[[PackageSpec], Any], expected: Any
) -> None:
    package = resolve_child(tmp_path, append_manifest)

    assert observe(package) == expected


@pytest.mark.parametrize(
    "child_manifest",
    [
        'remove = ["targets.t.target_type"]\n',
        'remove = ["targets.t.declared_in"]\n',
        'remove = ["ignore_patterns"]\n',
        '[append.targets.t]\nignore_patterns = ["child"]\n',
    ],
)
def test_inheritance_paths_reject_internal_field_names(tmp_path: Path, child_manifest: str) -> None:
    with pytest.raises(ValueError, match=r"does not name a manifest field"):
        resolve_child(tmp_path, child_manifest)


def test_append_rejects_keyed_path_rules(tmp_path: Path) -> None:
    # Path rules are a keyed table; a child adds one by declaring it, not by appending.
    with pytest.raises(ValueError, match=r"append target 'targets\.t\.path_rules' is not a list"):
        resolve_child(tmp_path, '[append.targets.t]\npath_rules = [{ pattern = "*.c" }]\n')
