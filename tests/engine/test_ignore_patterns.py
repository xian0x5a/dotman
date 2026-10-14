from __future__ import annotations

from pathlib import Path

import pytest

from dotman.engine import DotmanEngine
from dotman.ignore import IgnoreMatcher
from tests.engine.test_sync_directory_observation import put
from tests.helpers import initialize_git_repository, write_single_repo_config, write_tracked_packages_state


def _push_tracked_scope(engine: DotmanEngine) -> None:
    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert session.execute().result.status == "completed"


def test_gitignore_style_recursive_directory_patterns_ignore_nested_pycache_directories() -> None:
    matcher = IgnoreMatcher.from_patterns(("**/__pycache__/",))

    assert matcher.matches_directory("nested/__pycache__")
    assert not matcher.matches("visible.conf")


def test_gitignore_style_root_anchored_patterns_only_match_from_target_root() -> None:
    matcher = IgnoreMatcher.from_patterns(("/foo",))

    assert matcher.matches("foo")
    assert not matcher.matches("nested/foo")


def test_basename_only_ignore_patterns_still_match_nested_files() -> None:
    assert IgnoreMatcher.from_patterns(("bookmarks",)).matches("foo/bookmarks")
    assert IgnoreMatcher.from_patterns(("settings.ini",)).matches("gtk-3.0/settings.ini")


def test_negated_ignore_patterns_can_reinclude_specific_files() -> None:
    matcher = IgnoreMatcher.from_patterns(("*.pyc", "!keep.pyc"))

    assert not matcher.matches("keep.pyc")
    assert matcher.matches("drop.pyc")


def test_negated_directory_patterns_do_not_reinclude_still_ignored_descendant_files() -> None:
    matcher = IgnoreMatcher.from_patterns(("**/*.dotdropbak", "!plugins/pinned-window/"))

    assert not matcher.matches("plugins/pinned-window/BarWidget.qml")
    assert matcher.matches("plugins/pinned-window/BarWidget.qml.dotdropbak")


def write_sample_package(
    tmp_path: Path, *, package_extra: str = "", target_extra: str = "", repo_toml: str | None = None
) -> tuple[Path, Path]:
    """Write fixture:sample with directory target `config`; return its source and live roots."""
    repo_root = tmp_path / "repo"
    source_root = repo_root / "packages" / "sample" / "files" / "config"
    source_root.mkdir(parents=True)
    (repo_root / "profiles").mkdir()
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    if repo_toml is not None:
        (repo_root / "repo.toml").write_text(repo_toml, encoding="utf-8")
    (repo_root / "packages" / "sample" / "package.toml").write_text(
        f'{package_extra}\n'
        f'[targets.config]\nsource = "files/config"\npath = "~/.config/sample"\n{target_extra}\n',
        encoding="utf-8",
    )
    return source_root, Path.home() / ".config" / "sample"


def tracked_sample_engine(tmp_path: Path, *, dir_symlink_mode: str | None = None) -> DotmanEngine:
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("sample", "default")])
    return DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=tmp_path / "repo"),
        dir_symlink_mode=dir_symlink_mode,
    )


def observed_push_children(engine: DotmanEngine) -> set[str]:
    with engine.open_push_session(engine.resolve_sync_scope(), preview=True) as session:
        return {unit.identity.child_path for unit in session.view.observations}


def live_files(live_root: Path) -> list[str]:
    return sorted(str(path.relative_to(live_root)) for path in live_root.rglob("*") if path.is_file())


def test_push_skips_ignored_repository_children_and_preserves_ignored_live_children(tmp_path: Path) -> None:
    source, live = write_sample_package(
        tmp_path,
        target_extra='[targets.config.ignore]\npatterns = ["**/__pycache__/"]',
        repo_toml='[ignore]\nskip_markers = [".dotman-skip"]\n',
    )
    for name in ("visible.conf", "nested/__pycache__/cached.pyc"):
        put(source, name)
    for name in ("stale.conf", "nested/__pycache__/live.pyc", "cache/.dotman-skip", "cache/local-state"):
        put(live, name)

    _push_tracked_scope(tracked_sample_engine(tmp_path))

    # Push-only cleanup deletes the unignored live-only child but keeps ignored and skip-marked subtrees.
    assert live_files(live) == ["cache/.dotman-skip", "cache/local-state", "nested/__pycache__/live.pyc", "visible.conf"]


