from __future__ import annotations

import json
import stat
from pathlib import Path

from types import SimpleNamespace

import pytest

import dotman.add as add_module
import dotman.cli_interaction as cli
from dotman.add import AddOperationResult, AddReviewResult, resolve_live_path_spec, review_add_manifest
from dotman.cli import main
from dotman.command_runtime import ArgvCommand, CommandResult, MemoryCommandRuntime
from dotman.interaction import ConfirmationRequest, ScriptedInteraction

from tests.helpers import capture_parser_help, write_named_manager_config


def _write_repo(repo_root: Path, packages: dict[str, str] | None = None) -> None:
    for package_id, manifest_text in (packages or {}).items():
        package_root = repo_root / "packages" / Path(*package_id.split("/"))
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "package.toml").write_text(manifest_text, encoding="utf-8")


def test_add_help_uses_live_path_then_optional_package_query(capsys) -> None:
    output = capture_parser_help(capsys, "add")

    assert "usage: dotman add [-h] <live-path> [<package-query>]" in output
    assert "<live-path>" in output
    assert "[<package-query>]" in output


def test_add_cli_updates_existing_package_manifest_in_json_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    live_path = home / ".gitconfig"
    live_path.write_text("[user]\nname = Example\n", encoding="utf-8")
    live_path.chmod(0o600)

    repo_root = tmp_path / "repo"
    _write_repo(repo_root, {"git": ''})
    manifest_path = repo_root / "packages" / "git" / "package.toml"
    manifest_path.chmod(0o600)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main([
        "--config",
        str(config_path),
        "--json",
        "--no-confirm",
        "add",
        str(live_path),
        "fixture:git",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "add"
    assert payload["repo"] == "fixture"
    assert payload["package_id"] == "git"
    assert payload["created_package"] is False
    assert payload["target"]["name"] == "f_gitconfig"
    assert payload["target"]["source"] == "files/gitconfig"
    assert payload["target"]["path"] == "~/.gitconfig"
    assert payload["target"]["chmod"] == "600"

    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert '[targets.f_gitconfig]' in manifest_text
    assert 'source = "files/gitconfig"' in manifest_text
    assert 'path = "~/.gitconfig"' in manifest_text
    assert 'chmod = "600"' in manifest_text
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


def test_add_cli_creates_new_package_manifest_from_relative_live_path(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home)

    live_path = home / ".config" / "nvim" / "init.lua"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("vim.g.mapleader = ','\n", encoding="utf-8")
    live_path.chmod(0o644)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main([
        "--config",
        str(config_path),
        "--json",
        "--no-confirm",
        "add",
        ".config/nvim/init.lua",
        "fixture:newpkg",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created_package"] is True
    assert payload["package_id"] == "newpkg"
    assert payload["target"]["name"] == "f_config_nvim_init_lua"
    assert payload["target"]["source"] == "files/config/nvim/init.lua"
    assert payload["target"]["path"] == "~/.config/nvim/init.lua"
    assert "chmod" not in payload["target"]

    manifest_path = repo_root / "packages" / "newpkg" / "package.toml"
    assert manifest_path.read_text(encoding="utf-8") == "\n".join(
        [
            "[targets.f_config_nvim_init_lua]",
            'source = "files/config/nvim/init.lua"',
            'path = "~/.config/nvim/init.lua"',
            "",
        ]
    )


def test_add_cli_suffixes_duplicate_target_key_in_same_package(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    live_path = home / ".gitconfig"
    live_path.write_text("new\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    _write_repo(
        repo_root,
        {
            "git": "\n".join(
                [
                    "",
                    "[targets.f_gitconfig]",
                    'source = "files/other"',
                    'path = "~/.other"',
                    "",
                ]
            )
        },
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main([
        "--config",
        str(config_path),
        "--json",
        "--no-confirm",
        "add",
        str(live_path),
        "fixture:git",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"]["name"] == "f_gitconfig_2"
    manifest_text = (repo_root / "packages" / "git" / "package.toml").read_text(encoding="utf-8")
    assert "[targets.f_gitconfig_2]" in manifest_text


def test_add_cli_fails_when_package_already_declares_same_live_path(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    live_path = home / ".gitconfig"
    live_path.write_text("new\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    _write_repo(
        repo_root,
        {
            "git": "\n".join(
                [
                    "",
                    "[targets.existing]",
                    'source = "files/gitconfig"',
                    'path = "~/.gitconfig"',
                    "",
                ]
            )
        },
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main([
        "--config",
        str(config_path),
        "add",
        str(live_path),
        "fixture:git",
    ])

    assert exit_code == 2
    assert "already declares target path '~/.gitconfig'" in capsys.readouterr().err


def test_add_cli_keeps_package_trackable_when_live_files_derive_the_same_source(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    # `~/.zshrc` and `~/zshrc` both derive `files/zshrc` once leading dots are stripped.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in (".zshrc", "zshrc"):
        (home / name).write_text(f"{name}\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    sources = []
    for name in (".zshrc", "zshrc"):
        assert main(["--config", str(config_path), "--json", "--no-confirm", "add", str(home / name), "fixture:shell"]) == 0
        sources.append(json.loads(capsys.readouterr().out)["target"]["source"])

    assert len(set(sources)) == 2
    assert main(["--config", str(config_path), "--no-confirm", "track", "fixture:shell@default"]) == 0


def test_add_cli_strips_leading_dots_from_source_components_outside_home(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    live_path = tmp_path / "outside" / ".ssh" / "ssh_config"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("Host *\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main([
        "--config",
        str(config_path),
        "--json",
        "--no-confirm",
        "add",
        str(live_path),
        "fixture:ssh",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"]["path"] == str(live_path.resolve())
    assert payload["target"]["source"].endswith("/outside/ssh/ssh_config")
    assert "/.ssh/" not in payload["target"]["source"]


def test_add_cli_interactively_allows_create_when_package_query_is_omitted(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)

    live_path = home / ".config" / "foo.conf"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("value\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    _write_repo(repo_root, {"existing": ''})
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    interaction = ScriptedInteraction(
        choices=[None, "fixture"],
        text_inputs=["newpkg"],
    )

    exit_code = main([
        "--config",
        str(config_path),
        "add",
        str(live_path),
    ], interaction=interaction)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert interaction.requests[0].header_text == "Select a package for add:"
    assert "created package config fixture:newpkg" in output
    assert (repo_root / "packages" / "newpkg" / "package.toml").exists()


def test_review_add_manifest_uses_dedicated_add_review_wording(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_path = tmp_path / "package.toml"
    manifest_path.write_text('', encoding="utf-8")
    monkeypatch.setenv("EDITOR", "nvim")

    recorded: dict[str, object] = {}

    def fake_run(command: list[str], check: bool):
        recorded["command"] = command
        recorded["check"] = check
        review_path = Path(command[1])
        editable_path = Path(command[2])
        assert review_path.name == "add-review.md"
        review_text = review_path.read_text(encoding="utf-8")
        assert "# Dotman Add Review" in review_text
        assert "Nothing is written back to the repo until dotman asks for confirmation after the editor exits." in review_text
        assert "## Summary" in review_text
        assert "- action: update package manifest" in review_text
        assert "- package: fixture:git" in review_text
        assert "- target: f_gitconfig [file]" not in review_text
        assert "- source: files/gitconfig" not in review_text
        assert "- path: ~/.gitconfig" not in review_text
        assert f"- package manifest path: {manifest_path}" in review_text
        assert f"- current manifest: {manifest_path}" in review_text
        assert "- proposed manifest: proposed package.toml" in review_text
        assert f"- editable manifest copy: {editable_path}" in review_text
        assert "--- current package.toml" in review_text
        assert "+++ proposed package.toml" in review_text
        editable_path.write_text('[targets.f_gitconfig]\n', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    def run_editor(request):
        assert isinstance(request.command, ArgvCommand)
        completed = fake_run(list(request.command.arguments), False)
        return CommandResult(exit_code=completed.returncode)

    runtime = MemoryCommandRuntime([run_editor])
    monkeypatch.setattr(add_module, "current_command_runtime", lambda: runtime)

    result = AddOperationResult(
        repo_name="fixture",
        package_id="git",
        manifest_path=manifest_path,
        target_name="f_gitconfig",
        target_kind="file",
        source_path="files/gitconfig",
        config_path="~/.gitconfig",
        chmod=None,
        created_package=False,
        before_text='',
        after_text='[targets.f_gitconfig]\n',
    )

    review_result = review_add_manifest(result)
    assert review_result is not None
    assert review_result.exit_code == 0
    assert review_result.manifest_text == '[targets.f_gitconfig]\n'
    assert recorded["check"] is False
    assert recorded["command"][0] == "nvim"


def test_add_cli_reports_no_effective_package_config_changes_after_editor_review(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    interaction = ScriptedInteraction()

    live_path = home / ".config" / "foo.conf"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("value\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    manifest_path = repo_root / "packages" / "git" / "package.toml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('', encoding="utf-8")
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    monkeypatch.setattr(cli, "add_editor_available", lambda: True)

    def fake_review(result: AddOperationResult) -> AddReviewResult:
        return AddReviewResult(exit_code=0, manifest_text=result.before_text)

    monkeypatch.setattr(cli, "review_add_manifest", fake_review)
    exit_code = main([
        "--config",
        str(config_path),
        "add",
        str(live_path),
        "fixture:git",
    ], interaction=interaction)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert interaction.requests == []
    assert "No package config changes." in output
    assert manifest_path.read_text(encoding="utf-8") == ''



def test_add_cli_keeps_manifest_unchanged_when_editor_review_is_declined(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    live_path = home / ".config" / "foo.conf"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("value\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    monkeypatch.setattr(cli, "add_editor_available", lambda: True)
    monkeypatch.setattr(
        cli,
        "review_add_manifest",
        lambda result: AddReviewResult(exit_code=0, manifest_text=result.after_text),
    )
    interaction = ScriptedInteraction(
        choices=[None],
        confirmations=[False],
        text_inputs=[""],
    )

    exit_code = main([
        "--config",
        str(config_path),
        "add",
        str(live_path),
        "fixture:newpkg",
    ], interaction=interaction)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert interaction.requests[-1] == ConfirmationRequest(
        prompt="Write package config changes for fixture:newpkg? [y/n] "
    )
    assert "kept package config unchanged fixture:newpkg" in output
    assert not (repo_root / "packages" / "newpkg" / "package.toml").exists()


def test_add_json_does_not_authorize_manifest_write(tmp_path, monkeypatch, capsys) -> None:
    live_path = tmp_path / "live"
    live_path.write_text("content")
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, {"app": ''})
    manifest = repo_root / "packages/app/package.toml"
    before = manifest.read_bytes()
    config = write_named_manager_config(tmp_path, {"fixture": repo_root})

    assert main(["--config", str(config), "--json", "add", str(live_path), "fixture:app"]) == 1
    assert manifest.read_bytes() == before
    assert not (manifest.parent / "files").exists()
    assert "--no-confirm" in capsys.readouterr().err


def test_add_no_confirm_skips_editor_even_with_terminal(tmp_path, monkeypatch) -> None:
    live_path = tmp_path / "live"
    live_path.write_text("content")
    repo_root = tmp_path / "repo"
    _write_repo(repo_root, {"app": ''})
    config = write_named_manager_config(tmp_path, {"fixture": repo_root})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    def unexpected_editor(*args, **kwargs):
        raise AssertionError("--no-confirm must not open an editor")
    monkeypatch.setattr(cli, "review_add_manifest", unexpected_editor)
    monkeypatch.setattr(cli, "add_editor_available", lambda: True)

    assert main(["--config", str(config), "--no-confirm", "add", str(live_path), "fixture:app"]) == 0
    assert "[targets." in (repo_root / "packages/app/package.toml").read_text()


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_add_writes_chmod_the_manifest_accepts(tmp_path: Path, kind: str) -> None:
    live_path = tmp_path / "live"
    if kind == "file":
        live_path.write_text("payload\n", encoding="utf-8")
    else:
        live_path.mkdir()
    live_path.chmod(0o040)
    try:
        spec = resolve_live_path_spec(str(live_path), cwd=tmp_path, home=tmp_path)
    finally:
        # Let pytest clean up a directory its owner cannot list.
        live_path.chmod(0o700)

    # The manifest requires 3 or 4 octal digits, so a mode below 0o100 keeps its leading zero.
    assert spec.chmod == "040"


@pytest.mark.parametrize("package_query", ["git", "main:git"])
def test_add_cli_non_interactive_never_writes_into_a_partial_package_match(
    tmp_path: Path,
    monkeypatch,
    package_query: str,
) -> None:
    # Partial package matches are for interactive use; without a prompt, `git`
    # must not silently land in the only partially matching package `gitui`.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    live_path = home / ".gitconfig"
    live_path.write_text("[user]\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    _write_repo(repo_root, {"gitui": ''})
    gitui_package = repo_root / "packages" / "gitui"
    manifest_before = (gitui_package / "package.toml").read_bytes()
    config_path = write_named_manager_config(tmp_path, {"main": repo_root})

    main(["--config", str(config_path), "--no-confirm", "add", str(live_path), package_query])

    assert (gitui_package / "package.toml").read_bytes() == manifest_before


@pytest.mark.parametrize("relative_path", ["café/settings.ini", "\U0001F600/notes.txt"])
def test_add_cli_adds_non_ascii_live_path_to_a_loadable_manifest(
    tmp_path: Path,
    monkeypatch,
    capsys,
    relative_path: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    live_path = home / ".config" / relative_path
    live_path.parent.mkdir(parents=True)
    live_path.write_text("value\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    _write_repo(repo_root, {"app": ''})
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    add_exit_code = main(["--config", str(config_path), "--no-confirm", "add", str(live_path), "fixture:app"])

    assert add_exit_code == 0, capsys.readouterr().err
    capsys.readouterr()
    assert main(["--config", str(config_path), "--json", "info", "trackable", "fixture:app"]) == 0
    targets = json.loads(capsys.readouterr().out)["trackable"]["targets"]
    assert [target["path"] for target in targets] == [f"~/.config/{relative_path}"]


def _package_selectors(config_path: Path, capsys) -> set[str]:
    assert main(["--config", str(config_path), "--json", "list", "trackables"]) == 0
    payload = json.loads(capsys.readouterr().out)
    return {entry["qualified_selector"] for entry in payload["trackables"] if entry["kind"] == "package"}


def test_add_cli_creates_namespaced_package_without_id_line_that_still_loads(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    live_path = home / ".gitconfig"
    live_path.write_text("[user]\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    _write_repo(repo_root, {"git": ""})
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main([
        "--config", str(config_path), "--json", "--no-confirm", "add", str(live_path), "fixture:work/git",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created_package"] is True
    assert payload["package_id"] == "work/git"
    manifest_text = (repo_root / "packages" / "work" / "git" / "package.toml").read_text(encoding="utf-8")
    assert not any(line.strip().startswith("id") for line in manifest_text.splitlines())
    assert _package_selectors(config_path, capsys) == {"fixture:git", "fixture:work/git"}


def test_add_cli_writes_reviewed_new_manifest_without_requiring_id(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    live_path = home / ".config" / "foo.conf"
    live_path.parent.mkdir(parents=True)
    live_path.write_text("value\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    monkeypatch.setattr(cli, "add_editor_available", lambda: True)
    # The user rewrites the manifest by hand and, as the folder now names the package, writes no id line.
    reviewed_manifest_text = "\n".join([
        'description = "reviewed"',
        "",
        "[targets.foo]",
        'source = "files/config/foo.conf"',
        'path = "~/.config/foo.conf"',
        "",
    ])
    monkeypatch.setattr(
        cli,
        "review_add_manifest",
        lambda result: AddReviewResult(exit_code=0, manifest_text=reviewed_manifest_text),
    )
    interaction = ScriptedInteraction(choices=[None], confirmations=[True], text_inputs=[""])

    exit_code = main(
        ["--config", str(config_path), "add", str(live_path), "fixture:work/newpkg"],
        interaction=interaction,
    )

    assert exit_code == 0
    capsys.readouterr()
    manifest_text = (repo_root / "packages" / "work" / "newpkg" / "package.toml").read_text(encoding="utf-8")
    assert manifest_text == reviewed_manifest_text
    assert _package_selectors(config_path, capsys) == {"fixture:work/newpkg"}


def _repo_snapshot(repo_root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(repo_root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(repo_root.rglob("*"))
    }


@pytest.mark.parametrize(
    ("existing_package_id", "package_content_files", "new_package_query", "conflicting_selector"),
    [
        # The new package's folder would sit inside an existing package's folder.
        ("app", {}, "fixture:app/sub", "fixture:app"),
        # The new package's folder already holds a package below it.
        ("work/git", {}, "fixture:work", "fixture:work/git"),
        # A dotfile source named package.toml is package content, not a manifest (#208),
        # so the new package would still nest inside `app` and must not edit that dotfile.
        ("app", {"files/package.toml": 'name = "my-tool-settings"\n'}, "fixture:app/files", "fixture:app"),
    ],
)
def test_add_cli_refuses_to_create_a_package_that_would_nest(
    tmp_path: Path,
    monkeypatch,
    capsys,
    existing_package_id: str,
    package_content_files: dict[str, str],
    new_package_query: str,
    conflicting_selector: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    live_path = home / ".gitconfig"
    live_path.write_text("[user]\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    _write_repo(repo_root, {existing_package_id: ""})
    package_root = repo_root / "packages" / existing_package_id
    for relative_path, content in package_content_files.items():
        (package_root / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (package_root / relative_path).write_text(content, encoding="utf-8")
    snapshot_before = _repo_snapshot(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main(["--config", str(config_path), "--no-confirm", "add", str(live_path), new_package_query])

    captured = capsys.readouterr()
    assert exit_code != 0
    assert conflicting_selector in captured.out + captured.err
    assert _repo_snapshot(repo_root) == snapshot_before
    assert _package_selectors(config_path, capsys) == {f"fixture:{existing_package_id}"}


def test_add_cli_creates_a_namespaced_sibling_next_to_an_existing_package(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    live_path = home / ".toolsrc"
    live_path.write_text("value\n", encoding="utf-8")

    repo_root = tmp_path / "repo"
    _write_repo(repo_root, {"work/git": ""})
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main([
        "--config", str(config_path), "--json", "--no-confirm", "add", str(live_path), "fixture:work/tools",
    ])

    assert exit_code == 0, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert payload["created_package"] is True
    assert payload["package_id"] == "work/tools"
    assert (repo_root / "packages" / "work" / "tools" / "package.toml").is_file()
    assert _package_selectors(config_path, capsys) == {"fixture:work/git", "fixture:work/tools"}
