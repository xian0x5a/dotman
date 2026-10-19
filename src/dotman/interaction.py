from __future__ import annotations

import os
import shutil
import sys
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Generic, Protocol, TextIO, TypeVar, cast

from dotman import cli_style
from dotman.command_runtime import (
    ArgvCommand,
    CommandRequest,
    CommandRuntime,
    current_command_runtime,
)
from dotman.terminal import colors_enabled, read_prompt_line
from dotman.ui_context import current_ui_config

ChoiceValue = TypeVar("ChoiceValue")
MENU_SELECTION_OVERHEAD_LINES = 6


@dataclass(frozen=True)
class ChoiceOption(Generic[ChoiceValue]):
    value: ChoiceValue
    label: str
    display_fields: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ChoiceRequest(Generic[ChoiceValue]):
    header_text: str
    options: tuple[ChoiceOption[ChoiceValue], ...]


@dataclass(frozen=True)
class ConfirmationRequest:
    prompt: str
    default: bool | None = None
    message: str | None = None


@dataclass(frozen=True)
class TextInputRequest:
    prompt: str


InteractionRequest = ChoiceRequest[object] | ConfirmationRequest | TextInputRequest


class Interaction(Protocol):
    def choose(self, request: ChoiceRequest[ChoiceValue]) -> ChoiceValue: ...

    def confirm(self, request: ConfirmationRequest) -> bool: ...

    def read_text(self, request: TextInputRequest) -> str: ...


def _fzf_available() -> bool:
    return shutil.which("fzf") is not None


# fzf exits 130 on Esc or Ctrl-C and 1 when Enter finds no match; both are a
# user cancel. Any other failure is fzf's own error, such as an unknown option.
_FZF_CANCEL_EXIT_CODES = frozenset({1, 130})

# fzf prepends FZF_DEFAULT_OPTS to its arguments, and later options win. Reset
# the user defaults that change how options are read or how the pick is printed,
# so the output stays one "<index> <fields>" line, while theme and layout
# defaults still apply. `--accept-nth` (fzf 0.60+) has no reset and is unsupported.
_FZF_PINNED_RECORD_OPTIONS = (
    "--no-multi",
    "--no-print-query",
    "--no-expect",
    "--no-read0",
    "--no-print0",
    "--header-lines=0",
)


def choose_with_fzf(
    runtime: CommandRuntime,
    *,
    header_text: str,
    option_display_fields: Sequence[Sequence[str]],
) -> str:
    """Let the user pick an option in fzf; return its 1-based index as typed text."""
    entries = [
        " ".join((str(index), *(field for field in display_fields if field)))
        for index, display_fields in enumerate(option_display_fields, start=1)
    ]
    result = runtime.run(
        CommandRequest(
            command=ArgvCommand(
                (
                    "fzf",
                    "--prompt=Select> ",
                    f"--header={header_text}",
                    "--ansi",
                    # --wrap needs fzf 0.54, the documented minimum.
                    "--wrap",
                    "--with-nth=2..",
                    "--no-sort",
                    *_FZF_PINNED_RECORD_OPTIONS,
                )
            ),
            input=("\n".join(entries) + "\n").encode("utf-8"),
            # fzf reads its UI from the controlling terminal while selection
            # data uses pipes, so it must stay in Dotman's process group.
            isolate_process_group=False,
        )
    )
    if result.exit_code in _FZF_CANCEL_EXIT_CODES:
        raise KeyboardInterrupt
    if result.exit_code != 0:
        detail = result.stderr_text.strip() or "no error output"
        raise ValueError(f"fzf failed with exit status {result.exit_code}: {detail}")
    # fzf prints the whole picked line; its first field is the option index.
    selected = result.stdout_text.strip().partition(" ")[0]
    if not selected:
        # An empty answer means "default" to the index parsers.
        raise ValueError("fzf exited without a selection")
    return selected


def _terminal_lines() -> int:
    return shutil.get_terminal_size((80, 24)).lines