@pytest.mark.parametrize(
    ("dir_symlink_mode", "target_extra", "observed", "external_kept"),
    [
        (None, "", {"visible.conf": "directly-in-sync", "linked": "observation-failed"}, True),
        (None, '[targets.config.ignore]\npatterns = ["linked/"]', {"visible.conf": "directly-in-sync"}, True),
        ("follow", "", {"visible.conf": "directly-in-sync", "linked/extra.conf": "drifted"}, False),
    ],
)
def test_push_live_directory_symlink_is_rejected_ignored_or_followed(
    tmp_path: Path, dir_symlink_mode, target_extra, observed, external_kept
) -> None:
    source, live = write_sample_package(tmp_path, target_extra=target_extra)
    put(source, "visible.conf")
    put(live, "visible.conf")
    external = put(tmp_path / "external", "extra.conf")
    (live / "linked").symlink_to(external.parent, target_is_directory=True)
    engine = tracked_sample_engine(tmp_path, dir_symlink_mode=dir_symlink_mode)

    with engine.open_push_session(engine.resolve_sync_scope()) as session:
        assert {unit.identity.child_path: unit.state for unit in session.view.observations} == observed
        session.execute()

    assert external.exists() is external_kept


def test_push_following_live_directory_symlink_loop_fails_locally(tmp_path: Path) -> None:
    source, live = write_sample_package(tmp_path)
    put(source, "visible.conf")
    put(live, "visible.conf")
    (live / "loop").symlink_to(live, target_is_directory=True)
    engine = tracked_sample_engine(tmp_path, dir_symlink_mode="follow")

    with engine.open_push_session(engine.resolve_sync_scope(), preview=True) as session:
        units = {unit.identity.child_path: unit for unit in session.view.observations}

    assert units["visible.conf"].state == "directly-in-sync"
    assert units["loop"].diagnostics[0].code == "directory-symlink-loop"


