from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from typing import Iterator, Sequence, TextIO

try:
    import termios
except ImportError:  # pragma: no cover - non-POSIX platforms do not expose termios.
    termios = None


type TerminalStateSnapshot = tuple[int, list[int | bytes]]
ESCAPE_INPUT = "\x1b"


# Lives here rather than in cli_style so dotman.cli can use it without loading
# dotman.models at startup. Callers pass the stream they actually write to.
def colors_enabled(stream: TextIO) -> bool:
    return stream.isatty() and os.environ.get("NO_COLOR") is None


# Foreground terminal commands (a sudo password prompt, a tty Editor) and
# background commands run on different threads. A background command's snapshot
# goes stale once a foreground command takes the terminal: restoring it would
# undo that command's modes, e.g. turn echo back on under a password prompt.
_terminal_ownership_lock = threading.Lock()
_foreground_owner_count = 0
# Bumped whenever a foreground command takes or releases the terminal.
_foreground_ownership_changes = 0


@contextmanager
def own_terminal(*, streams: Sequence[object] | None = None) -> Iterator[None]:
    """Hold the terminal for a foreground command; always put its modes back afterwards."""
    global _foreground_owner_count, _foreground_ownership_changes
    with _terminal_ownership_lock:
        snapshots = _capture_terminal_state(streams=streams)
        _foreground_owner_count += 1
        _foreground_ownership_changes += 1
    try:
        yield
    finally:
        with _terminal_ownership_lock:
            _restore_terminal_state(snapshots)
            _foreground_owner_count -= 1
            _foreground_ownership_changes += 1


@contextmanager
def preserve_terminal_state(*, streams: Sequence[object] | None = None) -> Iterator[None]:
    """Put back the modes of a background command's run unless a foreground command held the terminal meanwhile."""
    with _terminal_ownership_lock:
        snapshots = _capture_terminal_state(streams=streams)
        captured_without_owner = _foreground_owner_count == 0
        changes_at_capture = _foreground_ownership_changes
    try:
        yield
    finally:
        # Check and restore under one lock, so no foreground command can take
        # the terminal in between.
        with _terminal_ownership_lock:
            if captured_without_owner and _foreground_ownership_changes == changes_at_capture:
                _restore_terminal_state(snapshots)


def read_prompt_line(
    message: str,
    *,
    input_stream: object | None = None,
    output_stream: object | None = None,
    escape_result: str | None = None,
) -> str:
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream

    if _prompt_toolkit_supported(input_stream=input_stream, output_stream=output_stream):
        try:
            return _prompt_with_toolkit(message, escape_result=escape_result).strip()
        except EOFError:
            return ""

    output_stream.write(message)
    output_stream.flush()
    answer = input_stream.readline()
    return answer.strip()


def _capture_terminal_state(*, streams: Sequence[object] | None = None) -> list[TerminalStateSnapshot]:
    if termios is None:
        return []

    snapshots: list[TerminalStateSnapshot] = []
    seen_fds: set[int] = set()
    for stream in streams or (sys.stdin, sys.stdout, sys.stderr):
        file_descriptor = _tty_file_descriptor(stream)
        if file_descriptor is None or file_descriptor in seen_fds:
            continue
        seen_fds.add(file_descriptor)
        try:
            snapshots.append((file_descriptor, termios.tcgetattr(file_descriptor)))
        except (OSError, termios.error, ValueError):
            continue
    return snapshots


def _restore_terminal_state(snapshots: Sequence[TerminalStateSnapshot]) -> None:
    if termios is None:
        return

    for file_descriptor, attributes in snapshots:
        try:
            # Interactive helpers sometimes exit on SIGINT before restoring the
            # tty they switched into raw mode. Put the caller's terminal back so
            # the next prompt does not echo Enter as `^M`.
            termios.tcsetattr(file_descriptor, termios.TCSADRAIN, attributes)
        except (OSError, termios.error, ValueError):
            continue


def _tty_file_descriptor(stream: object) -> int | None:
    try:
        is_tty = bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return None
    if not is_tty:
        return None
    try:
        return int(stream.fileno())
    except (AttributeError, OSError, ValueError, TypeError):
        return None


def _prompt_toolkit_supported(*, input_stream: object, output_stream: object) -> bool:
    return input_stream is sys.stdin and output_stream is sys.stdout and _tty_file_descriptor(input_stream) is not None and _tty_file_descriptor(output_stream) is not None


def _prompt_with_toolkit(message: str, *, escape_result: str | None = None) -> str:
    # Imported on first prompt: prompt_toolkit costs ~30ms at startup, and
    # most commands never prompt.
    from prompt_toolkit import prompt as prompt_toolkit_prompt
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings

    prompt_message: str | ANSI
    if "\x1b[" in message:
        prompt_message = ANSI(message)
    else:
        prompt_message = message
    if escape_result is None:
        return prompt_toolkit_prompt(prompt_message)

    key_bindings = KeyBindings()

    # Escape starts many terminal key sequences; eager handling makes a lone press abort immediately.
    @key_bindings.add("escape", eager=True)
    def exit_on_escape(event) -> None:
        event.app.exit(result=escape_result)

    return prompt_toolkit_prompt(prompt_message, key_bindings=key_bindings)


def emit_interrupt_notice() -> None:
    sys.stderr.write("\ninterrupted\n")
