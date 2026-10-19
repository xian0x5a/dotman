from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dotman.engine import DotmanEngine
from dotman.manifest import infer_profile_os
from dotman.profiles import compute_profile_heights, rank_profiles
from tests.helpers import write_example_local_override


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_REPO = PROJECT_ROOT / "examples" / "repo"
REFERENCE_REPO = PROJECT_ROOT / "tests" / "fixtures" / "reference_repo"


def write_manager_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    write_example_local_override(tmp_path, repo_name="example", repo_path=EXAMPLE_REPO)
    config_path.write_text(
        "\n".join(
            [
                "[repos.example]",
                f'path = "{EXAMPLE_REPO}"',
                "order = 10",
                "",
                "[repos.sandbox]",
                f'path = "{REFERENCE_REPO}"',
                "order = 20",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_compute_profile_heights_handles_nested_repo_graphs(tmp_path: Path) -> None:
    engine = DotmanEngine.from_config_path(write_manager_config(tmp_path))
    sandbox_profiles = engine.get_repo("sandbox").profiles

    heights = compute_profile_heights({name: profile.includes for name, profile in sandbox_profiles.items()})

    assert heights["runtime/python"] == 0
    assert heights["os/arch"] == 1
    assert heights["de/niri"] == 0
    assert heights["host/linux"] == 2
    assert heights["host/mac"] == 2


def test_rank_profiles_places_more_composed_profiles_first(tmp_path: Path) -> None:
    engine = DotmanEngine.from_config_path(write_manager_config(tmp_path))
    sandbox_profiles = engine.get_repo("sandbox").profiles

    ranked = rank_profiles({name: profile.includes for name, profile in sandbox_profiles.items()})

    assert ranked[0] == "host/linux"
    assert ranked[1] == "host/mac"
    assert ranked.index("os/arch") < ranked.index("runtime/python")
    assert ranked.index("os/mac") < ranked.index("runtime/python")


@pytest.mark.parametrize(
    ("profile_id", "lineage", "expected_os"),
    [
        ("os/mac", ["os/mac"], "darwin"),
        ("work", ["os/macos", "work"], "darwin"),
        ("host-darwin", ["host-darwin"], "darwin"),
        ("os/arch", ["os/arch"], "linux"),
        ("home_linux", ["home_linux"], "linux"),
        ("archlinux", ["archlinux"], "linux"),
        # Words that merely contain an OS name must not infer that OS.
        ("machete", ["machete"], "fallback"),
        ("work-machine", ["work-machine"], "fallback"),
        ("research", ["research"], "fallback"),
        ("archive-box", ["archive-box"], "fallback"),
    ],
)
def test_infer_profile_os_matches_whole_name_segments(
    monkeypatch: pytest.MonkeyPatch, profile_id: str, lineage: list[str], expected_os: str
) -> None:
    monkeypatch.setattr(sys, "platform", "fallback")

    assert infer_profile_os(profile_id, lineage, {}) == expected_os
