"""Contract of the fzf-backed menus, driven through a fake `fzf` on PATH (#152).

Terminal menus hand long lists to fzf. They must tell a user
cancel apart from an fzf failure, and must not rely on `--accept-nth`
(fzf >= 0.60): the minimum supported fzf is 0.54.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from dotman.interaction import ChoiceOption, ChoiceRequest, TerminalInteraction

# MENU_SELECTION_OVERHEAD_LINES is 6, so a 7-line terminal fits one menu row
# and any longer menu goes to fzf.
SHORT_TERMINAL_LINES = "7"
OPTION_COUNT = 12
# A two-digit index proves dotman reads the whole first field, not one character.
SELECTED_LINE_NUMBER = 11

PackageOption = tuple[str, tuple[str, ...]]


def _package_options() -> list[PackageOption]:
    return [
        (f"sandbox/pkg-{number:02d} [package]", (f"sandbox/pkg-{number:02d}", "[package]"))
        for number in range(1, OPTION_COUNT + 1)
    ]


def _choose_in_terminal(options: Sequence[PackageOption]) -> str:
    request = ChoiceRequest(
        header_text="Select a package:",
        options=tuple(
            ChoiceOption(value=label, label=label, display_fields=display_fields)
            for label, display_fields in options
        ),
    )
    return TerminalInteraction().choose(request)


@pytest.fixture
def install_fake_fzf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Put a shell-script `fzf` first on PATH and shrink the terminal so menus use it."""

    def install(script_body: str) -> None:
        bin_dir = tmp_path / "fake-fzf-bin"
        bin_dir.mkdir()
        fake_fzf = bin_dir / "fzf"
        fake_fzf.write_text(f"#!/bin/sh\n{script_body}\n", encoding="utf-8")
        fake_fzf.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        # shutil.get_terminal_size honours LINES before querying the terminal.
        monkeypatch.setenv("LINES", SHORT_TERMINAL_LINES)

    return install


def test_fzf_selection_takes_index_from_first_field_of_printed_line(
    install_fake_fzf: Callable[[str], None],
) -> None:
    # Without `--accept-nth`, fzf prints the whole selected input line:
    # "<index> <display fields...>".
    install_fake_fzf(f"exec sed -n '{SELECTED_LINE_NUMBER}p'")
    options = _package_options()

    assert _choose_in_terminal(options) == options[SELECTED_LINE_NUMBER - 1][0]


@pytest.mark.parametrize(
    ("exit_code", "stderr_text", "expected_error", "expected_message_patterns"),
    [
        pytest.param(130, "", KeyboardInterrupt, (), id="esc-or-ctrl-c-cancels"),
        pytest.param(1, "", KeyboardInterrupt, (), id="no-match-cancels"),
        pytest.param(
            2,
            "unknown option: --accept-nth=1",
            ValueError,
            (re.escape("unknown option: --accept-nth=1"), r"\b2\b"),
            id="fzf-error-reports-stderr-and-status",
        ),
    ],
)
def test_fzf_exit_status_distinguishes_cancel_from_fzf_failure(
    install_fake_fzf: Callable[[str], None],
    exit_code: int,
    stderr_text: str,
    expected_error: type[BaseException],
    expected_message_patterns: tuple[str, ...],
) -> None:
    install_fake_fzf(f"printf '%s\\n' '{stderr_text}' >&2\nexit {exit_code}")

    # Catch BaseException: an unexpected KeyboardInterrupt escaping a test
    # aborts the whole pytest session instead of failing this case.
    with pytest.raises(BaseException) as raised:
        _choose_in_terminal(_package_options())

    assert raised.type is expected_error, f"expected {expected_error.__name__}, got {raised.type.__name__}"
    message = str(raised.value)
    for pattern in expected_message_patterns:
        assert re.search(pattern, message), f"expected /{pattern}/ in error message {message!r}"


@pytest.mark.skipif(shutil.which("fzf") is None, reason="needs a real fzf")
@pytest.mark.parametrize(
    "output_shaping_option",
    [
        pytest.param("--print-query", id="print-query-adds-query-line"),
        pytest.param("--read0", id="read0-merges-all-options-into-one"),
        # Header lines cover the selected option, so fzf could never pick it.
        pytest.param(f"--header-lines={SELECTED_LINE_NUMBER}", id="header-lines-hide-the-first-options"),
    ],
)
def test_fzf_selection_ignores_output_shaping_user_defaults(
    monkeypatch: pytest.MonkeyPatch,
    output_shaping_option: str,
) -> None:
    # fzf merges FZF_DEFAULT_OPTS into dotman's command line (#241). `--filter`
    # runs the real fzf without a terminal and picks the one matching option.
    monkeypatch.delenv("FZF_DEFAULT_OPTS_FILE", raising=False)
    monkeypatch.setenv("FZF_DEFAULT_OPTS", f"--exact --filter=pkg-{SELECTED_LINE_NUMBER} {output_shaping_option}")
    monkeypatch.setenv("LINES", SHORT_TERMINAL_LINES)
    options = _package_options()

    assert _choose_in_terminal(options) == options[SELECTED_LINE_NUMBER - 1][0]


def test_choice_request_rejects_options_that_share_a_value() -> None:
    # A pick returns its value, so a shared value would hide which row was chosen.
    with pytest.raises(ValueError, match="distinct values"):
        ChoiceRequest(header_text="pick", options=(ChoiceOption("x", "package x"), ChoiceOption("x", "group x")))
