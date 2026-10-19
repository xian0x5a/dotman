from __future__ import annotations

import stat
from pathlib import Path

import pytest

from dotman.models import FullSpecSelector
from dotman.engine import DotmanEngine
from tests.helpers import (
    EXAMPLE_REPO,
    write_manager_config,
    write_multi_instance_repo,
    write_package_override_preview_repo,
    write_shared_stack_repo,
    write_single_repo_config,
    write_single_repo_config_with_state_key,
    untrack_entry,
)


def test_group_selected_package_is_marked_explicit_in_tracked_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "core-cli-meta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    package_detail = engine.describe_tracked_package("example:core-cli-meta")

    assert [package_entry.package_entry.selector for package_entry in package_detail.package_entries] == ["core-cli-meta"]
    assert [package_entry.tracked_reason for package_entry in package_detail.package_entries] == ["explicit"]

def test_info_tracked_drops_hooks_for_non_effective_provenance_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "core-cli-meta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    package_detail = engine.describe_tracked_package("example:git")

    assert [package_entry.package_entry.selector for package_entry in package_detail.package_entries] == ["core-cli-meta", "git"]
    assert package_detail.package_entries[0].hooks == {}
    assert set(package_detail.package_entries[1].hooks) == {"guard_push", "pre_push", "post_push"}

def test_preview_package_selection_implicit_overrides_returns_unique_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "override-preview-repo"
    write_package_override_preview_repo(repo_root)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    engine = DotmanEngine.from_config_path(config_path)

    engine.record_tracked_package_entry(FullSpecSelector(repo="fixture", selector="beta-meta", selector_kind="package", profile="basic"))

    overrides = engine.preview_tracked_package_entry_implicit_overrides(
        FullSpecSelector(
            repo="fixture",
            selector="alpha",
            selector_kind="package",
            profile="basic",
        )
    )

    assert len(overrides) == 1
    override = overrides[0]
    assert override.winner.selection_label == "fixture:alpha@basic"
    assert override.winner.package_id == "alpha"
    assert [contender.selection_label for contender in override.overridden] == ["fixture:beta@basic"]
    assert [contender.package_id for contender in override.overridden] == ["beta"]


def test_record_binding_writes_resolved_binding_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    engine = DotmanEngine.from_config_path(config_path)
    state_path = tmp_path / "state" / "dotman" / "repos" / "example" / "tracked-packages.toml"

    engine.record_tracked_package_entry(FullSpecSelector(repo="example", selector="git", selector_kind="package", profile="basic"))

    assert state_path.exists()
    assert state_path.read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            'package_id = "git"',
            'profile = "basic"',
            "",
        ]
    )


def test_record_binding_flattens_group_into_package_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    engine = DotmanEngine.from_config_path(config_path)
    _repo, binding = engine.resolve_full_spec_selector_text("example:os/arch@basic")
    state_path = tmp_path / "state" / "dotman" / "repos" / "example" / "tracked-packages.toml"

    assert binding.selector_kind == "group"

    engine.record_tracked_package_entry(binding)

    assert state_path.read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            'package_id = "core-cli-meta"',
            'profile = "basic"',
            "",
        ]
    )

def test_record_binding_replaces_existing_selector_binding_with_new_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    engine = DotmanEngine.from_config_path(config_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "tracked-packages.toml"
    state_path.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "nvim"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    engine.record_tracked_package_entry(FullSpecSelector(repo="example", selector="git", selector_kind="package", profile="work"))

    bindings = engine.read_tracked_package_entries(engine.get_repo("example"))
    assert [(binding.selector, binding.profile) for binding in bindings] == [
        ("git", "work"),
        ("nvim", "basic"),
    ]
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert not (state_dir / "tracked-packages.toml").with_suffix(".tmp").exists()

def test_record_binding_keeps_distinct_profiles_for_multi_instance_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_multi_instance_repo(repo_root)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    engine = DotmanEngine.from_config_path(config_path)

    engine.record_tracked_package_entry(FullSpecSelector(repo="fixture", selector="profiled", selector_kind="package", profile="basic"))
    engine.record_tracked_package_entry(FullSpecSelector(repo="fixture", selector="profiled", selector_kind="package", profile="work"))

    bindings = engine.read_tracked_package_entries(engine.get_repo("fixture"))
    assert [(binding.selector, binding.profile) for binding in bindings] == [
        ("profiled", "basic"),
        ("profiled", "work"),
    ]

    packages = engine.list_tracked_packages()
    assert [(package.package_ref, package.bound_profile) for package in packages] == [
        ("profiled<basic>", "basic"),
        ("profiled<work>", "work"),
    ]

    package_detail = engine.describe_tracked_package("fixture:profiled<work>")
    assert package_detail.package_ref == "profiled<work>"
    assert package_detail.bound_profile == "work"
    assert [package_entry.package_entry.profile for package_entry in package_detail.package_entries] == ["work"]


def test_engine_drops_installed_alias_helpers() -> None:
    assert not hasattr(DotmanEngine, "list_installed_packages")
    assert not hasattr(DotmanEngine, "describe_installed_package")
    assert not hasattr(DotmanEngine, "find_installed_package_matches")
    assert not hasattr(DotmanEngine, "_iter_installed_bindings")


def test_models_use_tracked_package_type_names() -> None:
    import dotman.models as models

    assert hasattr(models, "TrackedPackageSummary")
    assert hasattr(models, "TrackedPackageDetail")
    assert hasattr(models, "TrackedPackageEntrySummary")
    assert hasattr(models, "TrackedTargetSummary")

    assert not hasattr(models, "InstalledPackageSummary")
    assert not hasattr(models, "InstalledPackageDetail")
    assert not hasattr(models, "InstalledFullSpecSelectorSummary")
    assert not hasattr(models, "InstalledTargetSummary")

def test_remove_binding_rejects_single_partial_match_in_non_interactive_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    original_state = "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "example"',
            'package_id = "git"',
            'profile = "basic"',
            "",
        ]
    )
    (state_dir / "tracked-packages.toml").write_text(original_state, encoding="utf-8")

    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(ValueError, match="no exact match for 'gi'; use exact name 'example:git@basic'"):
        untrack_entry(engine, "example:gi")

    assert (state_dir / "tracked-packages.toml").read_text(encoding="utf-8") == original_state


