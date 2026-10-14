from __future__ import annotations

from pathlib import Path

import pytest

from dotman.edit_resolution import EditResolver
from dotman.engine import DotmanEngine
from dotman.interaction import ChoiceOption, ChoiceRequest, ScriptedInteraction

from tests.helpers import write_named_manager_config, write_tracked_packages_state


def _write_repo(repo_root: Path) -> None:
    (repo_root / "profiles").mkdir(parents=True, exist_ok=True)
    (repo_root / "profiles" / "basic.toml").write_text("", encoding="utf-8")
    packages = {
        "git": ("gitconfig", "files/gitconfig"),
        "altgit": ("gitconfig", "files/gitconfig"),
        "note": ("note", "files/note.txt"),
    }
    for package_id, (target_name, source) in packages.items():
        package_root = repo_root / "packages" / package_id
        source_path = package_root / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(package_id, encoding="utf-8")
        package_header = []
        target_path = f"~/.config/{package_id}"
        target_options: list[str] = []
        if package_id == "git":
            package_header.append('binding_mode = "multi_instance"')
            target_path = "~/.config/git/{{ profile }}"
            target_options = ['render = "jinja"', 'sync_policy = "push-only"']
        (package_root / "package.toml").write_text(
            "\n".join(
                [
                    *package_header,
                    "",
                    f"[targets.{target_name}]",
                    f'source = "{source}"',
                    f'path = "{target_path}"',
                    *target_options,
                    "",
                ]
            ),
            encoding="utf-8",
        )


def _engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_names: tuple[str, ...]) -> DotmanEngine:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    repo_paths: dict[str, Path] = {}
    for repo_name in repo_names:
        repo_root = tmp_path / repo_name
        _write_repo(repo_root)
        repo_paths[repo_name] = repo_root
        write_tracked_packages_state(
            tmp_path / "state",
            repo_name=repo_name,
            entries=[("git", "basic"), ("altgit", "basic"), ("note", "basic")],
        )
    return DotmanEngine.from_config_path(write_named_manager_config(tmp_path, repo_paths))


def test_edit_resolver_chooses_repo_for_local_path_and_rejects_partial_noninteractively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path, monkeypatch, ("alpha", "alpine"))
    interaction = ScriptedInteraction(choices=["alpine"])

    path = EditResolver(engine.config, engine=engine, interaction=interaction).resolve_local_path("al")

    assert path == engine.config.repos["alpine"].local_override_path
    assert interaction.requests == [
        ChoiceRequest(
            header_text="Select a repo for local overrides:",
            options=(
                ChoiceOption(value="alpha", label="alpha", display_fields=("alpha",)),
                ChoiceOption(value="alpine", label="alpine", display_fields=("alpine",)),
            ),
        )
    ]
    with pytest.raises(ValueError, match="edit local repo 'pha' is not exact; use 'alpha'"):
        EditResolver(engine.config, engine=engine).resolve_local_path("pha")


def test_edit_resolver_resolves_config_repo_package_and_package_instance_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path, monkeypatch, ("alpha",))
    resolver = EditResolver(engine.config, engine=engine)

    assert resolver.resolve_repo_path("alpha") == engine.config.repos["alpha"].path
    assert resolver.resolve_package_path("git") == engine.get_repo("alpha").resolve_package("git").package_root
    assert resolver.resolve_package_path("git<basic>") == engine.get_repo("alpha").resolve_package("git").package_root


def test_edit_resolver_interactively_ranks_and_selects_tracked_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path, monkeypatch, ("alpha",))
    git_path = engine.get_repo("alpha").resolve_package("git").package_root / "files" / "gitconfig"
    interaction = ScriptedInteraction(choices=[git_path])

    assert EditResolver(
        engine.config,
        engine=engine,
        interaction=interaction,
    ).resolve_target_path("gitconfig") == git_path
    assert interaction.requests == [
        ChoiceRequest(
            header_text="Select a tracked target for 'gitconfig':",
            options=(
                ChoiceOption(
                    value=engine.get_repo("alpha").resolve_package("altgit").package_root / "files" / "gitconfig",
                    label="alpha:altgit.gitconfig",
                ),
                ChoiceOption(value=git_path, label="alpha:git<basic>.gitconfig"),
            ),
        )
    ]


def test_edit_resolver_query_reports_cross_kind_ambiguity_and_preserves_target_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path, monkeypatch, ("alpha",))
    resolver = EditResolver(engine.config, engine=engine)

    with pytest.raises(ValueError) as exc_info:
        resolver.resolve_query_path("note")
    assert str(exc_info.value) == (
        "edit query 'note' is ambiguous: "
        "package alpha:note, target alpha:note.note"
    )
    assert resolver.resolve_query_path("alpha:note.note") == (
        engine.get_repo("alpha").resolve_package("note").package_root / "files" / "note.txt"
    )


def test_tracked_identity_resolution_returns_canonical_package_or_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path, monkeypatch, ("alpha",))
    interaction = ScriptedInteraction(choices=["alpha:git<basic>.gitconfig"])
    resolver = EditResolver(engine.config, engine=engine, interaction=interaction)

    assert resolver.resolve_tracked_identity("git", subject="sync scope") == "alpha:git<basic>"
    assert resolver.resolve_tracked_identity("gitconfig", subject="sync scope") == "alpha:git<basic>.gitconfig"
    assert interaction.requests[0].header_text == "Select a sync scope for 'gitconfig':"
