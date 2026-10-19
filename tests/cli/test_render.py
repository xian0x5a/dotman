from __future__ import annotations

from pathlib import Path
import re
import sys

import pytest

from dotman.cli import main
from tests.helpers import write_single_repo_config


def test_render_jinja_cli_renders_with_dotman_env(tmp_path: Path, monkeypatch, capsys) -> None:
    template_path = tmp_path / "profile"
    template_path.write_text(
        "\n".join(
            [
                "profile={{ profile }}",
                "os={{ os }}",
                "cpu_arch={{ cpu_arch }}",
                "name={{ vars.git.user_name }}",
                "{% include 'shared.txt' %}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "shared.txt").write_text("shared=1\n", encoding="utf-8")

    monkeypatch.setenv("DOTMAN_PROFILE", "basic")
    monkeypatch.setenv("DOTMAN_OS", "linux")
    monkeypatch.setenv("DOTMAN_CPU_ARCH", "riscv64")
    monkeypatch.setenv("DOTMAN_VAR_git__user_name", "Example User")

    exit_code = main(["render", "jinja", str(template_path)])

    assert exit_code == 0
    assert capsys.readouterr().out == "profile=basic\nos=linux\ncpu_arch=riscv64\nname=Example User\nshared=1\n"


def test_render_jinja_cli_accepts_explicit_profile_os_cpu_arch_and_vars(tmp_path: Path, capsys) -> None:
    template_path = tmp_path / "profile"
    template_path.write_text(
        "profile={{ profile }} os={{ os }} cpu_arch={{ cpu_arch }} name={{ vars.git.user_name }}\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "render",
            "jinja",
            "--profile",
            "work",
            "--os",
            "darwin",
            "--cpu-arch",
            "arm64",
            "--var",
            "git.user_name=Work User",
            str(template_path),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "profile=work os=darwin cpu_arch=arm64 name=Work User\n"


@pytest.mark.parametrize(
    ("args", "expected_header", "expected_error"),
    [
        (["render", "jinja", "--var", "missing_equals", "template.txt"], "UsageError", "invalid --var assignment"),
        (["render", "jinja", "missing.txt"], "UsageError", "source path does not exist"),
    ],
)
def test_render_jinja_cli_reports_invalid_input(
    args: list[str],
    expected_header: str,
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    exit_code = main(args)
    error_output = capsys.readouterr().err

    assert exit_code == 2
    assert expected_error in error_output
    stripped_output = re.sub(r"\x1b\[[0-9;]*m", "", error_output)
    assert f"\n:: {expected_header}\n" in stripped_output
    assert "detail:" in stripped_output
    assert "\x1b[" in error_output


def test_main_formats_toml_load_error_with_package_path_and_error_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    repo_root = tmp_path / "repo"
    package_manifest_path = repo_root / "packages" / "git" / "package.toml"
    package_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    package_manifest_path.write_text(
        "\n".join(
            [
                "",
                "[targets.git]",
                'source = "files/gitconfig"',
                'path = "~/.gitconfig',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = write_single_repo_config(tmp_path, repo_name="fixture", repo_path=repo_root)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    exit_code = main(["--config", str(config_path), "list", "tracked"])
    error_output = capsys.readouterr().err

    assert exit_code == 2
    assert "TomlLoadError" in error_output
    assert "package:" in error_output
    assert "path:" in error_output
    assert "detail:" in error_output
    stripped_output = re.sub(r"\x1b\[[0-9;]*m", "", error_output)
    assert "\n:: TomlLoadError\n" in stripped_output
    assert "fixture:git" in stripped_output
    assert str(package_manifest_path) in error_output
    assert "\x1b[" in error_output
