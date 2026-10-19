from __future__ import annotations

import os
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dotman import cli_style
from dotman.command_runtime import (
    ArgvCommand,
    CommandRequest,
    current_command_runtime,
    raise_for_command_interruption,
)
from dotman.models import AdditionalSource, EditorSpec
from dotman.printable import terminal_safe_line
from dotman.terminal import colors_enabled
from dotman.ui_context import current_ui_config


DEFAULT_REVIEW_PAGER = "less -FRX"
DEFAULT_COMPACT_PATH_TAIL_SEGMENTS = 2
REVIEW_PATH_HEAD_SEGMENTS = 1
ReviewBytesLoader = Callable[[], bytes]


@dataclass(frozen=True)
class ReviewItem:
    # Restore items take identity from snapshot provenance, which may be missing.
    selection_label: str | None
    package_id: str | None
    target_name: str | None
    action: str
    operation: str
    repo_path: Path
    live_path: Path
    source_path: str
    destination_path: str
    before_bytes: bytes | None = field(default=None, repr=False)
    after_bytes: bytes | None = field(default=None, repr=False)
    before_bytes_loader: ReviewBytesLoader | None = field(default=None, repr=False, compare=False)
    after_bytes_loader: ReviewBytesLoader | None = field(default=None, repr=False, compare=False)
    before_mode: int | None = None
    after_mode: int | None = None
    editor: EditorSpec | None = None
    editor_explicit: bool = False
    additional_sources: tuple[str, ...] = ()
    additional_source_entries: tuple[AdditionalSource, ...] = field(default=(), repr=False, compare=False)
    additional_sources_root: Path | None = field(default=None, repr=False, compare=False)
    command_cwd: Path | None = None
    command_env: dict[str, str] | None = field(default=None, repr=False)
    diff_unavailable_reason: str | None = None
    bound_profile: str | None = None
    is_probe: bool = False
    probe_command: str | None = field(default=None, repr=False)
    hook_command_summaries: tuple[str, ...] = ()


def _render_hook_command_summary(summary: str) -> str:
    if ": " not in summary:
        return summary
    hook_name, command = summary.split(": ", 1)
    hook_badge = cli_style.render_menu_badge(f"[{hook_name}]", use_color=colors_enabled(sys.stdout))
    return f"{hook_badge} {command}"


def run_review_item_diff(review_item: ReviewItem) -> None:
    if review_item.is_probe:
        for summary in review_item.hook_command_summaries:
            print(_render_hook_command_summary(summary))
        return
    if review_item.diff_unavailable_reason is not None:
        raise ValueError(review_item.diff_unavailable_reason)
    before_bytes = _review_item_bytes(review_item, before=True)
    after_bytes = _review_item_bytes(review_item, before=False)

    with tempfile.TemporaryDirectory(prefix="dotman-diff-") as temp_dir:
        temp_root = Path(temp_dir)
        left_side, right_side = _review_diff_side_names(operation=review_item.operation)
        before_mode, after_mode = _review_diff_file_modes(review_item)
        left_path = _write_review_file(
            root=temp_root,
            side=left_side,
            reference_path=review_item.live_path,
            content=before_bytes,
            mode=before_mode or 0o644,
        )
        right_path = _write_review_file(
            root=temp_root,
            side=right_side,
            reference_path=review_item.repo_path,
            content=after_bytes,
            mode=after_mode or 0o644,
        )
        try:
            pager_command = _select_review_pager_command() if sys.stdout.isatty() else None
            diff_command = _build_review_diff_command(
                root=temp_root,
                left_path=left_path,
                right_path=right_path,
                paginate=pager_command is not None,
            )
            diff_env: dict[str, str] = {}
            if pager_command is not None:
                diff_env["GIT_PAGER"] = pager_command
            result = current_command_runtime().run(
                CommandRequest(
                    command=ArgvCommand(tuple(diff_command)),
                    env=diff_env,
                    cwd=temp_root,
                    io="tty",
                )
            )
        except FileNotFoundError as exc:
            raise ValueError("git is required for diff review") from exc
    raise_for_command_interruption(result)
    if result.exit_code not in {0, 1}:
        raise ValueError("git diff failed during review")


def _review_item_bytes(review_item: ReviewItem, *, before: bool) -> bytes:
    bytes_value = review_item.before_bytes if before else review_item.after_bytes
    if bytes_value is not None:
        return bytes_value
    loader = review_item.before_bytes_loader if before else review_item.after_bytes_loader
    if loader is not None:
        return loader()
    raise ValueError("diff preview is unavailable")


def _review_diff_file_modes(review_item: ReviewItem) -> tuple[int | None, int | None]:
    if review_item.before_mode is None or review_item.after_mode is None:
        return None, None
    if review_item.before_mode == review_item.after_mode:
        return None, None
    return review_item.before_mode, review_item.after_mode


def _review_diff_side_names(*, operation: str) -> tuple[str, str]:
    if operation == "restore":
        return "live", "snapshot"
    return "live", "repo"


