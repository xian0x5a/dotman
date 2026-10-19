from __future__ import annotations

from collections.abc import Callable

import pytest

from dotman.rewrites.home import active_home_path, collapse_home_paths, expand_home_paths


EXPAND_INPUT = "proxy: ~\ndir: ~/projects ~/"
COLLAPSE_INPUT = "dir: /home/alice\nsub: /home/alice/projects /home/alice/"


# A bare `~` is YAML null, so bare forms only rewrite when explicitly opted in;
# descendants (including a trailing `/`) always rewrite.
@pytest.mark.parametrize(
    ("rewrite", "text", "options", "expected"),
    [
        pytest.param(
            expand_home_paths,
            EXPAND_INPUT,
            {},
            "proxy: ~\ndir: /home/alice/projects /home/alice/",
            id="expand-default",
        ),
        pytest.param(
            expand_home_paths,
            EXPAND_INPUT,
            {"bare_tilde": True},
            "proxy: /home/alice\ndir: /home/alice/projects /home/alice/",
            id="expand-bare-tilde",
        ),
        pytest.param(
            collapse_home_paths,
            COLLAPSE_INPUT,
            {},
            "dir: /home/alice\nsub: ~/projects ~/",
            id="collapse-default",
        ),
        pytest.param(
            collapse_home_paths,
            COLLAPSE_INPUT,
            {"bare_tilde": True},
            "dir: ~\nsub: ~/projects ~/",
            id="collapse-bare-tilde",
        ),
    ],
)
def test_home_rewrites_touch_bare_home_only_with_bare_tilde(
    rewrite: Callable[..., str],
    text: str,
    options: dict[str, bool],
    expected: str,
) -> None:
    assert rewrite(text, home="/home/alice", **options) == expected


def test_active_home_path_removes_redundant_trailing_slashes() -> None:
    assert active_home_path({"HOME": "/home/alice///"}) == "/home/alice"


@pytest.mark.parametrize("home", [None, "", "alice", "/", "///"])
def test_active_home_path_rejects_invalid_home_values(home: str | None) -> None:
    environment = {} if home is None else {"HOME": home}

    with pytest.raises(ValueError, match=r"\$HOME must be a non-root absolute POSIX path"):
        active_home_path(environment)


@pytest.mark.parametrize(
    "text",
    [
        "word~",
        "é~",
        ".~",
        "~~",
        "+~",
        "-~",
        "/~",
        r"\~",
        "~user",
        "~é",
        "~\N{COMBINING ACUTE ACCENT}",
        "~.",
        "~+",
        "~-",
        r"~\share",
        # Child-path forms reach the attachment check in default mode too.
        "word~/project",
        "é~/project",
        ".~/project",
        "~~/project",
        "+~/project",
        "-~/project",
        "https://~/project",
        r"\~/project",
    ],
)
@pytest.mark.parametrize("options", [{}, {"bare_tilde": True}], ids=["default", "bare-tilde"])
def test_expand_home_paths_protects_attached_lookalikes(text: str, options: dict[str, bool]) -> None:
    assert expand_home_paths(text, home="/home/alice", **options) == text


@pytest.mark.parametrize(
    "text",
    [
        "word/home/alice",
        "é/home/alice",
        "./home/alice",
        "~/home/alice",
        "+/home/alice",
        "-/home/alice",
        "//home/alice",
        r"\/home/alice",
        "https://host/home/alice",
        "/home/alice2",
        "/home/aliceé",
        "/home/alice\N{COMBINING ACUTE ACCENT}",
        "/home/alice.",
        "/home/alice~",
        "/home/alice+",
        "/home/alice-",
        r"/home/alice\share",
        # Child-path forms reach the attachment check in default mode too.
        "word/home/alice/project",
        "é/home/alice/project",
        "./home/alice/project",
        "~/home/alice/project",
        "+/home/alice/project",
        "-/home/alice/project",
        "//home/alice/project",
        r"\/home/alice/project",
        "https://host/home/alice/project",
    ],
)
@pytest.mark.parametrize("options", [{}, {"bare_tilde": True}], ids=["default", "bare-tilde"])
def test_collapse_home_paths_protects_attached_lookalikes(text: str, options: dict[str, bool]) -> None:
    assert collapse_home_paths(text, home="/home/alice", **options) == text


@pytest.mark.parametrize("options", [{}, {"bare_tilde": True}], ids=["default", "bare-tilde"])
def test_home_rewrites_leave_environment_variable_text_unchanged(options: dict[str, bool]) -> None:
    text = "$HOME ${HOME}"

    assert expand_home_paths(text, home="/home/alice", **options) == text
    assert collapse_home_paths(text, home="/home/alice", **options) == text
