"""Persistent initial Command Deck over public immutable SyncSession views."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from contextlib import ExitStack
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from difflib import unified_diff
from itertools import groupby
import re
import signal
import time

from rich.console import Group
from rich.padding import Padding
from rich.rule import Rule
from rich.style import Style
from rich.spinner import Spinner
from rich.table import Table

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.errors import NoWidget
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

from dotman.diff_review import display_review_path
from dotman.cli_style import MENU_HEADER_MARKER, MENU_HEADER_MARKER_STYLE, MENU_HINT_STYLE, MENU_PROMPT_STYLE, render_annotation_parentheses, render_conflict_lines, render_diff_line, render_info_section_header, render_key_hint, render_key_hint_chip, render_key_hint_separator, render_payload_action, render_payload_section_label, render_sync_term, render_package_label, style_text, SYNC_TERM_STYLE_BY_NAME
from dotman.sync_base_store import DirectoryChildPresent, FilePresent, Missing
from dotman.sync_deck_command import selection_uses_inclusion, auxiliary_resolution, additional_label, guard_skip_explanation, guard_skip_label, set_all_selected, set_selected, row_diagnostics, auxiliary_label, review, edit_proposal, set_resolution_intent, retry_materialization, effect_summary, primary_change_summary, render_guess_annotation, render_resolution, resolution_label, live_counts, summary_stats
from dotman.sync_session import AuthorizeSymlinkReplacement, AdditionalRow, AuxiliaryRow, CommandRejected, CommitOption, SessionRow, SetCommit, SyncSession, conflict_diagnostic


@dataclass(frozen=True)
class ReviewFact:
    label: str
    value: str


@dataclass(frozen=True)
class ReviewNote:
    text: str


@dataclass(frozen=True)
class ReviewDiff:
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ReviewConflict:
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ReviewSection:
    title: str
    items: tuple[ReviewFact | ReviewNote | ReviewDiff | ReviewConflict, ...]


REVIEW_ITEM_INDENT = 4
# Invisible style metadata marking where each change block starts, for Full View navigation.
REVIEW_CHANGE_KEY = "review_change"
REVIEW_CHANGE_META = {REVIEW_CHANGE_KEY: True}
# Invisible style metadata numbering each search match, so a match wrapped over two rows counts once.
REVIEW_MATCH_KEY = "review_match"
# Invisible style metadata naming the App action a clicked help chip runs.
HINT_ACTION_KEY = "hint_action"


@dataclass(frozen=True)
class HintChip:
    """A help hint that a click runs like its key, for confirmation without the keyboard."""
    key: str
    label: str
    action: str


def render_hint(hint: tuple[str, str] | HintChip, *, use_color: bool, hovered_action: str | None) -> Text:
    if isinstance(hint, tuple):
        return Text.from_ansi(render_key_hint(*hint, use_color=use_color))
    chip = Text.from_ansi(render_key_hint_chip(hint.key, hint.label, use_color=use_color,
                                               hovered=hint.action == hovered_action))
    chip.stylize(Style(meta={HINT_ACTION_KEY: hint.action}))
    return chip


def render_hint_line(hints: list[tuple[str, str] | HintChip], *, use_color: bool,
                     hovered_action: str | None) -> Text:
    separator = Text.from_ansi(render_key_hint_separator(use_color=use_color))
    return separator.join(render_hint(hint, use_color=use_color, hovered_action=hovered_action) for hint in hints)


class SearchMarks:
    """Reverse-styles case-insensitive matches of one query, numbered in document order."""

    def __init__(self, query: str) -> None:
        self.pattern = re.compile(re.escape(query), re.IGNORECASE) if query else None
        self.count = 0

    def __call__(self, text: Text) -> Text:
        if self.pattern is not None:
            for match in self.pattern.finditer(text.plain):
                text.stylize("reverse", *match.span())
                text.apply_meta({REVIEW_MATCH_KEY: self.count}, *match.span())
                self.count += 1
        return text


@dataclass(frozen=True)
class ReviewDocument:
    """One review, laid out as styled wrapping renderables or as copyable text.

    On screen the Deck title already names the review, so the body opens with
    the subject alone; copied text keeps the full heading for context.
    """

    heading: str
    subject: str
    sections: tuple[ReviewSection, ...]
    use_color: bool

    def text(self) -> str:
        indent = " " * REVIEW_ITEM_INDENT
        lines = [f"{self.heading} — {self.subject}"]
        for section in self.sections:
            lines += ["", render_info_section_header(section.title, use_color=self.use_color)]
            for item in section.items:
                if isinstance(item, ReviewFact):
                    lines.append(f"{indent}{render_payload_section_label(item.label + ':', use_color=self.use_color)} {item.value}")
                elif isinstance(item, ReviewNote):
                    lines.append(f"{indent}{item.text}")
                elif isinstance(item, ReviewConflict):
                    lines.extend(indent + line for line in render_conflict_lines(item.lines, use_color=self.use_color))
                else:
                    lines.extend(indent + "".join(render_diff_line(line, use_color=self.use_color)) for line in item.lines)
        return "\n".join(lines)

    def renderable(self, search: str = "") -> Group:
        mark = SearchMarks(search)
        parts: list = [mark(Text.from_ansi(self.subject, overflow="fold"))]
        for section in self.sections:
            parts += [Text(), self._section_rule(section.title, mark)]
            # Consecutive facts share one grid so their values align.
            for kind, items in groupby(section.items, type):
                items = list(items)
                if kind is ReviewFact:
                    body = fact_grid([(render_payload_section_label(item.label + ":", use_color=self.use_color), item.value)
                                      for item in items], mark=mark)
                elif kind is ReviewNote:
                    body = Group(*(mark(Text.from_ansi(item.text, overflow="fold")) for item in items))
                elif kind is ReviewConflict:
                    body = Group(*(self._conflict_line(line, mark) for item in items
                                   for line in render_conflict_lines(item.lines, use_color=self.use_color)))
                else:
                    body = Group(*(self._diff_grid(item.lines, mark) for item in items))
                parts.append(Padding(body, (0, 0, 0, REVIEW_ITEM_INDENT)))
        return Group(*parts)

    def _section_rule(self, title: str, mark: SearchMarks) -> Rule:
        # On screen a full-width rule separates sections without costing an extra row.
        lead = style_text("──", *MENU_HEADER_MARKER_STYLE) if self.use_color else "──"
        heading = style_text(title, "1") if self.use_color else title
        return Rule(mark(Text.from_ansi(f"{lead} {heading}")), align="left", style="dim" if self.use_color else "")

    @staticmethod
    def _conflict_line(line: str, mark: SearchMarks) -> Text:
        text = mark(Text.from_ansi(line, overflow="fold"))
        if text.plain.startswith("<<<<<<<"):
            text.apply_meta(REVIEW_CHANGE_META)
        return text

    def _diff_grid(self, lines: tuple[str, ...], mark: SearchMarks) -> Table:
        # A marker column keeps wrapped content hanging right of the +/- sign.
        grid = Table.grid()
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(ratio=1, overflow="fold")
        previous_changed = False
        for line in lines:
            marker, content = render_diff_line(line, use_color=self.use_color)
            marker_text = Text.from_ansi(marker)
            changed = line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            # Tag only a block's first line, so change navigation stops once per block.
            if changed and not previous_changed:
                marker_text.apply_meta(REVIEW_CHANGE_META)
            previous_changed = changed
            grid.add_row(marker_text, mark(Text.from_ansi(content)))
        return grid


# Matches unified_diff's default hunk context; Full View widens both to the whole file.
REVIEW_CONTEXT_LINES = 3
CONFLICT_ELISION = "⋯"


def conflict_excerpt(content: bytes, *, description: str, full_context: bool = False) -> ReviewConflict | ReviewNote:
    """zdiff3 conflict blocks with surrounding context, like unified-diff hunks."""
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return ReviewNote(f"Binary {description}: {len(content)} bytes")
    in_block, block_lines = False, []
    for index, line in enumerate(lines):
        if line.startswith("<<<<<<<"):
            in_block = True
        if in_block:
            block_lines.append(index)
        if line.startswith(">>>>>>>"):
            in_block = False
    context = len(lines) if full_context else REVIEW_CONTEXT_LINES
    kept = sorted({index for block_index in block_lines
                   for index in range(block_index - context, block_index + context + 1)
                   if 0 <= index < len(lines)})
    excerpt = []
    for previous, index in zip([-1, *kept], kept):
        if index - previous > 1:
            excerpt.append(CONFLICT_ELISION)
        excerpt.append(lines[index])
    if kept and kept[-1] < len(lines) - 1:
        excerpt.append(CONFLICT_ELISION)
    return ReviewConflict(tuple(excerpt))


def _review_difference(
    before: FilePresent | DirectoryChildPresent | Missing | None,
    after: FilePresent | DirectoryChildPresent | Missing | None,
    *,
    before_label: str,
    after_label: str,
    description: str,
    use_color: bool,
    full_context: bool = False,
) -> list[ReviewNote | ReviewDiff]:
    if before is None or after is None:
        return [ReviewNote("Comparison evidence unavailable")]
    present_types = (FilePresent, DirectoryChildPresent)
    before_bytes = before.content if isinstance(before, present_types) else b""
    after_bytes = after.content if isinstance(after, present_types) else b""
    mode_changed = (
        isinstance(before, DirectoryChildPresent)
        and isinstance(after, DirectoryChildPresent)
        and before.executable != after.executable
    )
    # Missing compares as empty bytes, so existence changes need their own Git-style
    # header; otherwise creating or deleting an empty file reads as no difference.
    existence_change = (
        ["new file"] if isinstance(before, Missing) and not isinstance(after, Missing)
        else ["deleted file"] if isinstance(after, Missing) and not isinstance(before, Missing)
        else []
    )
    if before_bytes == after_bytes and not mode_changed and not existence_change:
        return [ReviewNote(render_payload_section_label("No content difference", use_color=use_color))]

    lines: list[str] = existence_change
    if mode_changed:
        # Directory children carry Git's executable bit, so use the same mode
        # evidence as Git diffs instead of presenting host-specific permissions.
        lines.extend([
            f"old mode {'100755' if before.executable else '100644'}",
            f"new mode {'100755' if after.executable else '100644'}",
        ])
    if before_bytes != after_bytes:
        try:
            before_lines = before_bytes.decode("utf-8").splitlines(keepends=True)
            after_lines = after_bytes.decode("utf-8").splitlines(keepends=True)
            diff = unified_diff(
                before_lines, after_lines,
                fromfile="/dev/null" if isinstance(before, Missing) else before_label,
                tofile="/dev/null" if isinstance(after, Missing) else after_label,
                n=max(len(before_lines), len(after_lines)) if full_context else REVIEW_CONTEXT_LINES,
                lineterm="\n",
            )
            for line in diff:
                lines.append(line.rstrip("\n"))
                # Retain newline-only drift in both evidence and effect previews.
                if not line.endswith("\n"):
                    lines.append("\\ No newline at end of file")
        except UnicodeDecodeError:
            binary = ReviewNote(f"Binary {description}: {len(before_bytes)} → {len(after_bytes)} bytes")
            return [ReviewDiff(tuple(lines)), binary] if lines else [binary]
    return [ReviewDiff(tuple(lines))]




NOOP_NOTICE = "Nothing to do: Approval would neither write nor record a Sync Base"


class CommandDeck:
    def __init__(self, session: SyncSession, *, use_color: bool) -> None:
        self.session = session
        self.use_color = use_color
        self.focus = 0
        self.reviewing = False
        self.confirming = False
        # Title of the review section shown whole in Full View, over the unchanged review.
        self.full_view: str | None = None
        self.notice = ""
        # Case-insensitive Target substring; focus indexes the rows it leaves visible.
        self.filter = ""

    @property
    def visible_rows(self) -> tuple:
        rows = self.session.view.rows
        if not self.filter:
            return rows
        query = self.filter.casefold()
        return tuple(row for row in rows
                     if query in Text.from_ansi(workset_target_label(row, use_color=False)).plain.casefold())

    @property
    def hidden_selected_count(self) -> int:
        visible = {row.row_id for row in self.visible_rows}
        return sum(row_selected(row) for row in self.session.view.rows if row.row_id not in visible)

    @property
    def focused_row(self):
        rows = self.visible_rows
        return rows[self.focus] if rows else None

    def set_filter(self, query: str) -> None:
        """Keep the focused row focused when it stays visible; otherwise start at the top."""
        focused = self.focused_row
        self.filter = query
        row_ids = [row.row_id for row in self.visible_rows]
        self.focus = row_ids.index(focused.row_id) if focused and focused.row_id in row_ids else 0

    def back(self) -> bool:
        """Return False when already at the workset, where Escape has nothing to leave."""
        if self.confirming:
            self.confirming = False
        elif self.full_view is not None:
            self.full_view = None
        elif self.reviewing:
            self.reviewing = False
        else:
            return False
        return True

    def confirm(self) -> None:
        if self.reviewing or self.confirming:
            return
        view = self.session.view
        command = "preview" if view.preview else "execute"
        # Command availability is not proof that the reviewed Approval set is ready.
        invalid_approved = any(
            row.included and row.approved
            and (row.proposal is None or any(item.severity == "error" for item in row_diagnostics(row)))
            for row in view.rows
            if not isinstance(row, (AuxiliaryRow, AdditionalRow))
        )
        if view.topology_diagnostics:
            self.notice = view.topology_diagnostics[0].message
            return
        if command not in view.allowed_commands or invalid_approved:
            self.notice = "Selected Proposals must be ready before confirmation."
            return
        self.confirming = True

    def select(self, selected: bool | None = None) -> None:
        row = self.focused_row
        if row is None or self.confirming:
            return
        current = row.included if selection_uses_inclusion(row) else row.approved
        result = set_selected(self.session, row, not current if selected is None else selected)
        # Selecting may materialize the Proposal and only then reveal it as a no-op.
        focused = self.focused_row
        if isinstance(focused, SessionRow) and focused.proposal is not None and focused.proposal.noop:
            self.notice = NOOP_NOTICE
        else:
            self.notice = result.reason if isinstance(result, CommandRejected) else ""

    def select_all(self, selected: bool) -> None:
        if self.reviewing or self.confirming:
            return
        result = set_all_selected(self.session, selected, tuple(row.row_id for row in self.visible_rows))
        self.notice = result.reason if isinstance(result, CommandRejected) else ""

    def open_review(self) -> None:
        row = self.focused_row
        if row is None or self.confirming or self.reviewing:
            return
        if isinstance(row, AuxiliaryRow):
            self.notice = "Auxiliary work has no Proposal to review."
            return
        result = review(self.session, row.row_id)
        if isinstance(result, CommandRejected):
            self.notice = result.reason
        else:
            self.reviewing = True

    def edit(self) -> None:
        row = self.focused_row
        if row is None or self.confirming or "edit-proposal" not in row.allowed_commands:
            return
        result = edit_proposal(self.session, row.row_id)
        if isinstance(result, CommandRejected):
            self.notice = result.reason
        elif result.result.status == "cancelled":
            self.notice = "Editor cancelled; previous Proposal preserved."
        elif result.result.status == "unchanged":
            self.notice = "Editor closed without changes; Resolution kept."
        else:
            self.notice = "\n".join(item.message for item in result.result.diagnostics)

    def commit_choices(self) -> tuple[tuple[CommitOption, str], ...]:
        """Repos the selection writes, with the message Commit Work would use; preview never commits."""
        if self.session.view.preview:
            return ()
        return tuple((option, message) for option in self.session.view.commit_options
                     if (message := self.session.commit_message(option.repo)) is not None)

    def set_commit(self, repo: str, selected: bool) -> None:
        view = self.session.view
        result = self.session.dispatch(SetCommit(view.session_id, view.revision, repo, selected))
        self.notice = result.reason if isinstance(result, CommandRejected) else ""

    def toggle_all_commits(self) -> None:
        """Everything on unless everything already is."""
        choices = self.commit_choices()
        selected = not all(option.selected for option, _ in choices)
        for option, _ in choices:
            self.set_commit(option.repo, selected)

    def confirmation_text(self) -> str:
        selected = [row for row in self.session.view.rows if not isinstance(row, (AuxiliaryRow, AdditionalRow)) and row.approved]
        auxiliary_count = sum(row.included for row in self.session.view.rows if isinstance(row, AuxiliaryRow))
        live = live_counts([effect.kind for effect in row.proposal.publication_effects]
                           for row in selected if row.proposal)
        repository_changes = sum(row.proposal.primary_source_change is not None for row in selected if row.proposal)
        additional_count = sum(row.approved for row in self.session.view.rows if isinstance(row, AdditionalRow))
        repository_changes += additional_count
        verb = "Preview" if self.session.view.preview else "Execute"
        stats = summary_stats(
            (("approved", len(selected)), ("additional", additional_count),
             ("auxiliary", auxiliary_count), ("repos", repository_changes)),
            live=live, use_color=self.use_color,
        )
        if selected or additional_count or auxiliary_count:
            return f":: {verb}? — {stats}"
        # An empty set still confirms ("nothing to do" is a valid decision), but
        # warn so an accidental confirm is not read as a clean state.
        pending = sum(isinstance(row, SessionRow) and row.kind == "drift" and row.included
                      for row in self.session.view.rows)
        warning = (f"Nothing selected; {pending} drifted Sync Unit{'s' if pending != 1 else ''} "
                   f"stay{'' if pending != 1 else 's'} pending.")
        styled = style_text(warning, *SYNC_TERM_STYLE_BY_NAME["warning"]) if self.use_color else warning
        return f":: {verb}? — {stats}\n{styled}"

    def review_text(self) -> str:
        document = self.displayed_document()
        return document.text() if document else ""

    def full_view_titles(self) -> list[str]:
        """Review sections with diff or conflict evidence that Full View can widen."""
        document = self.review_document()
        return [section.title for section in document.sections
                if any(isinstance(item, (ReviewDiff, ReviewConflict)) for item in section.items)]

    def open_full_view(self, title: str) -> None:
        if self.reviewing and title in self.full_view_titles():
            self.full_view = title

    def displayed_document(self) -> ReviewDocument | None:
        if self.full_view is None:
            return self.review_document()
        document = self.review_document(full_context=True)
        [section] = [section for section in document.sections if section.title == self.full_view]
        return replace(document, heading=self.review_heading("Full View"), sections=(section,))

    def review_heading(self, title: str) -> str:
        if not self.use_color:
            return f"{MENU_HEADER_MARKER} {title}"
        return f"{style_text(MENU_HEADER_MARKER, *MENU_HEADER_MARKER_STYLE)} {style_text(title, '1')}"

    def review_document(self, *, full_context: bool = False) -> ReviewDocument | None:
        row = self.focused_row
        if row is None or isinstance(row, AuxiliaryRow):
            return None
        color = self.use_color
        term = lambda value: render_sync_term(value, use_color=color)
        difference = lambda *args, **kwargs: _review_difference(*args, **kwargs, use_color=color,
                                                                full_context=full_context)
        approval = ReviewFact("Approval", term(review_approval_term(row)))
        if isinstance(row, AdditionalRow):
            return ReviewDocument(
                heading=self.review_heading("Additional Source Review"),
                subject=additional_label(row, use_color=color),
                sections=(
                    ReviewSection("Decision", (approval, ReviewFact("References", ", ".join(row.references)))),
                    ReviewSection("Source Change", tuple(difference(
                        FilePresent(row.change.before), FilePresent(row.change.candidate),
                        before_label="Additional Source",
                        after_label="candidate Additional Source",
                        description="Additional Source",
                    ))),
                ),
                use_color=color,
            )
        proposal = row.proposal
        intent = row.intent
        observation = row.observation
        base = observation.base
        pull = self.session.view.operation == "pull" or intent in ("use-live", "merge")
        capture_required = pull and not (proposal and proposal.intent == "editor")
        # Full paths: the review wraps them, so compaction would only hide identity.
        display_path = lambda path: display_review_path(path, compact=False)
        primary = primary_change_summary(proposal, observation.repository_path)
        presence = lambda state: "missing" if isinstance(state, Missing) else "present"

        decision = [
            approval,
            ReviewFact("Resolution", render_row_resolution(row, use_color=color)
                       + render_guess_annotation(resolution_guess_shown(row), use_color=color)
                       if intent or self.session.view.operation == "pull" else term("blocked")),
            ReviewFact("Policy", observation.effective_policy),
        ]
        if observation.configured_policy != observation.effective_policy:
            decision.append(ReviewFact("Configured policy", observation.configured_policy))
        if "authorize-symlink-replacement" in row.allowed_commands:
            link = "Link replacement authorized" if row.symlink_authorized else "Link replacement requires authorization"
            decision.append(ReviewNote(f"{term(link)} (L)"))
        decision.extend(ReviewNote(f"{term(item.severity)}: {item.message}") for item in row_diagnostics(row))
        sections = [
            ReviewSection("Decision", tuple(decision)),
            ReviewSection("Paths", (
                ReviewFact("Repository path", display_path(observation.repository_path)),
                ReviewFact("Live path", display_path(observation.live_path)),
            )),
        ]

        primary_value = "none"
        if primary:
            annotation = render_annotation_parentheses("authorized by Proposal Approval", use_color=color)
            primary_value = f"{primary['kind']} · {display_path(primary['path'])}{annotation}"
        conflicted = conflict_diagnostic(row)
        captured = proposal.capture if proposal else conflicted.capture if conflicted else None
        capture = (presence(captured) if captured is not None
                   else term("pending") if capture_required else "not required")
        state = [
            ReviewFact("Observation", term(observation.state)),
            ReviewFact("Sync Base", term(base.status)),
        ]
        if base.reason:
            state.append(ReviewFact("Base reason", base.reason))
        if base.record:
            state.append(ReviewFact("Base payload", presence(base.record.payload)))
        state += [
            ReviewFact("Primary Source Change", primary_value),
            ReviewFact("Capture", capture),
            ReviewFact("Reconciliation", proposal.reconciliation if proposal
                       else term("conflict") if conflicted else term("pending")),
        ]
        if proposal and observation.configured_policy in ("pull-only", "both"):
            state.append(ReviewFact("Checkpoint qualified", "yes" if proposal.checkpoint_qualified else "no"))
        sections.append(ReviewSection("State", tuple(state)))
        conflict = conflicted.conflict if conflicted else None
        if isinstance(conflict, (FilePresent, DirectoryChildPresent)):
            sections.append(ReviewSection("Merge conflicts", (
                ReviewNote(render_payload_section_label("Resolve in the Editor (E); it opens with this merge output",
                                                        use_color=color)),
                conflict_excerpt(conflict.content, description="merge output", full_context=full_context),
            )))

        # Capture differs while the live bytes already match, so publication
        # runs push hooks to reapply live state (see materialize).
        reapply = proposal is not None and any(
            effect.kind == "reapply" for effect in proposal.publication_effects)
        if proposal is not None:
            effects = []
            for effect in proposal.publication_effects:
                summary = effect_summary(effect)
                detail = f"{render_payload_action(effect.kind, use_color=color)} {display_path(summary['path'])}"
                if "bytes" in summary:
                    detail += render_annotation_parentheses(f"{summary['bytes']} bytes", use_color=color)
                if "mode" in summary:
                    detail += f" → {summary['mode']}"
                if effect.kind == "reapply":
                    detail += render_annotation_parentheses("push-hooks", use_color=color)
                effects.append(ReviewNote(detail))
            if not effects:
                effects.append(ReviewNote(render_payload_section_label("none", use_color=color)))
            sections.append(ReviewSection("Publication Effects", tuple(effects)))

            repository_effect = pull or observation.effective_policy == "both" or proposal.intent == "editor"
            effect_preview = lambda side, before, after, notes=(): ReviewSection(
                f"{side.capitalize()} effect preview", (*notes, *difference(
                    before, after, before_label=side,
                    after_label=f"{side} outcome", description=f"{side} outcome",
                )))
            pull_only = observation.effective_policy == "pull-only"
            notes = (ReviewNote("Live remains unchanged"),) if pull_only else ()
            if repository_effect:
                sections.append(effect_preview("repository", observation.repository, proposal.repository, notes))
            if not pull_only:
                notes = (ReviewNote("Live file unchanged: Capture differs, so push hooks reapply live state"),
                         ) if reapply else ()
                sections.append(effect_preview("live", observation.live, proposal.live, notes))
        # Drift explains a drifted row only when no outcome preview shows a change,
        # e.g. Capture reproduces the repository while the compared copies differ,
        # or a hook reapply that leaves the live file unchanged.
        writes_nothing = proposal is None or (
            proposal.primary_source_change is None and not proposal.publication_effects)
        if (observation.effective_policy in ("both", "pull-only")
                and observation.state == "drifted" and (writes_nothing or reapply)):
            drift = [
                ReviewNote(NOOP_NOTICE if proposal.noop else "Nothing will be written; Approval records the Sync Base"),
                # A Capture differing from the outcome reapplies push hooks (see materialize), so
                # no effect means only compare sees drift, or no hook can apply it.
                ReviewNote("compare does not match Capture, so this unit keeps appearing; align them to stop it"),
            ] if proposal and writes_nothing else []
            drift += [ReviewFact("Repository comparison", observation.compare_repo),
                      ReviewFact("Live comparison", observation.compare_live)]
            drift.extend(difference(
                observation.comparison_repository, observation.comparison_live,
                before_label="compared repository",
                after_label="compared live", description="compared copies",
            ))
            sections.append(ReviewSection("Drift", tuple(drift)))
        additional = [item for item in self.session.view.rows
                      if isinstance(item, AdditionalRow) and row.row_id in item.references]
        if additional:
            sections.append(ReviewSection(
                "Additional Source Changes (independent Approval and Review)",
                tuple(ReviewNote(additional_label(item, use_color=color)) for item in additional),
            ))
        return ReviewDocument(
            heading=self.review_heading("Proposal Review"),
            subject=unit_label(row, use_color=color),
            sections=tuple(sections), use_color=color,
        )


def render_selection_marker(selected: bool, *, selectable: bool = True, term: str, use_color: bool) -> str:
    """The workset `[x]`/`[ ]`/`[-]` marker, colored like its Selection term."""
    marker = "[x]" if selected else "[ ]" if selectable else "[-]"
    return render_sync_term(term, use_color=use_color).replace(term, marker)


def commit_choice_label(option: CommitOption, message: str, *, use_color: bool,
                        cursor_style: Style | None = None) -> Table:
    """`[x] repo@branch` over its dimmed commit subject; the dotman repo name, as everywhere else in the Deck.

    Long lines wrap under the repo, keeping the marker column clear: the list has
    no detail panel to recover truncated text. Overlong words break mid-word.
    `cursor_style` marks the repo line the way the workset cursor marks a row.
    """
    marker = Text.from_ansi(render_selection_marker(
        option.selected, term="selected" if option.selected else "unselected", use_color=use_color))
    repo = style_text(option.repo, *MENU_PROMPT_STYLE) if use_color else option.repo
    branch = f"@{option.branch}" if option.branch else " (detached HEAD)"
    branch = style_text(branch, *MENU_HINT_STYLE) if use_color else branch
    identity = Text.from_ansi(f"{repo}{branch}")
    subject = message.splitlines()[0]
    subject = style_text(subject, *MENU_HINT_STYLE) if use_color else subject
    if cursor_style is not None:
        marker, identity = without_colors(marker), without_colors(identity)
    label = Table.grid(padding=(0, 1), expand=True)
    label.add_column(no_wrap=True)
    label.add_column(overflow="fold", ratio=1)
    label.add_row(marker, identity, style=cursor_style)
    label.add_row("", Text.from_ansi(subject))
    return label


def without_colors(text: Text) -> Text:
    """Keep bold and dim but let the cursor colour show, as the workset's DataTable cursor does."""
    flattened = Text(text.plain)
    for span in text.spans:
        flattened.stylize(Style(bold=span.style.bold, dim=span.style.dim), span.start, span.end)
    return flattened


