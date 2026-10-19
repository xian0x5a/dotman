from __future__ import annotations

from pathlib import Path

import pytest

from dotman.engine import DotmanEngine
from tests.helpers import write_single_repo_config

VALID_TARGET = '[targets.a]\nsource = "files/a"\npath = "~/a"\n'
PACKAGE = "packages/p/package.toml"


def load_repo(tmp_path: Path, files: dict[str, str]) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    for relative_path, text in {PACKAGE: VALID_TARGET, **files}.items():
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root))
    repo = engine.get_repo("fixture")
    for package_id in repo.packages:
        repo.resolve_package(package_id)


# Every error names the file it came from and the field that is wrong.
@pytest.mark.parametrize(
    ("relative_path", "text", "expected"),
    [
        (PACKAGE, '[targets.a]\nsource = 42\npath = "~/a"\n', r"package\.toml target 'a' source must be a string"),
        (PACKAGE, '[targets.a]\nsource = "files/a"\npath = ["~/a"]\n', r"package\.toml target 'a' path must be a string"),
        (PACKAGE, "[targets]\na = 5\n", r"package\.toml target 'a' must be a table"),
        (PACKAGE, '[targets]\na = "x"\n', r"package\.toml target 'a' must be a table"),
        (PACKAGE, "targets = 5\n", r"package\.toml targets must be a table"),
        (PACKAGE, "hooks = 5\n" + VALID_TARGET, r"package\.toml hooks must be a table"),
        (PACKAGE, "vars = 5\n" + VALID_TARGET, r"package\.toml vars must be a table"),
        (PACKAGE, "append = 5\n" + VALID_TARGET, r"package\.toml append must be a table"),
        (PACKAGE, "description = 5\n" + VALID_TARGET, r"package\.toml description must be a string"),
        (PACKAGE, "depends = [1]\n" + VALID_TARGET, r"package\.toml depends\[0\] must be a string, got int"),
        (PACKAGE, "depends = 1\n" + VALID_TARGET, r"package\.toml depends must be a string or list of strings, got int"),
        (PACKAGE, 'sync_policy = "bogus"\n' + VALID_TARGET, r"package\.toml has unsupported sync_policy 'bogus'"),
        (PACKAGE, VALID_TARGET + 'render = ""\n', r"package\.toml target 'a' render must not be empty"),
        (PACKAGE, VALID_TARGET + 'type = "socket"\n', r"package\.toml target 'a' has unsupported type 'socket'"),
        (
            PACKAGE,
            VALID_TARGET + '[targets.a.path_rules.r]\npattern = "*"\nsync_policy = "bogus"\n',
            r"package\.toml target 'a' path_rules\.r has unsupported sync_policy 'bogus'",
        ),
        (
            PACKAGE,
            VALID_TARGET + '[targets.a.path_rules.r]\npattern = "*"\nrender = 5\n',
            r"package\.toml target 'a' path_rules\.r render must be a string",
        ),
        (PACKAGE, VALID_TARGET + '[targets.a.path_rules.r]\nrender = "jinja"\n', r'target "fixture:p\.a" path rule \'r\' must define pattern'),
        ("groups/g.toml", 'member = ["p"]\n', r"g\.toml has unsupported keys: member"),
        ("groups/g.toml", "members = [1]\n", r"g\.toml members\[0\] must be a string"),
        ("groups/g.toml", 'members = ["p"]\ndescription = 5\n', r"g\.toml description must be a string"),
        ("groups/dotted.name.toml", 'members = ["p"]\n', r"dotted\.name\.toml: invalid group id 'dotted\.name'"),
        ("profiles/work.toml", 'include = ["default"]\n', r"work\.toml has unsupported keys: include"),
        ("profiles/work.toml", "[var]\nx = 1\n", r"work\.toml has unsupported keys: var"),
        ("profiles/work.toml", "vars = 5\n", r"work\.toml vars must be a table"),
        ("profiles/work.toml", "includes = [1]\n", r"work\.toml includes\[0\] must be a string"),
        ("profiles/we<ird>.toml", "", r"we<ird>\.toml: invalid profile id 'we<ird>'"),
    ],
)
def test_manifest_errors_name_file_and_field(tmp_path: Path, relative_path: str, text: str, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        load_repo(tmp_path, {relative_path: text})


def test_profile_ids_may_contain_dots(tmp_path: Path) -> None:
    load_repo(tmp_path, {"profiles/work.v2.toml": '[vars]\nname = "work"\n', "groups/g.toml": 'members = ["p"]\ndescription = "all"\n'})