def test_repo_toml_loads_skip_markers_from_ignore_table(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages").mkdir()
    (repo_root / "repo.toml").write_text(
        "\n".join(["[ignore]", 'skip_markers = [".dotman-skip"]', ""]),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    assert engine.get_repo("fixture").ignore_defaults.skip_markers == (".dotman-skip",)


def test_repo_toml_rejects_prune_marker_path_names(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "repo.toml").write_text(
        "\n".join(["[ignore]", 'skip_markers = ["nested/.dotman-skip"]', ""]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="skip_markers entries must be basenames"):
        DotmanEngine.from_config_path(
            write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
        )


def test_repo_toml_rejects_empty_prune_marker_names(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "repo.toml").write_text(
        "\n".join(["[ignore]", 'skip_markers = [""]', ""]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="skip_markers entries must not be empty"):
        DotmanEngine.from_config_path(
            write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
        )


def test_repo_toml_loads_gitignore_from_ignore_table(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages").mkdir()
    (repo_root / "repo.toml").write_text(
        "\n".join(["[ignore]", 'gitignore = true', ""]),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    assert engine.get_repo("fixture").ignore_defaults.gitignore is True


def test_repo_toml_gitignore_defaults_to_disabled(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages").mkdir()
    (repo_root / "repo.toml").write_text(
        "\n".join(["[ignore]", 'skip_markers = [".dotman-skip"]', ""]),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )

    assert engine.get_repo("fixture").ignore_defaults.gitignore is False


def test_repo_toml_rejects_invalid_gitignore_values(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "repo.toml").write_text(
        "\n".join(["[ignore]", 'gitignore = 1', ""]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="gitignore must be a boolean"):
        DotmanEngine.from_config_path(
            write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
        )


def test_push_applies_repository_gitignore_to_both_trees_with_explicit_negation_override(tmp_path: Path) -> None:
    source, live = write_sample_package(
        tmp_path,
        target_extra='[targets.config.ignore]\npatterns = ["!important.log"]',
        repo_toml="[ignore]\ngitignore = true\n",
    )
    # Negating a control file must not turn it into a payload.
    put(source, ".gitignore", b"*.log\n*.local\n!.gitignore\n!nested/.gitignore\n")
    put(source, "nested/.gitignore", b"*.tmp\n")
    for name in ("visible.conf", "important.log", "trash.log", "nested/drop.tmp"):
        put(source, name)
    for name in ("machine.local", "stale.conf"):
        put(live, name)

    _push_tracked_scope(tracked_sample_engine(tmp_path))

    assert live_files(live) == ["important.log", "machine.local", "visible.conf"]


@pytest.mark.parametrize(
    ("package_ignore", "excluded"),
    [('patterns = ["*.secret"]', "machine.secret"), ("gitignore = true", "machine.local")],
)
def test_package_ignore_table_is_applied_during_push(tmp_path: Path, package_ignore: str, excluded: str) -> None:
    source, live = write_sample_package(tmp_path, package_extra=f"[ignore]\n{package_ignore}\n")
    put(source, ".gitignore", b"*.local\n")
    for name in ("visible.conf", excluded):
        put(source, name)

    _push_tracked_scope(tracked_sample_engine(tmp_path))

    assert live_files(live) == ["visible.conf"]


def test_repository_ignore_patterns_are_direction_independent(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages").mkdir()
    (repo_root / "repo.toml").write_text(
        "[ignore]\npatterns = [\"*.cache\"]\n",
        encoding="utf-8",
    )
    engine = DotmanEngine.from_config_path(
        write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    )
    defaults = engine.get_repo("fixture").ignore_defaults
    assert defaults.patterns == ("*.cache",)
    assert not hasattr(defaults, "push")
    assert not hasattr(defaults, "pull")


def test_ignore_patterns_compose_repo_package_target_in_order_and_keep_empty_package_layer(tmp_path: Path) -> None:
    source, _live = write_sample_package(
        tmp_path,
        package_extra="[ignore]\npatterns = []\n",
        target_extra='[targets.config.ignore]\npatterns = ["!keep.log"]',
        repo_toml='[ignore]\npatterns = ["*.tmp"]\ngitignore = true\n',
    )
    put(source, ".gitignore", b"*.log\n")
    for name in ("keep.tmp", "drop.tmp", "keep.log", "drop.log", "visible.conf"):
        put(source, name)

    assert observed_push_children(tracked_sample_engine(tmp_path)) == {"keep.log", "visible.conf"}


def test_ignore_pattern_order_preserves_later_repeated_exclusion_after_negation(tmp_path: Path) -> None:
    source, _live = write_sample_package(
        tmp_path,
        package_extra='[ignore]\npatterns = ["!keep.tmp"]\n',
        target_extra='[targets.config.ignore]\npatterns = ["*.tmp"]',
        repo_toml='[ignore]\npatterns = ["*.tmp"]\n',
    )
    for name in ("keep.tmp", "drop.tmp", "visible.conf"):
        put(source, name)

    assert observed_push_children(tracked_sample_engine(tmp_path)) == {"visible.conf"}


def test_pull_unified_controls_preserve_excluded_repository_children(tmp_path, monkeypatch):
    from tests.engine.test_sync_directory_observation import directory_engine

    engine = directory_engine(
        tmp_path, monkeypatch,
        extra='[targets.tree.ignore]\npatterns = ["excluded/", "*.secret"]',
    )
    repo, live = tmp_path / "repo/packages/app/tree", tmp_path / "live/tree"
    put(repo, ".gitignore", b"*.ignored\n")
    for name in ("excluded/a", "private.secret", "a.ignored", "repo-marked/a", "live-marked/a"):
        put(repo, name, b"preserve")
    put(repo, "repo-marked/.dotman-skip")
    put(live, "live-marked/.dotman-skip")
    put(live, "a.ignored", b"must not capture")
    put(repo, "managed", b"old")
    put(live, "managed", b"new")
    with engine.open_pull_session(engine.resolve_sync_scope()) as session:
        assert [unit.identity.child_path for unit in session.view.observations] == ["managed"]
        assert session.execute().result.status == "completed"
    assert (repo / "managed").read_bytes() == b"new"
    for name in ("excluded/a", "private.secret", "a.ignored", "repo-marked/a", "live-marked/a"):
        assert (repo / name).read_bytes() == b"preserve"
    assert (repo / ".gitignore").read_bytes() == b"*.ignored\n"

def open_session(engine: DotmanEngine, operation: str):
    opener = {"push": engine.open_push_session, "pull": engine.open_pull_session, "sync": engine.open_sync_session}
    return opener[operation](engine.resolve_sync_scope(), preview=True)


@pytest.mark.parametrize("operation", ["push", "pull", "sync"])
@pytest.mark.parametrize("enabled", [True, False])
def test_repository_gitignore_chain_is_symmetric(tmp_path, monkeypatch, operation, enabled):
    home = tmp_path / "home"
    live = home / "config"
    live.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "repo"
    source = root / "packages/sample/files/config"
    source.mkdir(parents=True)
    (root / "profiles").mkdir()
    (root / "profiles/default.toml").write_text("")
    (root / "repo.toml").write_text("[ignore]\ngitignore = true\n")
    (root / "packages/sample/package.toml").write_text(
        '[ignore]\ngitignore = ' + str(enabled).lower() +
        '\n[targets.config]\nsource = "files/config"\npath = "~/config"\n')
    (root / ".gitignore").write_text('/packages/*/files/config/root-*\n*.log\n')
    (root / "packages/.gitignore").write_text('sample/files/config/ancestor-*\n')
    (source / ".gitignore").write_text('!keep.log\n/target-*\nblocked/\n')
    for base in (source, live):
        (base / "nested").mkdir()
        (base / "blocked").mkdir()
        for name in ("visible", "root-only", "ancestor-only", "target-only", "drop.log", "keep.log", "nested/drop.tmp", "nested/keep.tmp", "blocked/keep"):
            (base / name).write_text(operation + str(base))
    (source / "nested/.gitignore").write_text('*.tmp\n!keep.tmp\n')
    (source / "blocked/.gitignore").write_text('!keep\n')
    (live / "live-only.log").write_text("live")
    (live / ".gitignore").write_text("visible\n")
    initialize_git_repository(root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("sample", "default")])
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=root))
    with open_session(engine, operation) as session:
        paths = {row.observation.identity.child_path for row in session.view.rows}
    if enabled:
        assert paths == {"visible", "keep.log", "nested/keep.tmp"}
    else:
        assert {"root-only", "ancestor-only", "target-only", "drop.log", "live-only.log", "nested/drop.tmp", "blocked/keep"} <= paths

def test_scoped_gitignore_preserves_git_pattern_syntax_and_parent_exclusion(tmp_path):
    from dotman.ignore import IgnoreMatcher, collect_gitignore_chain

    source = tmp_path / "packages/app/config"
    source.mkdir(parents=True)
    (tmp_path / ".gitignore").write_text(
        "/packages/[ab]pp/config/root-*\n"
        "packages/**/config/double-*\n"
        "/packages/app/config/blocked/\n"
        "*.log\n")
    (source / ".gitignore").write_text(
        "!keep.log\n/local-*\n\\#literal\n\\!literal\nspace\\ \n")
    (source / "blocked").mkdir()
    (source / "blocked/.gitignore").write_text("!keep\n")
    (source / "nested").mkdir()
    (source / "nested/.gitignore").write_text("/only-here\n")
    matcher = IgnoreMatcher.from_patterns((), gitignore=collect_gitignore_chain(source, tmp_path))
    for path in ("root-file", "double-file", "drop.log", "#literal", "!literal", "space ", "blocked/keep", "nested/only-here"):
        assert matcher.matches(path), path
    for path in ("keep.log", "nested/local-file", "nested/deeper/only-here", "only-here", "space"):
        assert not matcher.matches(path), path

@pytest.mark.parametrize("operation", ["push", "pull", "sync"])
@pytest.mark.parametrize("control_scope", ["repository", "target"])
def test_gitignore_exclusion_allows_separately_owned_nested_target(tmp_path, monkeypatch, operation, control_scope):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "repo"
    package = root / "packages/app"
    (package / "tree").mkdir(parents=True)
    (package / "tree/keep").write_text("keep")
    (package / "owned").write_text("owned")
    if control_scope == "repository":
        (root / ".gitignore").write_text("/packages/*/tree/owned\n")
    else:
        (package / "tree/.gitignore").write_text("/owned\n")
    (root / "profiles").mkdir()
    (root / "profiles/default.toml").write_text("")
    (root / "repo.toml").write_text("[ignore]\ngitignore = true\n")
    (package / "package.toml").write_text(
        '[targets.tree]\nsource = "tree"\npath = "~/tree"\n'
        '[targets.owned]\nsource = "owned"\npath = "~/tree/owned"\n')
    initialize_git_repository(root)
    write_tracked_packages_state(tmp_path / "state", repo_name="fixture", entries=[("app", "default")])
    engine = DotmanEngine.from_config_path(write_single_repo_config(tmp_path, repo_name="fixture", repo_path=root))
    with open_session(engine, operation) as session:
        assert {unit.identity.target_name for unit in session.view.observations} == {"tree", "owned"}