def _write_review_file(*, root: Path, side: str, reference_path: Path, content: bytes, mode: int = 0o444) -> Path:
    # Review temp files must stay under the temp root even when the display path
    # keeps an absolute slash for system files.
    output_path = root / side / _review_materialized_display_path(reference_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    output_path.chmod(mode)
    return output_path


def _review_materialized_display_path(reference_path: Path) -> Path:
    display_path = _review_display_path(reference_path)
    if display_path.is_absolute():
        relative_parts = display_path.parts[1:]
        return Path(*relative_parts) if relative_parts else Path("content")
    return display_path


def display_review_path(reference_path: Path | str, *, compact: bool = True, tail_segments: int | None = None) -> str:
    """A path for a person to read, in a row or a heading. A live path is outside text and can hold unprintable characters, so it is escaped."""
    path = Path(reference_path)
    if not compact:
        return terminal_safe_line(str(path))
    return terminal_safe_line(str(_review_display_path(path, tail_segments=tail_segments)))


def _review_display_path(reference_path: Path, *, tail_segments: int | None = None) -> Path:
    normalized_path = _normalize_review_display_path(reference_path)
    return _compact_review_display_path(normalized_path, tail_segments=tail_segments)


def _normalize_review_display_path(reference_path: Path) -> Path:
    # Keep diff labels readable and machine-independent by collapsing the
    # current home directory to `~` instead of embedding an absolute prefix.
    try:
        home_relative_path = reference_path.relative_to(Path.home())
    except ValueError:
        if reference_path.is_absolute():
            return reference_path
        return reference_path if reference_path.parts else Path("content")
    return Path("~") / home_relative_path if home_relative_path.parts else Path("~")


def _compact_review_display_path(display_path: Path, *, tail_segments: int | None = None) -> Path:
    parts = display_path.parts
    if not parts:
        return Path("content")
    resolved_tail_segments = _effective_compact_path_tail_segments(tail_segments)
    if display_path.is_absolute():
        head_parts = parts[: 1 + REVIEW_PATH_HEAD_SEGMENTS]
        if len(parts) <= len(head_parts) + resolved_tail_segments:
            return display_path
        # Absolute paths keep their root slash so system files stay clearly
        # distinguished from repo-relative paths in review and selection menus.
        return Path(*head_parts, "...", *parts[-resolved_tail_segments:])
    head_parts = parts[:REVIEW_PATH_HEAD_SEGMENTS]
    if len(parts) <= len(head_parts) + resolved_tail_segments:
        return display_path
    # Keep the anchor and tail so long review labels stay short without
    # dropping the most useful disambiguating path context.
    return Path(*head_parts, "...", *parts[-resolved_tail_segments:])


def _effective_compact_path_tail_segments(tail_segments: int | None) -> int:
    configured_tail_segments = tail_segments
    if configured_tail_segments is None:
        ui_config = current_ui_config()
        configured_tail_segments = (
            ui_config.compact_path_tail_segments
            if ui_config is not None
            else DEFAULT_COMPACT_PATH_TAIL_SEGMENTS
        )
    if not isinstance(configured_tail_segments, int) or isinstance(configured_tail_segments, bool) or configured_tail_segments < 1:
        raise ValueError("compact path tail segments must be an integer >= 1")
    return configured_tail_segments


def _build_review_diff_command(*, root: Path, left_path: Path, right_path: Path, paginate: bool) -> list[str]:
    command = ["git"]
    if paginate:
        command.append("--paginate")
    command.extend(
        [
            "diff",
            "--no-index",
            "--color=auto",
            "--",
            str(left_path.relative_to(root)),
            str(right_path.relative_to(root)),
        ]
    )
    return command


def _select_review_pager_command() -> str | None:
    git_pager = os.environ.get("GIT_PAGER")
    if git_pager is not None:
        return None if _pager_command_is_disabled(git_pager) else git_pager

    configured_pager = _git_configured_pager_command()
    if configured_pager is not None:
        return None if _pager_command_is_disabled(configured_pager) else configured_pager

    pager = os.environ.get("PAGER")
    if pager is not None:
        return None if _pager_command_is_disabled(pager) else pager

    return DEFAULT_REVIEW_PAGER if shutil.which("less") is not None else None


def _git_configured_pager_command() -> str | None:
    for key in ("pager.diff", "core.pager"):
        try:
            result = current_command_runtime().run(
                CommandRequest(
                    command=ArgvCommand(("git", "config", "--get", key)),
                )
            )
        except FileNotFoundError:
            return None
        raise_for_command_interruption(result)
        if result.exit_code == 0:
            pager = result.stdout_text.strip()
            if pager:
                return pager
    return None


def _pager_command_is_disabled(command: str) -> bool:
    try:
        command_parts = shlex.split(command)
    except ValueError:
        return False
    if not command_parts:
        return False
    return Path(command_parts[0]).name == "cat"