def test_remove_binding_treats_implicit_package_match_as_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "fixture-repo"
    write_shared_stack_repo(repo_root)
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    original_state = "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "fixture"',
            'package_id = "shared-stack"',
            'profile = "basic"',
            "",
        ]
    )
    (state_dir / "tracked-packages.toml").write_text(original_state, encoding="utf-8")

    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(ValueError, match="ambiguous"):
        untrack_entry(engine, "shared")

    assert (state_dir / "tracked-packages.toml").read_text(encoding="utf-8") == original_state


def test_remove_binding_deletes_only_the_selected_tracked_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "core-cli-meta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    removed = untrack_entry(engine, "example:git@basic")

    assert removed.repo == "example"
    assert removed.selector == "git"
    assert removed.profile == "basic"
    assert engine.read_tracked_package_entries(engine.get_repo("example")) == [
        removed.__class__(repo="example", selector="core-cli-meta", selector_kind="package", profile="basic")
    ]

def test_remove_binding_allows_selector_only_when_tracked_binding_is_unique(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    removed = untrack_entry(engine, "example:git")

    assert removed == removed.__class__(repo="example", selector="git", selector_kind="package", profile="basic")
    assert engine.read_tracked_package_entries(engine.get_repo("example")) == []

def test_remove_binding_can_remove_invalid_configured_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "old-meta"',
                'profile = "basic"',
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    removed = untrack_entry(engine, "example:old-meta@basic")

    assert removed == removed.__class__(repo="example", selector="old-meta", selector_kind="package", profile="basic")
    assert engine.read_tracked_package_entries(engine.get_repo("example")) == [
        removed.__class__(repo="example", selector="git", selector_kind="package", profile="basic")
    ]


def test_remove_binding_can_remove_orphan_binding_from_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_home = tmp_path / "xdg-state"
    state_home.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    config_path = write_single_repo_config_with_state_key(tmp_path, repo_name="example", repo_path=EXAMPLE_REPO)
    orphan_state_dir = state_home / "dotman" / "repos" / "removed"
    orphan_state_dir.mkdir(parents=True, exist_ok=True)
    orphan_state_path = orphan_state_dir / "tracked-packages.toml"
    orphan_state_path.write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "removed-repo"',
                'package_id = "linux"',
                'profile = "basic"',
                "",
                "[[packages]]",
                'repo = "removed-repo"',
                'package_id = "mac"',
                'profile = "work"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    removed = untrack_entry(engine, "removed-repo:linux@basic")

    assert removed == removed.__class__(repo="removed-repo", selector="linux", selector_kind="package", profile="basic")
    assert orphan_state_path.read_text(encoding="utf-8") == "\n".join(
        [
            "schema_version = 1",
            "",
            "[[packages]]",
            'repo = "removed-repo"',
            'package_id = "mac"',
            'profile = "work"',
            "",
        ]
    )


def test_remove_binding_reports_tracked_owner_when_selector_is_only_a_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "example"',
                'package_id = "core-cli-meta"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(
        ValueError,
        match=r"cannot untrack 'example:nvim': required by tracked package entries: example:core-cli-meta@basic",
    ):
        untrack_entry(engine, "nvim@basic")


def test_tracked_state_requires_schema_version_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)
    state_dir = tmp_path / "state" / "dotman" / "repos" / "example"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "[[packages]]",
                'repo = "example"',
                'package_id = "git"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    engine = DotmanEngine.from_config_path(config_path)

    with pytest.raises(ValueError, match=r"tracked packages file '.*/tracked-packages\.toml' must declare schema_version = 1"):
        engine.list_tracked_packages()