def auxiliary_row_label(row: AuxiliaryRow, *, use_color: bool) -> str:
    if row.guard_skip is not None:
        return guard_skip_label(row.scope, row.directions[0], row.guard_skip.path_rule_pattern, use_color=use_color)
    return auxiliary_label(row.scope, row.kind, row.directions, use_color=use_color)


def workset_target_label(row, *, use_color: bool) -> str:
    if isinstance(row, AdditionalRow):
        return additional_label(row, use_color=use_color)
    label = auxiliary_row_label if isinstance(row, AuxiliaryRow) else unit_label
    if row.included_via and use_color:
        # Rows only a dependency closure brought in recede; the detail panel says which input did.
        return style_text(label(row, use_color=False), *MENU_HINT_STYLE)
    return label(row, use_color=use_color)


def included_via_fact(row: SessionRow | AuxiliaryRow, *, use_color: bool) -> list[tuple[str, str]]:
    if not row.included_via:
        return []
    labels = (render_package_label(repo_name=identity.repo, package_id=identity.package_id,
                                   bound_profile=identity.bound_profile, use_color=use_color)
              for identity in row.included_via)
    return [("Included via", ", ".join(labels))]


def row_selected(row) -> bool:
    return row.included if selection_uses_inclusion(row) else row.approved


