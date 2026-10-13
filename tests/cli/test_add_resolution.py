from __future__ import annotations

from pathlib import Path

import pytest

from dotman.add_resolution import AddDestination, AddResolver
from dotman.engine import DotmanEngine
from dotman.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    ScriptedInteraction,
    TextInputRequest,
)

from tests.helpers import write_named_manager_config


def _write_packages(repo_root: Path, *package_ids: str) -> None:
    for package_id in package_ids:
        package_root = repo_root / "packages" / Path(*package_id.split("/"))
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "package.toml").write_text(f'id = "{package_id}"\n', encoding="utf-8")


def _engine(tmp_path: Path, repos: dict[str, tuple[str, ...]]) -> DotmanEngine:
    repo_paths: dict[str, Path] = {}
    for repo_name, package_ids in repos.items():
        repo_root = tmp_path / repo_name
        _write_packages(repo_root, *package_ids)
        repo_paths[repo_name] = repo_root
    return DotmanEngine.from_config_path(write_named_manager_config(tmp_path, repo_paths))


def test_add_resolver_offers_create_first_then_collects_an_explicit_valid_destination(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {"alpha": ("zsh",), "beta": ("git",)})
    validation_errors: list[ValueError] = []
    interaction = ScriptedInteraction(
        choices=[None, "beta"],
        text_inputs=["bad.package", "work/tools"],
    )

    destination = AddResolver(
        engine,
        interaction=interaction,
        error_sink=validation_errors.append,
    ).resolve(None)

    assert destination == AddDestination(repo_name="beta", package_id="work/tools")
    assert [str(error) for error in validation_errors] == ["invalid package id 'bad.package'"]
    assert interaction.requests == [
        ChoiceRequest(
            header_text="Select a package for add:",
            options=(
                ChoiceOption(value=None, label="create a new package"),
                ChoiceOption(
                    value=AddDestination(repo_name="alpha", package_id="zsh"),
                    label="alpha:zsh",
                ),
                ChoiceOption(
                    value=AddDestination(repo_name="beta", package_id="git"),
                    label="beta:git",
                ),
            ),
        ),
        ChoiceRequest(
            header_text="Select a repo for the new package:",
            options=(
                ChoiceOption(value="alpha", label="alpha"),
                ChoiceOption(value="beta", label="beta"),
            ),
        ),
        TextInputRequest(prompt="Package ID: "),
        TextInputRequest(prompt="Package ID: "),
    ]


def test_add_resolver_fails_fast_on_invalid_created_package_id_without_error_sink(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, {"alpha": ("zsh",)})
    interaction = ScriptedInteraction(
        choices=[None, "alpha"],
        text_inputs=["bad.package", "work/tools"],
    )

    with pytest.raises(ValueError, match="invalid package id 'bad.package'"):
        AddResolver(engine, interaction=interaction).resolve(None)


def test_add_resolver_ranks_repo_and_package_fragments_and_keeps_create_first(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            "main": ("git-tools", "toolkit"),
            "domain": ("git",),
        },
    )
    selected = AddDestination(repo_name="main", package_id="git-tools")
    interaction = ScriptedInteraction(choices=[selected])

    assert AddResolver(engine, interaction=interaction).resolve("ma:git") == selected
    assert interaction.requests == [
        ChoiceRequest(
            header_text="Select a package for 'ma:git':",
            options=(
                ChoiceOption(value=None, label="create a new package"),
                ChoiceOption(value=selected, label="main:git-tools"),
                ChoiceOption(
                    value=AddDestination(repo_name="domain", package_id="git"),
                    label="domain:git",
                ),
            ),
        )
    ]


def test_add_resolver_preserves_noninteractive_exact_partial_and_create_rules(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {"alpha": ("git",), "beta": ("git-tools",)})
    resolver = AddResolver(engine)

    assert resolver.resolve("alpha:git") == AddDestination("alpha", "git")
    assert resolver.resolve("beta:git") == AddDestination("beta", "git")
    assert resolver.resolve("alpha:new/tools") == AddDestination("alpha", "new/tools")

    with pytest.raises(ValueError, match="package query is required in non-interactive mode"):
        resolver.resolve(None)
    with pytest.raises(ValueError, match="no exact match for 'tools'; use exact name 'beta:git-tools'"):
        resolver.resolve("tools")
    with pytest.raises(
        ValueError,
        match="use an explicit repo-qualified query to create one in non-interactive mode",
    ):
        resolver.resolve("missing")
    with pytest.raises(
        ValueError,
        match="cannot create non-interactively without an exact repo",
    ):
        resolver.resolve("al:new")


def test_add_resolver_owns_manifest_write_confirmation_policy(tmp_path: Path) -> None:
    engine = _engine(tmp_path, {"alpha": ("git",)})
    interaction = ScriptedInteraction(confirmations=[False])
    resolver = AddResolver(engine, interaction=interaction)

    assert resolver.confirm_manifest_write(repo_name="alpha", package_id="git") is False
    assert interaction.requests == [
        ConfirmationRequest(
            prompt="Write package config changes for alpha:git? [y/n] "
        )
    ]
    assert AddResolver(engine).confirm_manifest_write(
        repo_name="alpha",
        package_id="git",
    ) is False
    assert AddResolver(engine).confirm_manifest_write(
        repo_name="alpha",
        package_id="git",
        no_confirm=True,
    ) is True
