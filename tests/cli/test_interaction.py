from __future__ import annotations

from io import StringIO

import pytest

from dotman.command_runtime import ArgvCommand, CommandResult, MemoryCommandRuntime
from dotman.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    ScriptedInteraction,
    TerminalInteraction,
    TextInputRequest,
)


def test_scripted_interaction_returns_queued_responses_and_records_typed_requests() -> None:
    choice_request = ChoiceRequest(
        header_text="Select a repo:",
        options=(
            ChoiceOption(value="alpha", label="alpha"),
            ChoiceOption(value="beta", label="beta"),
        ),
    )
    confirmation_request = ConfirmationRequest(prompt="Use beta? [y/n] ")
    text_request = TextInputRequest(prompt="New package ID: ")
    interaction = ScriptedInteraction(
        choices=["beta"],
        confirmations=[True],
        text_inputs=["tools/new"],
    )

    assert interaction.choose(choice_request) == "beta"
    assert interaction.confirm(confirmation_request) is True
    assert interaction.read_text(text_request) == "tools/new"
    assert interaction.requests == [choice_request, confirmation_request, text_request]


def test_terminal_interaction_runs_numbered_menu_help_and_invalid_selection_loop() -> None:
    input_stream = StringIO("?\nwat\n2\n")
    output_stream = StringIO()
    error_stream = StringIO()
    interaction = TerminalInteraction(
        input_stream=input_stream,
        output_stream=output_stream,
        error_stream=error_stream,
        fzf_available=lambda: False,
        use_color=False,
        menus_bottom_up=True,
    )

    selected = interaction.choose(
        ChoiceRequest(
            header_text="Select a repo:",
            options=(
                ChoiceOption(value="alpha", label="alpha"),
                ChoiceOption(value="beta", label="beta"),
            ),
        )
    )

    assert selected == "beta"
    output = output_stream.getvalue()
    assert output.index("   2) beta") < output.index("   1) alpha")
    assert output.count('Select a number ("?"; default: 1): ') == 3
    assert "Selection help:\n  <number>  choose that item\n" in output
    assert error_stream.getvalue() == "invalid selection: unsupported selection: wat\n"


def test_terminal_interaction_validates_confirmation_and_reads_text_once() -> None:
    input_stream = StringIO("\nmaybe\ny\n  tools/new  \n")
    output_stream = StringIO()
    error_stream = StringIO()
    interaction = TerminalInteraction(
        input_stream=input_stream,
        output_stream=output_stream,
        error_stream=error_stream,
        use_color=False,
    )

    assert interaction.confirm(ConfirmationRequest(prompt="Create package? [y/n] ")) is True
    assert interaction.read_text(TextInputRequest(prompt="New package ID: ")) == "tools/new"
    assert output_stream.getvalue() == (
        "Create package? [y/n] "
        "Create package? [y/n] "
        "Create package? [y/n] "
        "New package ID: "
    )
    assert error_stream.getvalue() == (
        "invalid confirmation: enter 'y' or 'n'\n"
        "invalid confirmation: enter 'y' or 'n'\n"
    )


def test_terminal_interaction_uses_fzf_for_menu_taller_than_terminal() -> None:
    runtime = MemoryCommandRuntime([CommandResult(exit_code=0, stdout=b"2 sandbox/host/linux-meta [group]\n")])
    interaction = TerminalInteraction(
        command_runtime=runtime,
        fzf_available=lambda: True,
        terminal_lines=lambda: 7,
    )
    request = ChoiceRequest(
        header_text="Select a package:",
        options=(
            ChoiceOption(
                value="sunshine",
                label="sandbox/sunshine [package]",
                display_fields=("sandbox/sunshine", "[package]"),
            ),
            ChoiceOption(
                value="linux-meta",
                label="sandbox/host/linux-meta [group]",
                display_fields=("sandbox/host/linux-meta", "[group]"),
            ),
        ),
    )

    assert interaction.choose(request) == "linux-meta"

    command_request = runtime.requests[0]
    assert isinstance(command_request.command, ArgvCommand)
    assert command_request.command.arguments == (
        "fzf",
        "--prompt=Select> ",
        "--header=Select a package:",
        "--ansi",
        "--wrap",
        "--with-nth=2..",
        "--no-sort",
    )
    assert command_request.input == (
        b"1 sandbox/sunshine [package]\n"
        b"2 sandbox/host/linux-meta [group]\n"
    )
    assert command_request.isolate_process_group is False


def test_terminal_interaction_falls_back_to_prompt_for_menu_that_fits_terminal() -> None:
    input_stream = StringIO("\n")
    interaction = TerminalInteraction(
        input_stream=input_stream,
        output_stream=StringIO(),
        error_stream=StringIO(),
        command_runtime=MemoryCommandRuntime(),
        fzf_available=lambda: True,
        terminal_lines=lambda: 24,
        use_color=False,
    )

    selected = interaction.choose(
        ChoiceRequest(
            header_text="Select a repo:",
            options=(
                ChoiceOption(value="alpha", label="alpha"),
                ChoiceOption(value="beta", label="beta"),
            ),
        )
    )

    assert selected == "alpha"


def test_terminal_interaction_applies_confirmation_default() -> None:
    interaction = TerminalInteraction(
        input_stream=StringIO("\n"),
        output_stream=StringIO(),
        error_stream=StringIO(),
    )

    assert interaction.confirm(ConfirmationRequest(prompt="Continue? [Y/n] ", default=True)) is True


def test_terminal_interaction_writes_confirmation_message_before_prompting() -> None:
    input_stream = StringIO("y\n")
    output_stream = StringIO()
    interaction = TerminalInteraction(
        input_stream=input_stream,
        output_stream=output_stream,
        error_stream=StringIO(),
    )

    assert interaction.confirm(
        ConfirmationRequest(
            prompt="Continue? [y/n] ",
            message="Replacement summary\n",
        )
    ) is True
    assert output_stream.getvalue() == "Replacement summary\nContinue? [y/n] "


def test_scripted_interaction_fails_fast_when_queued_choice_is_not_an_option() -> None:
    interaction = ScriptedInteraction(choices=["missing"])
    request = ChoiceRequest(
        header_text="Select a repo:",
        options=(ChoiceOption(value="alpha", label="alpha"),),
    )

    with pytest.raises(AssertionError, match="scripted choice is not present"):
        interaction.choose(request)
