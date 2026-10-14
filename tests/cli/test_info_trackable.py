from __future__ import annotations

import json
from pathlib import Path

from dotman.cli import main
from tests.helpers import write_manager_config, write_multi_instance_repo, write_named_manager_config


def test_info_trackable_cli_emits_untracked_package_details_in_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    config_path = write_manager_config(tmp_path)

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--json",
            "info",
            "trackable",
            "git",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "info-trackable"
    trackable = payload["trackable"]
    assert trackable["kind"] == "package"
    assert trackable["repo"] == "example"
    assert trackable["selector"] == "git"
    assert trackable["tracked"] is False
    assert trackable["tracked_instances"] == []
    assert [target["target_name"] for target in trackable["targets"]] == ["gitconfig"]


def test_info_trackable_cli_exposes_declared_target_type_in_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "sample" / "files").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "sample" / "files" / "profile").write_text("profile\n", encoding="utf-8")
    (repo_root / "packages" / "sample" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.profile]",
                'type = "file"',
                'source = "files/profile"',
                'path = "~/.profile"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    exit_code = main(["--config", str(config_path), "--json", "info", "trackable", "sample"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trackable"]["targets"][0]["target_type"] == "file"


def test_info_trackable_cli_exposes_probe_targets_in_json_and_text(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "packages" / "sample").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "packages" / "sample" / "package.toml").write_text(
        "\n".join(
            [
                "",
                "[targets.version]",
                'probe = "exit 0"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})

    assert main(["--config", str(config_path), "--json", "info", "trackable", "sample"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trackable"]["targets"][0]["probe_command"] == "exit 0"

    assert main(["--config", str(config_path), "info", "trackable", "sample"]) == 0
    assert "version [probe]" in capsys.readouterr().out


def test_info_trackable_cli_emits_partial_group_status_in_text_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    (repo_root / "profiles").mkdir(parents=True)
    (repo_root / "groups").mkdir(parents=True)
    (repo_root / "packages" / "alpha" / "files").mkdir(parents=True)
    (repo_root / "packages" / "beta" / "files").mkdir(parents=True)
    (repo_root / "profiles" / "default.toml").write_text("", encoding="utf-8")
    (repo_root / "groups" / "bundle.toml").write_text('members = ["alpha", "beta"]\n', encoding="utf-8")
    (repo_root / "packages" / "alpha" / "files" / "alpha.conf").write_text("alpha\n", encoding="utf-8")
    (repo_root / "packages" / "beta" / "files" / "beta.conf").write_text("beta\n", encoding="utf-8")
    (repo_root / "packages" / "alpha" / "package.toml").write_text(
        '\n'.join([
            '',
            '[targets.alpha]',
            'source = "files/alpha.conf"',
            'path = "~/.config/alpha.conf"',
            '',
        ]),
        encoding="utf-8",
    )
    (repo_root / "packages" / "beta" / "package.toml").write_text(
        '\n'.join([
            '',
            '[targets.beta]',
            'source = "files/beta.conf"',
            'path = "~/.config/beta.conf"',
            '',
        ]),
        encoding="utf-8",
    )

    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "alpha"',
                'profile = "default"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["--config", str(config_path), "info", "trackable", "bundle"])

    assert exit_code == 0
    assert capsys.readouterr().out == "\n".join(
        [
            "fixture:bundle [group]",
            "",
            "  :: status",
            "    tracked: partially tracked",
            "    tracked members: 1/2",
            "",
            "  :: members",
            "    alpha [tracked]",
            "    beta [untracked]",
            "",
        ]
    )


def test_info_trackable_cli_lists_all_tracked_instances_for_multi_instance_package(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_multi_instance_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "profiled"',
                'profile = "basic"',
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "profiled"',
                'profile = "work"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["--config", str(config_path), "--json", "info", "trackable", "profiled"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    trackable = payload["trackable"]
    assert trackable["kind"] == "package"
    assert trackable["tracked"] is True
    assert [instance["package_ref"] for instance in trackable["tracked_instances"]] == [
        "profiled<basic>",
        "profiled<work>",
    ]


def test_info_trackable_cli_omits_instance_list_for_singleton_package_text_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
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

    exit_code = main(["--config", str(config_path), "info", "trackable", "git"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "  :: status" in output
    assert "    tracked: yes" in output
    assert "tracked instances:" not in output


def test_info_trackable_cli_keeps_instance_list_for_multi_instance_package_text_output(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repo_root = tmp_path / "repo"
    write_multi_instance_repo(repo_root)
    config_path = write_named_manager_config(tmp_path, {"fixture": repo_root})
    state_dir = tmp_path / "state" / "dotman" / "repos" / "fixture"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tracked-packages.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                "",
                "[[packages]]",
                'repo = "fixture"',
                'package_id = "profiled"',
                'profile = "basic"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(["--config", str(config_path), "info", "trackable", "profiled"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "  :: status" in output
    assert "    tracked: yes" in output
    assert "    tracked instances:" in output
    assert "      explicit: fixture:profiled<basic>" in output

