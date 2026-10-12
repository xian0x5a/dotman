from __future__ import annotations

from pathlib import Path

from dotman.engine import DotmanEngine
from dotman.interaction import ChoiceRequest, ConfirmationRequest, ScriptedInteraction
from dotman.track_resolution import TrackResolver

from tests.helpers import (
    EXAMPLE_REPO,
    REFERENCE_REPO,
    write_named_manager_config,
    write_profile_switch_repo,
    write_tracked_packages_state,
)


def test_track_resolver_chooses_between_exact_repos_and_resolves_profile(tmp_path: Path) -> None:
    config_path = write_named_manager_config(
        tmp_path,
        {"alpha": REFERENCE_REPO, "beta": REFERENCE_REPO},
    )
    interaction = ScriptedInteraction(choices=["beta:sunshine"])

    result = TrackResolver(
        DotmanEngine.from_config_path(config_path),
        interaction=interaction,
    ).resolve("sunshine@host/linux")

    assert result.disposition == "ready"
    assert result.binding.repo == "beta"
    assert result.binding.selector == "sunshine"
    assert result.binding.profile == "host/linux"
    assert isinstance(interaction.requests[0], ChoiceRequest)
    assert interaction.requests[0].header_text == "Select a repo for exact selector 'sunshine':"
    assert [option.value for option in interaction.requests[0].options] == [
        "alpha:sunshine",
        "beta:sunshine",
    ]


def test_track_resolver_returns_kept_when_profile_replacement_is_declined(
    tmp_path: Path,
) -> None:
    config_path = write_named_manager_config(tmp_path, {"example": EXAMPLE_REPO})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="example",
        entries=[("git", "basic")],
    )
    interaction = ScriptedInteraction(confirmations=[False])

    result = TrackResolver(
        DotmanEngine.from_config_path(config_path),
        interaction=interaction,
    ).resolve("example:git@work")

    assert result.disposition == "kept"
    assert result.binding.profile == "basic"
    assert interaction.requests == [
        ConfirmationRequest(
            prompt="Confirm replacement? [y/n] ",
            message=(
                "\n"
                "Confirm tracked package entry replacement for example:git:\n"
                "  existing: example:git@basic\n"
                "  new:      example:git@work\n"
            ),
        )
    ]


def test_track_resolver_no_confirm_emits_replacement_summary_without_interaction(
    tmp_path: Path,
) -> None:
    config_path = write_named_manager_config(tmp_path, {"example": EXAMPLE_REPO})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="example",
        entries=[("git", "basic")],
    )
    messages: list[str] = []

    result = TrackResolver(
        DotmanEngine.from_config_path(config_path),
        message_sink=messages.append,
    ).resolve("example:git@work", no_confirm=True)

    assert result.disposition == "ready"
    assert result.binding.profile == "work"
    assert messages == [
        "\nConfirm tracked package entry replacement for example:git:\n"
        "  existing: example:git@basic\n"
        "  new:      example:git@work\n"
    ]


def test_track_resolver_switches_to_selected_non_conflicting_profile(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    write_profile_switch_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="fixture",
        entries=[("beta", "basic")],
    )
    interaction = ScriptedInteraction(choices=["basic", "work"])

    result = TrackResolver(
        DotmanEngine.from_config_path(config_path),
        interaction=interaction,
    ).resolve("fixture:alpha")

    assert result.disposition == "ready"
    assert result.binding.profile == "work"
    assert [request.header_text for request in interaction.requests if isinstance(request, ChoiceRequest)] == [
        "Select a profile for fixture:alpha:",
        "Select a non-conflicting profile for fixture:alpha@basic:",
    ]


def test_track_resolver_skips_declined_different_profile_implicit_override(tmp_path: Path) -> None:
    config_path = write_named_manager_config(tmp_path, {"example": EXAMPLE_REPO})
    write_tracked_packages_state(
        tmp_path / "state",
        repo_name="example",
        entries=[("os/arch", "basic")],
    )
    interaction = ScriptedInteraction(confirmations=[False])

    result = TrackResolver(
        DotmanEngine.from_config_path(config_path),
        interaction=interaction,
    ).resolve("example:work/git@work")

    assert result.disposition == "skipped"
    request = interaction.requests[0]
    assert isinstance(request, ConfirmationRequest)
    assert request.message is not None
    assert "Confirm explicit override for example:work/git@work:" in request.message
    assert "implicit: example:git@basic (git)" in request.message