@dataclass
class TerminalInteraction:
    input_stream: TextIO = field(default_factory=lambda: sys.stdin)
    output_stream: TextIO = field(default_factory=lambda: sys.stdout)
    error_stream: TextIO = field(default_factory=lambda: sys.stderr)
    command_runtime: CommandRuntime | None = None
    fzf_available: Callable[[], bool] = _fzf_available
    terminal_lines: Callable[[], int] = _terminal_lines
    use_color: bool | None = None
    menus_bottom_up: bool | None = None

    def choose(self, request: ChoiceRequest[ChoiceValue]) -> ChoiceValue:
        if not request.options:
            raise ValueError("choice request must include at least one option")
        if self.fzf_available() and self._should_use_fzf(request):
            selected_index = self._choose_with_fzf(request)
        else:
            selected_index = self._choose_with_prompt(request)
        return request.options[selected_index].value

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.message is not None:
            self.output_stream.write(request.message)
        while True:
            answer = self._prompt(request.prompt).lower()
            if answer == "" and request.default is not None:
                return request.default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.error_stream.write("invalid confirmation: enter 'y' or 'n'\n")

    def read_text(self, request: TextInputRequest) -> str:
        return self._prompt(request.prompt)

    def _should_use_fzf(self, request: ChoiceRequest[ChoiceValue]) -> bool:
        available_menu_lines = max(1, self.terminal_lines() - MENU_SELECTION_OVERHEAD_LINES)
        return len(request.options) > available_menu_lines

    def _choose_with_prompt(self, request: ChoiceRequest[ChoiceValue]) -> int:
        self._print_selection_header(request.header_text)
        indexed_options = list(enumerate(request.options, start=1))
        if self._menus_bottom_up_enabled():
            indexed_options.reverse()
        for index, option in indexed_options:
            self._print_selection_item(index, option.label)

        while True:
            answer = self._prompt(self._selection_prompt())
            if answer == "?":
                self.output_stream.write("Selection help:\n  <number>  choose that item\n")
                continue
            try:
                return self._parse_selection_index(answer, len(request.options)) - 1
            except ValueError as error:
                self.error_stream.write(f"invalid selection: {error}\n")

    def _choose_with_fzf(self, request: ChoiceRequest[ChoiceValue]) -> int:
        selected = choose_with_fzf(
            self.command_runtime or current_command_runtime(),
            header_text=request.header_text,
            option_display_fields=[
                option.display_fields if option.display_fields is not None else (option.label,)
                for option in request.options
            ],
        )
        return self._parse_selection_index(selected, len(request.options)) - 1

    def _print_selection_header(self, header_text: str) -> None:
        self.output_stream.write("\n")
        if not self._colors_enabled():
            self.output_stream.write(f"{header_text}\n")
            return
        marker = cli_style.style_text(cli_style.MENU_HEADER_MARKER, *cli_style.MENU_HEADER_MARKER_STYLE)
        self.output_stream.write(f"{marker} {cli_style.style_text(header_text, '1')}\n")

    def _print_selection_item(self, index: int, label: str) -> None:
        if not self._colors_enabled():
            self.output_stream.write(f"  {index:>2}) {label}\n")
            return
        rendered_index = cli_style.style_text(f"{index:>2})", *cli_style.MENU_INDEX_STYLE)
        self.output_stream.write(f"  {rendered_index} {label}\n")

    def _selection_prompt(self) -> str:
        prompt_text = "Select a number"
        hint_text = '("?"; default: 1)'
        if not self._colors_enabled():
            return f"{prompt_text} {hint_text}: "
        marker = cli_style.style_text(cli_style.MENU_HEADER_MARKER, *cli_style.MENU_HEADER_MARKER_STYLE)
        prompt = cli_style.style_text(prompt_text, *cli_style.MENU_PROMPT_STYLE)
        hint = cli_style.style_text(hint_text, *cli_style.MENU_HINT_STYLE)
        return f"{marker} {prompt} {hint}: "

    def _prompt(self, message: str) -> str:
        return read_prompt_line(
            message,
            input_stream=self.input_stream,
            output_stream=self.output_stream,
        )

    def _colors_enabled(self) -> bool:
        if self.use_color is not None:
            return self.use_color
        return colors_enabled(self.output_stream)

    def _menus_bottom_up_enabled(self) -> bool:
        if self.menus_bottom_up is not None:
            return self.menus_bottom_up
        raw_value = os.environ.get("DOTMAN_MENU_BOTTOM_UP")
        if raw_value is not None:
            return raw_value.strip().lower() not in {"0", "false", "no", "off"}
        ui_config = current_ui_config()
        if ui_config is not None:
            return ui_config.menus.bottom_up
        return True

    @staticmethod
    def _parse_selection_index(raw_answer: str, item_count: int) -> int:
        answer = raw_answer.strip()
        if not answer:
            return 1
        if not answer.isdigit():
            raise ValueError(f"unsupported selection: {answer}")
        selected_index = int(answer)
        if not 1 <= selected_index <= item_count:
            raise ValueError(f"selection index out of range: {selected_index}")
        return selected_index


@dataclass(init=False)
class ScriptedInteraction:
    _choices: deque[object]
    _confirmations: deque[bool]
    _text_inputs: deque[str]
    requests: list[InteractionRequest] = field(default_factory=list)

    def __init__(
        self,
        *,
        choices: Iterable[object] = (),
        confirmations: Iterable[bool] = (),
        text_inputs: Iterable[str] = (),
    ) -> None:
        self._choices = deque(choices)
        self._confirmations = deque(confirmations)
        self._text_inputs = deque(text_inputs)
        self.requests = []

    def choose(self, request: ChoiceRequest[ChoiceValue]) -> ChoiceValue:
        self.requests.append(cast(ChoiceRequest[object], request))
        if not self._choices:
            raise AssertionError("scripted interaction has no queued choice")
        scripted_choice = self._choices.popleft()
        for option in request.options:
            if option.value == scripted_choice:
                return option.value
        raise AssertionError(f"scripted choice is not present in request options: {scripted_choice!r}")

    def confirm(self, request: ConfirmationRequest) -> bool:
        self.requests.append(request)
        if not self._confirmations:
            raise AssertionError("scripted interaction has no queued confirmation")
        return self._confirmations.popleft()

    def read_text(self, request: TextInputRequest) -> str:
        self.requests.append(request)
        if not self._text_inputs:
            raise AssertionError("scripted interaction has no queued text input")
        return self._text_inputs.popleft()
