from __future__ import annotations

from pathlib import Path

import pytest

from dotman.engine import DotmanEngine
from dotman.interaction import ChoiceRequest, ScriptedInteraction
from dotman.untrack_resolution import (
    UntrackEntryRequest,
    UntrackGroupRequest,
    UntrackResolver,
)

from tests.helpers import (
    EXAMPLE_REPO,
    write_multi_instance_repo,
    write_named_manager_config,
    write_tracked_packages_state,
)


def test_untrack_resolver_returns_persisted_orphan_entry(tmp_path: Path) -> None:
    config_path = write_named_manager_config(tmp_path, {"example": EXAMPLE_REPO})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="removed",
        entries=[("linux", "orphan")],
    )

    result = UntrackResolver(DotmanEngine.from_config_path(config_path)).resolve(
        "removed:linux@orphan"
    )

    assert isinstance(result, UntrackEntryRequest)
    assert result.binding.repo == "removed"
    assert result.binding.selector == "linux"
    assert result.binding.profile == "orphan"


def test_untrack_resolver_reports_implicit_package_owner(tmp_path: Path) -> None:
    config_path = write_named_manager_config(tmp_path, {"example": EXAMPLE_REPO})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="example",
        entries=[("core-cli-meta", "basic")],
    )

    with pytest.raises(
        ValueError,
        match=(
            "cannot untrack 'example:nvim': required by tracked package entries: "
            "example:core-cli-meta@basic"
        ),
    ):
        UntrackResolver(DotmanEngine.from_config_path(config_path)).resolve("nvim@basic")


def test_untrack_resolver_resolves_group_members_across_singleton_profiles(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    (repo_root / "profiles" / "work.toml").write_text("", encoding="utf-8")
    for package_id in ("alpha", "beta"):
        package_root = repo_root / "packages" / package_id
        package_root.mkdir(parents=True)
        (package_root / "package.toml").write_text(
            '', encoding="utf-8"
        )
    (repo_root / "groups").mkdir()
    (repo_root / "groups" / "bundle.toml").write_text(
        'members = ["alpha", "beta"]\n', encoding="utf-8"
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("alpha", "basic"), ("beta", "work")],
    )

    result = UntrackResolver(DotmanEngine.from_config_path(config_path)).resolve(
        "fixture:bundle"
    )

    assert isinstance(result, UntrackGroupRequest)
    assert result.label == "fixture:bundle"
    assert [(binding.selector, binding.profile) for binding in result.removal_bindings] == [
        ("alpha", "basic"),
        ("beta", "work"),
    ]


def test_untrack_resolver_prefers_interactive_persisted_partial_over_exact_group(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    for package_id in ("alpha", "bundle-tools"):
        package_root = repo_root / "packages" / package_id
        package_root.mkdir(parents=True)
        (package_root / "package.toml").write_text(
            '', encoding="utf-8"
        )
    (repo_root / "groups").mkdir()
    (repo_root / "groups" / "bundle.toml").write_text(
        'members = ["alpha"]\n', encoding="utf-8"
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("alpha", "basic"), ("bundle-tools", "basic")],
    )
    interaction = ScriptedInteraction(
        choices=["entry:fixture:fixture:bundle-tools@basic"]
    )

    result = UntrackResolver(
        DotmanEngine.from_config_path(config_path),
        interaction=interaction,
    ).resolve("fixture:bundle")

    assert isinstance(result, UntrackEntryRequest)
    assert result.binding.selector == "bundle-tools"
    request = interaction.requests[0]
    assert isinstance(request, ChoiceRequest)
    assert request.header_text == "Select a tracked package entry for 'bundle':"


def test_untrack_resolver_chooses_package_instance_for_group(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    write_multi_instance_repo(repo_root)
    (repo_root / "groups").mkdir()
    (repo_root / "groups" / "bundle.toml").write_text(
        'members = ["profiled"]\n', encoding="utf-8"
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("profiled", "basic"), ("profiled", "work")],
    )
    interaction = ScriptedInteraction(choices=["work"])

    result = UntrackResolver(
        DotmanEngine.from_config_path(config_path),
        interaction=interaction,
    ).resolve("fixture:bundle")

    assert isinstance(result, UntrackGroupRequest)
    assert result.profile == "work"
    assert [binding.profile for binding in result.removal_bindings] == ["work"]
    request = interaction.requests[0]
    assert isinstance(request, ChoiceRequest)
    assert request.header_text == "Select a tracked profile for fixture:bundle:"


def test_untrack_resolver_describes_remaining_tracking_after_removal(tmp_path: Path) -> None:
    config_path = write_named_manager_config(tmp_path, {"example": EXAMPLE_REPO})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="example",
        entries=[("git", "basic"), ("core-cli-meta", "basic")],
    )
    engine = DotmanEngine.from_config_path(config_path)
    resolver = UntrackResolver(engine)
    request = resolver.resolve("example:git@basic")
    assert isinstance(request, UntrackEntryRequest)
    engine.remove_tracked_package_entry("example:git@basic")

    remaining = resolver.remaining_tracked_package(request.binding)

    assert remaining is not None
    assert remaining.package_id == "git"
    assert [entry.package_entry.selector for entry in remaining.package_entries] == [
        "core-cli-meta"
    ]