def review_approval_term(row) -> str:
    if isinstance(row, SessionRow) and row.proposal is not None and row.proposal.noop:
        return "not needed"
    return "approved" if row.approved else "unapproved"


def unit_label(row, *, use_color: bool) -> str:
    identity = row.observation.identity
    label = render_package_label(
        repo_name=identity.repo, package_id=identity.package_id,
        target_name=identity.target_name, bound_profile=identity.bound_profile,
        use_color=use_color,
    )
    return label if identity.child_path is None else f"{label}/{identity.child_path}"


def unit_detail_facts(observation, *, use_color: bool) -> list[tuple[str, str]]:
    """Surface what the workset columns cannot fit; labels and colors match Proposal Review."""
    state = f"{observation.state} · Sync Base: {render_sync_term(observation.base.status, use_color=use_color)}"
    if observation.configured_policy != observation.effective_policy:
        state += f" · Configured policy: {observation.configured_policy}"
    # Full paths: the ring wraps them, so compaction would only hide identity.
    paths = [(label, display_review_path(path, compact=False))
             for label, path in (("Live path", observation.live_path), ("Repository path", observation.repository_path))
             if path is not None]
    return [*paths, ("Observation", state)]


def fact_grid(facts: list[tuple[str, str]], *, mark=lambda text: text) -> Table:
    """Label/value grid: wrapped values keep a hanging indent under their column."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    # Paths have no spaces to break at; fold them at the column edge.
    grid.add_column(ratio=1, overflow="fold")
    for label, value in facts:
        grid.add_row(mark(Text.from_ansi(label)), mark(Text.from_ansi(value)))
    return grid


def detail_renderable(identity: str, facts: list[tuple[str, str]]) -> Group:
    return Group(Text.from_ansi(identity, overflow="fold"),
                 Padding(fact_grid([(f"{label}:", value) for label, value in facts]), (0, 0, 0, 2)))


def row_resolution(row) -> str:
    """Capability absence is not a failed filesystem observation."""
    if isinstance(row, AuxiliaryRow):
        return auxiliary_resolution(row.kind)
    if isinstance(row, AdditionalRow):
        return "Additional Source Change"
    if any(item.severity == "error" for item in row.observation.diagnostics) or row.observation.state == "observation-failed":
        return "Observation failed"
    if row.diagnostics:
        return "Proposal failed"
    if row.proposal is not None and row.proposal.noop:
        return "No-op"
    if "prepare-proposal-review" not in row.allowed_commands:
        # In-sync units appear only to surface their warnings.
        return "In sync" if row.observation.state == "directly-in-sync" else "Unsupported"
    # Pull leaves its fixed direction implicit; Push records use-repository.
    return resolution_label((row.proposal and row.proposal.intent) or row.intent or "use-live")


def resolution_choosable(row) -> bool:
    return (isinstance(row, SessionRow) and "set-resolution-intent" in row.allowed_commands
            and len(row.allowed_intents) >= 2)


# Directions a one-way Policy or a push/pull operation imposes; Edited stays a decision.
POLICY_DIRECTION_LABELS = (resolution_label("use-repository"), resolution_label("use-live"))


def resolution_guess_shown(row) -> bool:
    # Failures and No-op describe the row itself, so they outrank the guess.
    return isinstance(row, SessionRow) and row.resolution_guessed and row_resolution(row) == resolution_label(row.intent)


def render_row_resolution(row, *, use_color: bool, mark_guess: bool = False) -> str:
    label = row_resolution(row)
    fixed = isinstance(row, SessionRow) and not resolution_choosable(row) and label in POLICY_DIRECTION_LABELS
    guessed = resolution_guess_shown(row)
    # The workset cell has no room for "(guessed)"; a "?" keeps the guess readable without color.
    if guessed and mark_guess:
        label += "?"
    return render_resolution(label, guessed=guessed, fixed=fixed, use_color=use_color)


def elide_middle(label: Text, width: int) -> Text:
    """Keep both the repo prefix and the target-name tail of long identities."""
    if label.cell_len <= width:
        return label
    # Identities are canonical ASCII in practice; character slicing may leave
    # wide glyphs slightly over width, which DataTable then crops.
    kept = max(width - 1, 0)
    head = kept // 2
    return Text.assemble(label[:head], "…", label[len(label) - (kept - head):])


class ReviewBody(Widget):
    """Review text laid out at the current width, then shown as plain styled lines.

    Textual selects individual characters only in text content; a Rich table
    (which provides the hanging indents) would select only as a whole block.
    """

    DEFAULT_CSS = "ReviewBody { height: auto; }"
    document: reactive[ReviewDocument | None] = reactive(None, layout=True)
    search: reactive[str] = reactive("", layout=True)
    # Rows where change blocks start, as laid out at the current width.
    change_rows: tuple[int, ...] = ()
    # Row where each search match starts, in match order.
    match_rows: tuple[int, ...] = ()

    def render(self) -> Text:
        change_rows = []
        match_rows: dict[int, int] = {}
        self.change_rows = self.match_rows = ()
        if self.document is None:
            return Text()
        console = self.app.console
        options = console.options.update_width(max(self.content_size.width, 1))
        lines = []
        for segments in console.render_lines(self.document.renderable(self.search), options, pad=False):
            metas = [segment.style.meta for segment in segments if segment.style]
            if any(meta.get(REVIEW_CHANGE_KEY) for meta in metas):
                change_rows.append(len(lines))
            for meta in metas:
                if REVIEW_MATCH_KEY in meta:
                    match_rows.setdefault(meta[REVIEW_MATCH_KEY], len(lines))
            line = Text.assemble(*((segment.text, segment.style) for segment in segments))
            # Strip grid padding so copied text has no trailing blanks.
            line.rstrip()
            lines.append(line)
        self.change_rows = tuple(change_rows)
        self.match_rows = tuple(row for _, row in sorted(match_rows.items()))
        return Text("\n").join(lines)


class CommitList(OptionList):
    """Commit Work toggles. Each entry spans two lines, but the cursor marks only
    its repo line (drawn by the label), as the workset cursor marks one row."""

    COMPONENT_CLASSES = {"commit-list--cursor"}


class WorksetTable(DataTable):
    """Render native cells; the app input boundary owns row actions."""

    # Narrower targets hide too much identity; horizontal scrolling takes over.
    MIN_TARGET_WIDTH = 16
    TARGET_COLUMN = 1

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.full_targets: dict[str, Text] = {}
        self._fitted_target_width: int | None = None

    def add_workset_row(self, row_id: str, target: Text, policy: str) -> None:
        self.full_targets[row_id] = target
        self._fitted_target_width = None
        self.add_row("", target, policy, "", key=row_id)

    def clear(self, columns: bool = False):
        self.full_targets.clear()
        self._fitted_target_width = None
        return super().clear(columns)

    def fit_targets(self) -> None:
        """Shrink the Target column so Selection, Policy and Resolution stay on screen."""
        if not self.full_targets or not self.size.width:
            return
        target_key = self.ordered_columns[self.TARGET_COLUMN].key
        other_columns = sum(
            column.get_render_width(self) for column in self.ordered_columns if column.key != target_key
        )
        available = self.scrollable_content_region.width - other_columns - 2 * self.cell_padding
        width = max(self.MIN_TARGET_WIDTH, available)
        if width == self._fitted_target_width:
            return
        self._fitted_target_width = width
        for row_id, target in self.full_targets.items():
            self.update_cell(row_id, target_key, elide_middle(target, width), update_width=True)

    def on_resize(self, event: events.Resize) -> None:
        self.fit_targets()
        # DataTable only scrolls on cursor moves; a detail that grows after the move
        # (wrapped paths) shrinks the table and would otherwise hide the focused row.
        self.move_cursor(row=self.cursor_row)

    def on_click(self, event: events.Click) -> None:
        if event.style.meta.get("row", -1) >= 0:
            # Row clicks already ran in input order at the App boundary. Letting
            # DataTable replay them here could undo newer keyboard navigation.
            event.prevent_default()
            event.stop()


# Enough to step back through a session's recent queries without scrolling a long list.
SEARCH_HISTORY_LIMIT = 10


class SearchInput(Input):
    """The / box; Up and Down recall recent queries, newest first, as in a shell."""

    BINDINGS = [
        Binding("up", "recall(1)", "Older query", show=False),
        Binding("down", "recall(-1)", "Newer query", show=False),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        # Index 0 is the draft typed before recall, so Down past the newest query restores it.
        self._recall: list[str] = [""]
        self._recall_index = 0

    def start(self, value: str, history: list[str]) -> None:
        self._history = history
        self._recall, self._recall_index = [value], 0
        self.value = value
        self.cursor_position = len(value)

    def remember(self, query: str) -> None:
        if query:
            self._history[:] = [query, *(entry for entry in self._history if entry != query)][:SEARCH_HISTORY_LIMIT]

    def action_recall(self, step: int) -> None:
        # Editing a recalled query makes it the new draft.
        if self.value != self._recall[self._recall_index]:
            self._recall, self._recall_index = [self.value], 0
        if self._recall_index == 0:
            # A query equal to the draft would make the first Up look like a no-op.
            self._recall = [self.value, *(entry for entry in self._history if entry != self.value)]
        self._recall_index = min(max(self._recall_index + step, 0), len(self._recall) - 1)
        self.value = self._recall[self._recall_index]
        self.cursor_position = len(self.value)


PROGRESS_REVEAL_DELAY_SECONDS = 0.3
PROGRESS_FRAME_SECONDS = 0.1
# Every notice clears on the next input; pure feedback (a copy, a query without
# matches) also clears on its own while the deck is idle.
TRANSIENT_NOTICE_SECONDS = 2.0
# Full View places a change block a few lines below the top, keeping context above it.
FULL_VIEW_LEAD_LINES = 3


class SyncDeckApp(App[bool]):
    """Terminal adapter: the public session remains the sole mutation authority."""

    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $surface; }
    /* Textual's 2-cell default steals width from the dense deck; match the 1-cell horizontal bar. */
    * { scrollbar-size-vertical: 1; }
    /* Textual's ANSI header puts default text on bright blue, a pastel in many dark palettes;
       bright black stays a darker fill under light text and differs from the light row cursor. */
    WorksetTable:ansi > .datatable--header { background: ansi_bright_black; color: ansi_default; }
    #title { height: auto; padding: 0 1; text-style: bold; color: $accent; }
    #workset { height: 1fr; }
    /* No side margin: a margined sibling narrows the workset table in Textual's vertical layout. */
    #detail { height: auto; max-height: 35%; border: round $foreground 30%; padding: 0 1; overflow-y: auto; }
    #detail:focus { border: round $accent; }
    #resolution { height: auto; max-height: 5; border: round $accent; margin: 0 1; }
    #review { height: 1fr; overflow-x: hidden; }
    /* Grid, not vertical: the 1fr row caps the commit list at the space left
       under the summary (max-height: 1fr has no effect in a vertical layout). */
    #confirmation-page { height: 1fr; layout: grid; grid-size: 1; grid-rows: auto 1fr; }
    #confirmation { height: auto; padding: 1 2; }
    /* Scrolls itself, so many repos never push the summary or hints away. */
    #commit-list { height: auto; max-height: 100%; border: round $foreground 30%; margin: 0 2; padding: 0 1; }
    #commit-list:focus { border: round $accent; }
    /* OptionList highlights every line of an entry; the label draws this cursor on its repo line instead. */
    CommitList > .option-list--option-highlighted,
    CommitList:focus > .option-list--option-highlighted { color: $foreground; background: transparent; text-style: none; }
    CommitList > .commit-list--cursor {
        color: $block-cursor-foreground; background: $block-cursor-background; text-style: $block-cursor-text-style;
    }
    #notice { height: auto; padding: 0 1; color: $warning; }
    #search-bar { height: 1; padding: 0 1; }
    #search-prompt { width: 1; }
    #help { dock: bottom; height: auto; max-height: 2; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        *[
            Binding(key, f"navigate('{table_action}', '{review_action}')",
                    show=False, priority=True)
            for key, table_action, review_action in (
                ("up,k", "cursor_up", "scroll_up"),
                ("down,j", "cursor_down", "scroll_down"),
                ("left,h", "cursor_left", "scroll_left"),
                ("right,l", "cursor_right", "scroll_right"),
                ("pageup", "page_up", "page_up"),
                ("pagedown", "page_down", "page_down"),
                ("home", "scroll_home", "scroll_home"),
                ("end", "scroll_end", "scroll_end"),
                ("ctrl+home", "scroll_top", "scroll_home"),
                ("ctrl+end", "scroll_bottom", "scroll_end"),
            )
        ],
        Binding("r", "resolution", "Resolution", priority=True),
        Binding("t", "retry", "Retry", priority=True),
        # Lowercase l is vim-style right; authorization needs the deliberate Shift+L.
        Binding("L", "authorize_link", "Authorize link replacement", priority=True),
        Binding("e", "editor", "Editor", priority=True),
        Binding("v", "full_view", "Full view", priority=True),
        Binding("slash", "open_search", "Search", priority=True),
        Binding("n", "step(1)", "Next match or change", priority=True),
        Binding("N", "step(-1)", "Previous match or change", priority=True),
        Binding("tab,shift+tab", "toggle_detail_focus", "Detail", priority=True),
        # x mirrors the "[x]" Selection marker; Space stays as the common TUI toggle but goes unhinted.
        Binding("space,x", "select", "Select", priority=True),
        Binding("a", "select_all", "Select all", priority=True),
        Binding("u", "clear_selection", "Clear selection", priority=True),
        Binding("enter", "review_or_confirm", "Review / Confirm", priority=True),
        Binding("c", "confirm", "Preview / Execute", priority=True),
        Binding("g", "commit_all", "Commit all", priority=True),
        # Ctrl+C stays Abort, so copying needs its own key (vim-style yank).
        Binding("y", "copy", "Copy", priority=True),
        Binding("escape", "back", "Back", priority=True),
        # Lowercase like every other letter key, matching the q-to-quit convention of other TUIs.
        Binding("q", "quit_workset", "Abort", priority=True),
        Binding("ctrl+c", "abort", "Abort", priority=True),
    ]

    def __init__(self, deck: CommandDeck) -> None:
        super().__init__(ansi_color=True)
        self.theme = "ansi-dark"
        self.deck = deck
        self.review_positions: dict[str, tuple[float, float]] = {}
        self._workset_mouse_down = False
        self._pressed_hint_action: str | None = None
        self._hovered_hint_action: str | None = None
        self._detail_row_id: str | None = None
        self._lane = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sync-materialization")
        self._materialization: asyncio.Task | None = None
        self._aborting = False
        self._editing = False
        # One OptionList serves the Resolution and Full View menus.
        self._menu_choose = None
        # Full View reuses the review scroller; the review offset is restored on return.
        self._review_position_before_full_view = (0.0, 0.0)
        # A plain flag: Textual may ask check_action before the search box is composed.
        self._search_open = False
        self._filter_before_search = ""
        self._match_index = 0
        # Filters name Targets and searches find file text, so each keeps its own history.
        self._search_history: dict[str, list[str]] = {"filter": [], "search": []}

    @property
    def busy(self) -> bool:
        # Completed dispatch has drained even before its done callback clears the handle.
        return self._aborting or (
            self._materialization is not None and not self._materialization.done()
        )

    def materialize(self, action, *, editor_io: str | None = None, row_ids: tuple[str, ...] | None = None) -> None:
        """Admit one mutation; all further input is rejected until actual work drains."""
        if self.busy:
            return
        self._editing = editor_io is not None
        self._progress_label = (
            "Editing Proposal · Ctrl+C cancel Editor" if self._editing
            else "Materializing Proposal · Ctrl+C abort"
        )
        focused = self.deck.focused_row
        self._progress_rows = row_ids if row_ids is not None else ((focused.row_id,) if focused else ())
        self._progress_spinner = Spinner("dots")
        self._progress_started = time.monotonic()
        # A terminal Editor owns the screen, so deck progress would never be seen.
        self._progress = None if editor_io == "tty" else self.set_interval(PROGRESS_FRAME_SECONDS, self._render_progress)
        self._materialization = asyncio.create_task(self._materialize(action, editor_io=editor_io))
        self._materialization.add_done_callback(self._materialization_finished)

    def _render_progress(self) -> None:
        """Spin in the affected rows' Selection cells, so progress takes no extra line."""
        now = time.monotonic()
        # Most dispatches finish within a frame or two; showing progress
        # immediately makes every Enter/Space flash it.
        if not self.busy or now - self._progress_started < PROGRESS_REVEAL_DELAY_SECONDS:
            return
        frame = self._progress_spinner.render(now).plain
        table = self.query_one(WorksetTable)
        help_text = self._progress_label
        if table.display:
            for row_id in self._progress_rows:
                # Pad to the "[ ]" marker width so the column does not jitter.
                table.update_cell(row_id, table.ordered_columns[0].key, Text(f" {frame} ", style="bold"))
        else:
            # Review has no Selection cells on screen; spin in the help line instead.
            help_text = f"{frame} {help_text}"
        self.query_one("#help", Static).update(help_text)

    def _materialization_finished(self, task: asyncio.Task) -> None:
        if self._materialization is task:
            self._materialization = None
        if not task.cancelled() and (error := task.exception()) is not None:
            self._handle_exception(error)

    async def _materialize(self, action, *, editor_io: str | None = None) -> None:
        def dispatch():
            # KeyboardInterrupt must not escape a Future into the asyncio runner.
            try:
                self.deck.session.check_cancelled()
                action()
                self.deck.session.check_cancelled()
            except KeyboardInterrupt as exc:
                raise InterruptedError("Materialization interrupted") from exc

        try:
            with ExitStack() as terminal:
                if editor_io == "tty":
                    # The event loop keeps running while the Editor owns the
                    # terminal. Suspension stops Textual's bounded writer
                    # thread, so repaints (the busy spinner) would fill its
                    # queue and block the loop forever. Resume runs first on
                    # exit (LIFO), then the batch ends and repaints the deck.
                    terminal.enter_context(self.batch_update())
                    suspension = self.suspend()
                    suspension.__enter__()
                    # Textual resumes after its yield only on normal context exit.
                    # Always restore the terminal, including unexpected dispatch errors.
                    terminal.callback(suspension.__exit__, None, None, None)
                work = asyncio.get_running_loop().run_in_executor(
                    self._lane, copy_context().run, dispatch,
                )
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    self._aborting = True
                    self.deck.session.request_cancel()
                    try:
                        await asyncio.shield(work)
                    except InterruptedError:
                        pass
                except InterruptedError:
                    self._aborting = True
                except Exception:
                    self._aborting = True
                    raise
        except SuspendNotSupported:
            self.deck.notice = "Terminal Editor requires a suspend-capable terminal."
        finally:
            self._materialization = None
            self._editing = False
            if self._progress is not None:
                self._progress.stop()
        if self._aborting or any(d.code == "interrupted" for row in self.deck.session.view.rows for d in row.diagnostics):
            self.exit(False)
            return
        self.update_workset()
        if self.deck.reviewing:
            self.show_review()

    async def on_unmount(self) -> None:
        if self._materialization is not None:
            self._aborting = True
            self.deck.session.request_cancel()
            await asyncio.shield(self._materialization)
        # Textual cancellation is not a thread join. Release the lane only after
        # its dispatch and provider temporary-resource cleanup have really ended.
        self._lane.shutdown(wait=True)

    async def on_event(self, event: events.Event) -> None:
        if self.busy and isinstance(event, events.InputEvent):
            if isinstance(event, events.Key) and event.key == "ctrl+c":
                self.action_abort()
            # A dropped press must not pair with a later release on a chip or row.
            self._pressed_hint_action = None
            self._workset_mouse_down = False
            event.stop()
            event.prevent_default()
            return
        # A notice answers the previous key or click. Clear it before this input's
        # action runs, so a new notice from that action still shows.
        if isinstance(event, (events.Key, events.MouseDown)) and self.deck.notice:
            self.deck.notice = ""
            self.query_one("#notice", Static).update("")
        # App receives terminal input in order, before forwarding mouse events to
        # widget queues. Resolve row clicks here alongside priority key actions;
        # neither input modality may overtake the other when bytes arrive together.
        if isinstance(event, (events.MouseDown, events.MouseUp)) and not event.is_forwarded:
            try:
                widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
            except NoWidget:
                widget = None
            workset = isinstance(widget, WorksetTable) and not (
                self.deck.reviewing or self.deck.confirming
            )
            metadata = self.screen.get_style_at(event.screen_x, event.screen_y).meta
            hint_action = metadata.get(HINT_ACTION_KEY)
            if isinstance(event, events.MouseDown):
                self._workset_mouse_down = workset
                self._pressed_hint_action = hint_action
            else:
                # Like a button, a chip runs only when pressed and released on it.
                if hint_action is not None and hint_action == self._pressed_hint_action:
                    getattr(self, f"action_{hint_action}")()
                elif workset and self._workset_mouse_down:
                    row, column = metadata.get("row", -1), metadata.get("column", -1)
                    if row >= 0 and column >= 0 and not metadata.get("out_of_bounds"):
                        widget.move_cursor(row=row, column=column)
                        self.deck.focus = row
                        if column == 0:
                            self.materialize(self.deck.select)
                        self.close_menu()
                        self.update_workset()
                        if column == 3:
                            self.action_resolution()
                self._workset_mouse_down = False
                self._pressed_hint_action = None
        # Preserve native focus, mouse capture, selection cleanup and scrolling.
        await super().on_event(event)

    def compose(self) -> ComposeResult:
        yield Static(f":: {self.deck.session.view.operation.title()} Command Deck", id="title", markup=False)
        yield WorksetTable(id="workset", cursor_type="row", zebra_stripes=True)
        # A scroll container: a bare Static cannot scroll, so overflowing details would be unreachable.
        with VerticalScroll(id="detail"):
            yield Static(id="detail-body", markup=False)
        # Review wraps instead of scrolling sideways; a Static re-wraps on resize, unlike a RichLog.
        with VerticalScroll(id="review"):
            yield ReviewBody(id="review-body")
        with Vertical(id="confirmation-page"):
            yield Static(id="confirmation", markup=False)
            yield CommitList(id="commit-list")
        # After the pages, so menus open at the bottom above notice and help in every view.
        yield OptionList(id="resolution")
        yield Static(id="notice", markup=False)
        with Horizontal(id="search-bar"):
            yield Static("/", id="search-prompt", markup=False)
            yield SearchInput(id="search", compact=True, select_on_focus=False)
        yield Static(id="help", markup=False)

    def on_mount(self) -> None:
        self.query_one("#resolution", OptionList).display = False
        self.query_one("#search-bar").display = False
        self.watch(self.query_one("#help"), "hover_style", self.hover_hint, init=False)
        table = self.query_one(WorksetTable)
        # The Selection header matches its narrow "[ ]" markers, leaving width for targets.
        table.add_columns("✓", "Target", "Policy", "Resolution")
        self.show_workset()

    def rebuild_workset(self) -> None:
        table = self.query_one(WorksetTable)
        table.clear()
        rows = self.deck.visible_rows
        self.deck.focus = max(0, min(self.deck.focus, len(rows) - 1))
        for row in rows:
            table.add_workset_row(row.row_id, Text.from_ansi(workset_target_label(row, use_color=self.deck.use_color)),
                                  row.observation.effective_policy if isinstance(row, SessionRow) else "")
        table.move_cursor(row=self.deck.focus)

    def update_workset(self) -> None:
        table = self.query_one(WorksetTable)
        rows = self.deck.visible_rows
        # Editor saves can add or remove canonical source rows; filtering changes the visible ones.
        if tuple(key.value for key in table.rows) != tuple(row.row_id for row in rows):
            self.rebuild_workset()
        for row in rows:
            auxiliary = selection_uses_inclusion(row)
            selected = row_selected(row)
            selectable = row.approvable if isinstance(row, SessionRow) else bool(
                {"set-included", "set-approval"}.intersection(row.allowed_commands))
            term = ("selected" if selected else "unselected") if auxiliary else ("approved" if selected else "unapproved")
            table.update_cell(row.row_id, table.ordered_columns[0].key,
                              Text.from_ansi(render_selection_marker(selected, selectable=selectable, term=term,
                                                                     use_color=self.deck.use_color)),
                              update_width=True)
            table.update_cell(row.row_id, table.ordered_columns[3].key,
                              Text.from_ansi(render_row_resolution(row, use_color=self.deck.use_color, mark_guess=True)),
                              update_width=True)
        # Resolution width varies with intent, so refit after every cell update.
        table.fit_targets()
        self.update_detail()
        if not self.deck.reviewing and not self.deck.confirming:
            self.query_one("#title", Static).update(self.workset_title())
        self.query_one("#notice", Static).update(self.deck.notice)
        self.update_hints()

    def workset_title(self) -> str:
        title = f":: {self.deck.session.view.operation.title()} Command Deck"
        # Execution includes hidden selections, so a filter must never hide them silently.
        hidden = self.deck.hidden_selected_count
        return f"{title} ({hidden} selected hidden)" if hidden else title

    @property
    def detail_focused(self) -> bool:
        return self.focused is self.query_one("#detail")

    def update_hints(self) -> None:
        review_scroll = ("↑/↓/j/k", "scroll")
        select_hint = ("x", "select")
        bulk_selection = ("a/u", "all/none")
        # First, so a wrapped help line never pushes it out of sight.
        confirm_chip = HintChip("c", "confirm", "confirm")
        body = self.query_one(ReviewBody)
        if body.search:
            review_lead = [(f"/{body.search}", f"{self._match_index + 1}/{len(body.match_rows)}"),
                           ("n/N", "next/previous match"), ("Esc", "clear")]
        elif self.deck.full_view is not None:
            review_lead = [("Esc", "return"), ("n/N", "next/previous change"), ("/", "search")]
        else:
            review_lead = [("Esc", "return"), ("/", "search")]
        if self._search_open:
            # Deck keys type into the box, so only its own keys apply.
            hints = [("Enter", "search" if self.deck.reviewing else "filter"), ("↑/↓", "history"),
                     ("Ctrl+U", "clear"), ("Esc", "cancel"), ("Ctrl+C", "abort")]
        elif self.query_one("#resolution", OptionList).display:
            hints = [("↑/↓/j/k", "move"), ("Enter", "choose"), ("Esc", "dismiss")]
        elif self.deck.confirming:
            verb = "preview" if self.deck.session.view.preview else "execute"
            # Return takes the spot of the workset's leading confirm chip, so a
            # double-click on [c confirm] cannot land on execute.
            hints = [HintChip("Esc", "return", "back"), HintChip("Enter", verb, "review_or_confirm"),
                     *((("x", "commit"), ("g", "commit all"))
                       if self.query_one("#commit-list", OptionList).display else ()),
                     ("Ctrl+C", "abort")]
        elif self.deck.full_view is not None:
            hints = [*review_lead, ("y", "copy"), review_scroll, ("Ctrl+C", "abort")]
        elif self.deck.reviewing and isinstance(self.deck.focused_row, AdditionalRow):
            hints = [*review_lead, select_hint, ("v", "full view"), ("y", "copy"), review_scroll,
                     ("Ctrl+C", "abort")]
        elif self.deck.reviewing:
            hints = [*review_lead, select_hint, ("e", "edit"), ("t", "retry"), ("v", "full view"),
                     ("y", "copy"), review_scroll, ("Ctrl+C", "abort")]
        elif self.detail_focused:
            hints = [("Tab/Esc", "return"), review_scroll, select_hint, bulk_selection,
                     ("Enter", "review"), ("e", "edit"), ("t", "retry"), ("y", "copy")]
        elif self.deck.filter:
            hints = [confirm_chip, (f"/{self.deck.filter}", f"{len(self.deck.visible_rows)}/{len(self.deck.session.view.rows)}"),
                     ("Esc", "clear"), select_hint, bulk_selection, ("Enter", "review"),
                     ("e", "edit"), ("t", "retry"), ("y", "copy"), ("Tab", "detail")]
        else:
            hints = [confirm_chip, ("q", "abort"), select_hint, bulk_selection, ("/", "filter"),
                     ("Enter", "review"), ("e", "edit"), ("t", "retry"), ("y", "copy"), ("Tab", "detail")]
        row = self.deck.focused_row
        if (row and "authorize-symlink-replacement" in row.allowed_commands and not self.deck.confirming
                and not self._search_open and self.deck.full_view is None):
            hints.append(("L", "link"))
        if (resolution_choosable(row) and not self.deck.reviewing and not self._search_open
                and not self.query_one("#resolution", OptionList).display and not self.deck.confirming):
            hints.append(("r", "intent"))
        self.query_one("#help", Static).update(render_hint_line(
            hints, use_color=self.deck.use_color, hovered_action=self._hovered_hint_action))

    def hover_hint(self, hover_style: Style) -> None:
        # Textual's own link hover only follows "@click" metadata, whose deferred
        # widget dispatch could let a chip click overtake a later key.
        hovered_action = hover_style.meta.get(HINT_ACTION_KEY)
        if hovered_action != self._hovered_hint_action:
            self._hovered_hint_action = hovered_action
            self.update_hints()

    def update_detail(self) -> None:
        row = self.deck.focused_row
        use_color = self.deck.use_color
        detail, body = self.query_one("#detail"), self.query_one("#detail-body", Static)
        if row is None:
            empty = "No Target matches the filter." if self.deck.filter else "No drifted work."
            body.update(Text.from_ansi(render_payload_section_label(empty, use_color=use_color)))
            return
        facts = [(render_sync_term(item.severity, use_color=use_color), item.message) for item in row_diagnostics(row)]
        if isinstance(row, AdditionalRow):
            identity = additional_label(row, use_color=use_color)
        elif isinstance(row, AuxiliaryRow):
            identity = auxiliary_row_label(row, use_color=use_color)
            if row.guard_skip is not None:
                facts.append((render_sync_term('Guard skipped', use_color=use_color), guard_skip_explanation(row)))
            facts += included_via_fact(row, use_color=use_color)
        else:
            identity = unit_label(row, use_color=use_color)
            facts += included_via_fact(row, use_color=use_color)
            facts += unit_detail_facts(row.observation, use_color=use_color)
        if row.row_id != self._detail_row_id:
            # A newly focused row starts from its identity, not the old scroll offset.
            self._detail_row_id = row.row_id
            detail.scroll_home(animate=False)
        body.update(detail_renderable(identity, facts))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self.busy and not self.deck.reviewing and not self.deck.confirming:
            self.sync_focus()
            self.update_detail()
            # Row-specific hints (r, L) follow the focused row.
            self.update_hints()

    def show_workset(self) -> None:
        self.query_one("#workset").display = True
        self.query_one("#detail").display = True
        self.query_one("#review").display = False
        self.query_one("#confirmation-page").display = False
        self.update_workset()
        self.query_one(WorksetTable).focus()

    async def action_navigate(self, table_action: str, review_action: str) -> None:
        if self.busy:
            return
        # Every native cursor action shares the Approval/Review queue. Otherwise
        # batched terminal keys can approve the old row before navigation runs.
        if self.deck.confirming:
            commit_list = self.query_one("#commit-list", OptionList)
            if commit_list.display and table_action in ("cursor_up", "cursor_down"):
                await commit_list.run_action(table_action)
            return
        if self.query_one("#resolution", OptionList).display:
            menu_action = {"cursor_up": "cursor_up", "cursor_down": "cursor_down", "scroll_home": "first", "scroll_end": "last"}.get(table_action)
            if menu_action:
                await self.query_one("#resolution", OptionList).run_action(menu_action)
            return
        if self.deck.reviewing:
            await self.query_one("#review").run_action(review_action)
        elif self.detail_focused:
            await self.query_one("#detail").run_action(review_action)
        else:
            table = self.query_one(WorksetTable)
            if table.row_count:
                await table.run_action(table_action)
            self.sync_focus()

    def sync_focus(self) -> None:
        # A following key may arrive before RowHighlighted is delivered (paste/PTY).
        # Read the widget cursor at the action boundary, not the queued notification.
        if not self.deck.reviewing and not self.deck.confirming:
            self.deck.focus = self.query_one(WorksetTable).cursor_row

    def action_select(self) -> None:
        if self.busy or self.deck.full_view is not None:
            return
        if self.deck.confirming:
            commit_list = self.query_one("#commit-list", OptionList)
            if commit_list.display and commit_list.highlighted is not None:
                self.toggle_commit(commit_list.highlighted)
            return
        if self.query_one("#resolution", OptionList).display:
            return
        self.sync_focus()
        self.materialize(self.deck.select)

    def action_select_all(self) -> None:
        if self.busy or self.deck.full_view is not None:
            return
        if self.query_one("#resolution", OptionList).display:
            return
        self.materialize(lambda: self.deck.select_all(True), row_ids=self.all_row_ids())

    def action_clear_selection(self) -> None:
        if self.busy or self.deck.full_view is not None:
            return
        if self.query_one("#resolution", OptionList).display:
            return
        self.materialize(lambda: self.deck.select_all(False), row_ids=self.all_row_ids())

    def all_row_ids(self) -> tuple[str, ...]:
        return tuple(row.row_id for row in self.deck.visible_rows)

    def show_review(self, position: tuple[float, float] | None = None) -> None:
        log = self.query_one("#review", VerticalScroll)
        if position is None:
            position = self.review_positions.get(self.deck.focused_row.row_id, (0, 0))
            if log.display:
                position = (log.scroll_x, log.scroll_y)
        self.query_one("#workset").display = False
        self.query_one("#detail").display = False
        log.display = True
        self.clear_search()
        self.query_one(ReviewBody).document = self.deck.review_document()
        row = self.deck.focused_row
        title = ":: Additional Source Review" if isinstance(row, AdditionalRow) else ":: Proposal Review"
        # x toggles Approval while the Decision section may be scrolled away; the title keeps it in view.
        approval = render_sync_term(review_approval_term(row), use_color=self.deck.use_color)
        self.query_one("#title", Static).update(Text.from_ansi(f"{title} ({approval})"))
        log.focus()
        self.call_after_refresh(log.scroll_to, *position, animate=False)

    def action_review_or_confirm(self) -> None:
        if self.busy:
            return
        menu = self.query_one("#resolution", OptionList)
        if menu.display:
            if menu.highlighted is not None:
                self._menu_choose(menu.highlighted)
            return
        self.sync_focus()
        if self.deck.confirming:
            self.exit(True)
        elif not self.deck.reviewing:
            self.materialize(self.deck.open_review)

    def action_confirm(self) -> None:
        if self.busy:
            return
        self.close_menu()
        self.deck.confirm()
        if self.deck.confirming:
            self.query_one("#workset").display = False
            self.query_one("#detail").display = False
            self.query_one("#confirmation-page").display = True
            self.query_one("#confirmation", Static).update(Text.from_ansi(self.deck.confirmation_text()))
            self.query_one("#title", Static).update(":: Confirmation")
            self.show_commit_list()
        self.update_workset()

    def show_commit_list(self) -> None:
        """Render the commit toggles from session state, keeping the highlighted repo."""
        commit_list = self.query_one("#commit-list", CommitList)
        choices = self.deck.commit_choices()
        if [item.id for item in commit_list.options] != [option.repo for option, _ in choices]:
            commit_list.clear_options()
            commit_list.add_options([Option("", id=option.repo) for option, _ in choices])
        commit_list.border_title = "Commit to git"
        commit_list.display = bool(choices)
        if choices:
            if commit_list.highlighted is None:
                commit_list.highlighted = 0
            self.render_commit_labels()
            commit_list.focus()
        else:
            self.set_focus(None)
        self.update_hints()

    def render_commit_labels(self) -> None:
        """Redraw labels in place; the cursor lives in the highlighted label."""
        commit_list = self.query_one("#commit-list", CommitList)
        cursor_style = commit_list.get_component_rich_style("commit-list--cursor")
        for index, (option, message) in enumerate(self.deck.commit_choices()):
            commit_list.replace_option_prompt_at_index(index, commit_choice_label(
                option, message, use_color=self.deck.use_color,
                cursor_style=cursor_style if index == commit_list.highlighted else None))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "commit-list":
            self.render_commit_labels()

    def toggle_commit(self, index: int) -> None:
        option, _ = self.deck.commit_choices()[index]
        self.deck.set_commit(option.repo, not option.selected)
        self.show_commit_list()

    def action_commit_all(self) -> None:
        if self.busy or not self.deck.confirming or not self.query_one("#commit-list", OptionList).display:
            return
        self.deck.toggle_all_commits()
        self.show_commit_list()

    def action_full_view(self) -> None:
        if self.busy or not self.deck.reviewing or self.deck.full_view is not None or self.query_one("#resolution", OptionList).display:
            return
        titles = self.deck.full_view_titles()
        if not titles:
            self.deck.notice = "No diff to show in Full View."
            self.update_workset()
        elif len(titles) == 1:
            self.open_full_view(titles[0])
        else:
            self.open_menu([Text(title) for title in titles], highlighted=0,
                           choose=lambda index: self.open_full_view(titles[index]))
            self.update_hints()

    def open_full_view(self, title: str) -> None:
        self.close_menu()
        log = self.query_one("#review", VerticalScroll)
        self._review_position_before_full_view = (log.scroll_x, log.scroll_y)
        self.deck.open_full_view(title)
        self.clear_search()
        body = self.query_one(ReviewBody)
        body.document = self.deck.displayed_document()
        self.query_one("#title", Static).update(":: Full View")
        self.update_hints()
        log.scroll_home(animate=False)

        def land_on_first_change() -> None:
            # Stay at the top, with the target and section heading, unless the first
            # change block starts below the screen.
            if body.change_rows and body.change_rows[0] + FULL_VIEW_LEAD_LINES >= log.scrollable_content_region.height:
                self.scroll_to_review_row(body.change_rows[0])
        self.call_after_refresh(land_on_first_change)

    def scroll_to_review_row(self, row: int) -> None:
        self.query_one("#review").scroll_to(0, max(0, row - FULL_VIEW_LEAD_LINES), animate=False)

    def review_anchor(self) -> float:
        return self.query_one("#review").scroll_y + FULL_VIEW_LEAD_LINES

    def action_step(self, direction: int) -> None:
        """Step search matches while a search is active, otherwise Full View change blocks."""
        if self.busy or not self.deck.reviewing:
            return
        body = self.query_one(ReviewBody)
        if body.search:
            # A key batched with Enter can arrive before the first render counts matches.
            if not body.match_rows:
                return
            # Matches wrap around; an index, not the scroll anchor, because the last
            # matches may sit below the furthest reachable scroll position.
            self._match_index = (self._match_index + direction) % len(body.match_rows)
            self.scroll_to_review_row(body.match_rows[self._match_index])
            self.update_hints()
            return
        if self.deck.full_view is None:
            return
        anchor = self.review_anchor()
        rows = body.change_rows
        target = (next((row for row in rows if row > anchor), None) if direction > 0
                  else next((row for row in reversed(rows) if row < anchor), None))
        if target is not None:
            self.scroll_to_review_row(target)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Deck keys are priority bindings. While the search box is open a disabled
        # action lets Textual forward the key to the box as text instead.
        return not self._search_open or action in ("back", "abort")

    def action_open_search(self) -> None:
        if self.busy or self.deck.confirming or self.query_one("#resolution", OptionList).display:
            return
        self._search_open = True
        # Esc in the box restores the workset filter from before it opened.
        self._filter_before_search = self.deck.filter
        # A workset filter opens for refinement; a reader search starts afresh, as in less.
        box = self.query_one(SearchInput)
        box.start("" if self.deck.reviewing else self.deck.filter,
                  self._search_history["search" if self.deck.reviewing else "filter"])
        self.query_one("#search-bar").display = True
        box.focus()
        self.update_hints()

    def close_search_box(self) -> None:
        self._search_open = False
        self.query_one("#search-bar").display = False
        self.query_one("#review" if self.deck.reviewing else WorksetTable).focus()
        self.update_hints()

    def cancel_search_box(self) -> None:
        self.close_search_box()
        if not self.deck.reviewing:
            self.apply_filter(self._filter_before_search)

    def apply_filter(self, query: str) -> None:
        self.deck.set_filter(query)
        self.update_workset()

    def on_input_changed(self, event: Input.Changed) -> None:
        # The workset narrows while typing; the reader highlights only on Enter.
        if self._search_open and not self.deck.reviewing:
            self.apply_filter(event.value)

    def clear_search(self) -> None:
        self.query_one(ReviewBody).search = ""
        self._match_index = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value
        self.query_one(SearchInput).remember(query)
        self.close_search_box()
        if not self.deck.reviewing:
            if query and not self.deck.visible_rows:
                self.apply_filter("")
                self.flash_notice(f"No match for {query}.")
            return
        if not query:
            return
        body = self.query_one(ReviewBody)
        body.search = query

        def land_on_first_match() -> None:
            if not body.match_rows:
                self.clear_search()
                self.flash_notice(f"No match for {query}.")
                return
            anchor = self.review_anchor()
            self._match_index = next(
                (index for index, row in enumerate(body.match_rows) if row >= anchor), 0)
            self.scroll_to_review_row(body.match_rows[self._match_index])
            self.update_hints()
        self.call_after_refresh(land_on_first_match)

    def action_back(self) -> None:
        if self.busy:
            return
        if self._search_open:
            self.cancel_search_box()
            return
        if self.query_one("#resolution", OptionList).display:
            self.close_menu()
            self.update_workset()
            return
        if self.query_one(ReviewBody).search:
            self.clear_search()
            self.update_hints()
            return
        if self.deck.full_view is not None:
            self.deck.back()
            self.show_review(self._review_position_before_full_view)
            self.update_hints()
            return
        if self.detail_focused:
            self.query_one(WorksetTable).focus()
            return
        if self.deck.filter and not self.deck.reviewing and not self.deck.confirming:
            self.apply_filter("")
            return
        if self.deck.reviewing:
            log = self.query_one("#review", VerticalScroll)
            self.review_positions[self.deck.focused_row.row_id] = (log.scroll_x, log.scroll_y)
        # At the workset Esc does nothing: repeated Esc presses out of review
        # must not discard the session.
        if self.deck.back():
            self.show_workset()

    def action_toggle_detail_focus(self) -> None:
        if self.busy or self.deck.reviewing or self.deck.confirming or self.query_one("#resolution", OptionList).display:
            return
        (self.query_one(WorksetTable) if self.detail_focused else self.query_one("#detail")).focus()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # A click elsewhere cancels an open search box: deck keys stay disabled only while it has focus.
        if self._search_open and event.widget is not self.query_one("#search"):
            self.close_search_box()
        # Keyboard toggles and mouse clicks both move focus; hints follow either.
        self.update_hints()

    def open_menu(self, labels: list[Text], *, highlighted: int, choose) -> None:
        self._menu_choose = choose
        menu = self.query_one("#resolution", OptionList)
        menu.clear_options()
        menu.add_options(labels)
        menu.highlighted = highlighted
        menu.display = True
        menu.focus()

    def close_menu(self) -> None:
        self.query_one("#resolution", OptionList).display = False
        self.query_one("#review" if self.deck.reviewing else WorksetTable).focus()

    def action_resolution(self) -> None:
        if self.busy:
            return
        if self.deck.reviewing or self.deck.confirming:
            return
        self.sync_focus()
        row = self.deck.focused_row
        if not resolution_choosable(row):
            return
        if len(row.allowed_intents) == 2:
            # With one alternative, a menu only adds a keystroke; toggling is visible in the Resolution cell.
            self.set_resolution(row, next(intent for intent in row.allowed_intents if intent != row.intent))
            return
        self.open_menu([Text.from_ansi(render_sync_term(resolution_label(intent), use_color=self.deck.use_color))
                        for intent in row.allowed_intents],
                       highlighted=row.allowed_intents.index(row.intent),
                       choose=self.choose_resolution)
        self.update_workset()

    def choose_resolution(self, index: int) -> None:
        if self.busy:
            return
        row = self.deck.focused_row
        self.close_menu()
        self.set_resolution(row, row.allowed_intents[index])

    def set_resolution(self, row: SessionRow, intent: str) -> None:
        def change():
            result = set_resolution_intent(self.deck.session, row.row_id, intent)
            self.deck.notice = result.reason if isinstance(result, CommandRejected) else ""
        self.materialize(change)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self.busy:
            return
        # Enter belongs to execute, so only a click selects a commit toggle.
        if event.option_list.id == "commit-list":
            self.toggle_commit(event.option_index)
        elif self.query_one("#resolution", OptionList).display:
            self._menu_choose(event.option_index)

    def action_editor(self) -> None:
        if self.busy or self.deck.confirming or self.deck.full_view is not None or self.query_one("#resolution", OptionList).display:
            return
        self.sync_focus()
        row = self.deck.focused_row
        if row is None or "edit-proposal" not in row.allowed_commands:
            return
        self.materialize(self.deck.edit, editor_io=row.editor_io)

    def action_authorize_link(self) -> None:
        if self.busy or self.deck.confirming or self.deck.full_view is not None or self.query_one("#resolution", OptionList).display:
            return
        self.sync_focus()
        row = self.deck.focused_row
        if row is None or "authorize-symlink-replacement" not in row.allowed_commands:
            return
        def authorize():
            view = self.deck.session.view
            self.deck.session.dispatch(AuthorizeSymlinkReplacement(view.session_id, view.revision, row.row_id))
            self.deck.notice = "Link replacement authorized; select the Proposal to approve."
        self.materialize(authorize)

    def action_retry(self) -> None:
        if self.busy:
            return
        if self.deck.confirming or self.deck.full_view is not None:
            return
        self.sync_focus()
        row = self.deck.focused_row
        if row is None or "retry-materialization" not in row.allowed_commands:
            return
        def retry():
            result = retry_materialization(self.deck.session, row.row_id)
            self.deck.notice = result.reason if isinstance(result, CommandRejected) else ""
        self.materialize(retry)

    def action_copy(self) -> None:
        """Copy the full Target identity, or the review selection or whole review, via the terminal clipboard (OSC 52)."""
        if self.busy or self.deck.confirming or self.query_one("#resolution", OptionList).display:
            return
        self.sync_focus()
        row = self.deck.focused_row
        if row is None:
            return
        if self.deck.reviewing and (selected := self.screen.get_selected_text()):
            # Ctrl+C is Abort, so y also copies a mouse selection.
            text, subject = selected, "selection"
            self.screen.clear_selection()
        elif self.deck.reviewing:
            text, subject = Text.from_ansi(self.deck.review_text()).plain, "review"
        else:
            # The Target cell may be elided; copy the untruncated identity.
            text, subject = self.query_one(WorksetTable).full_targets[row.row_id].plain, "Target"
        self.copy_to_clipboard(text)
        self.flash_notice(f"Copied {subject} to clipboard.")

    def flash_notice(self, notice: str) -> None:
        self.deck.notice = notice
        self.update_workset()
        self.set_timer(TRANSIENT_NOTICE_SECONDS, lambda: self.dismiss_notice(notice))

    def dismiss_notice(self, notice: str) -> None:
        # A later command may have replaced the notice; leave that one alone.
        if self.deck.notice == notice:
            self.deck.notice = ""
            self.query_one("#notice", Static).update("")

    def action_quit_workset(self) -> None:
        # q is a pager habit elsewhere, so only the idle workset treats it as Abort;
        # Ctrl+C remains the abort from every screen. A filtered workset is not idle:
        # q right after Enter may be meant for the query and would discard the session.
        if (self.busy or self.deck.reviewing or self.deck.confirming or self.deck.filter
                or self.query_one("#resolution", OptionList).display):
            return
        self.action_abort()

    def action_abort(self) -> None:
        if self._editing:
            self.deck.session.request_editor_cancel()
            return
        self._aborting = True
        self.deck.session.request_cancel()
        if self._materialization is None or self._materialization.done():
            self.exit(False)


def run_command_deck(session: SyncSession, *, use_color: bool) -> bool:
    app = SyncDeckApp(CommandDeck(session, use_color=use_color))
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda signum, frame: app.action_abort())
    try:
        return bool(app.run())
    finally:
        try:
            if app._materialization is not None:
                session.request_cancel()
            app._lane.shutdown(wait=True)
        finally:
            signal.signal(signal.SIGINT, previous)
