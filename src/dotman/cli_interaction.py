from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dotman import cli_emit, cli_style
from dotman.add import (
    AddOperationResult,
    AddReviewResult,
    review_add_manifest,
)
from dotman.command_runtime import (
    CommandRequest,
    current_command_runtime,
)
from dotman.diff_review import (
    ReviewItem,
    run_review_item_diff,
)
from dotman.editor import editor_command, resolve_editor_command
from dotman.engine import DotmanEngine
from dotman.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    Interaction,
    TextInputRequest,
    parse_selection_index,
)
from dotman.errors import UsageError
from dotman.interaction_policy import InteractionRequiredError
from dotman.models import (
    SelectorKind,
    package_ref_text,
)
from dotman.package_resolution import (
    parse_full_spec_selector_text,
    parse_package_ref_text,
)
from dotman.printable import terminal_safe_line
from dotman.resolver import (
    ResolverOption,
    build_package_field_kinds,
    build_package_match_fields,
    build_selector_field_kinds,
    build_selector_match_fields,
    resolve_match,
)
from dotman.repository import Repository
from dotman.snapshot import (
    RestoreAction,
    SnapshotRecord,
    find_snapshot_matches,
)
from dotman.terminal import ESCAPE_INPUT, colors_enabled as terminal_colors_enabled
from dotman.ui_context import current_ui_config

MENU_HEADER_MARKER = cli_style.MENU_HEADER_MARKER
MENU_HEADER_MARKER_STYLE = cli_style.MENU_HEADER_MARKER_STYLE
MENU_INDEX_STYLE = cli_style.MENU_INDEX_STYLE
MENU_PROMPT_STYLE = cli_style.MENU_PROMPT_STYLE
MENU_HINT_STYLE = cli_style.MENU_HINT_STYLE
MENU_ACTION_STYLE_BY_NAME = cli_style.MENU_ACTION_STYLE_BY_NAME


def colors_enabled() -> bool:
    return terminal_colors_enabled(sys.stdout)


def style_text(text: str, *codes: str) -> str:
    return cli_style.style_text(text, *codes)


def repo_name_from_selection_label(selection_label: str) -> str:
    return cli_style.repo_name_from_selection_label(selection_label)


def render_package_label(
    *,
    repo_name: str,
    package_id: str,
    bound_profile: str | None = None,
    target_name: str | None = None,
    package_first: bool = False,
    include_repo_context: bool = False,
) -> str:
    return cli_style.render_package_label(
        repo_name=repo_name,
        package_id=package_id,
        bound_profile=bound_profile,
        target_name=target_name,
        package_first=package_first,
        include_repo_context=include_repo_context,
        use_color=colors_enabled(),
    )


def render_package_target_label(*, repo_name: str, package_id: str, target_name: str, bound_profile: str | None = None) -> str:
    return cli_style.render_package_target_label(
        repo_name=repo_name,
        package_id=package_id,
        target_name=target_name,
        bound_profile=bound_profile,
        use_color=colors_enabled(),
    )


def open_editor_path(path: Path, *, missing_editor_label: str = "path") -> int:
    command = editor_command([str(path)])
    if command is None:
        print(f"No editor configured. {missing_editor_label}: {terminal_safe_line(str(path))}")
        return 0

    try:
        result = current_command_runtime().run(
            CommandRequest(
                command=command,
                io="tty",
            )
        )
    except FileNotFoundError as exc:
        raise ValueError("editor command was not found") from exc
    return result.exit_code


def resolve_variable_text(
    engine: DotmanEngine,
    variable_text: str,
    *,
    interaction: Interaction | None,
) -> str:
    query_text = variable_text.strip().removeprefix("vars.")
    exact_matches, partial_matches = engine.find_variable_matches(variable_text)
    return resolve_match(
        interaction,
        exact_matches=exact_matches,
        partial_matches=partial_matches,
        query_text=query_text,
        exact_header_text=f"Select a variable for '{variable_text}':",
        partial_header_text=f"Select a variable for '{variable_text}':",
        option_resolver=lambda match: ResolverOption(
            display_label=match,
            match_fields=(match,),
            field_kinds=("variable",),
        ),
        value_resolver=lambda match: match,
        exact_error_text=f"variable '{variable_text}' is ambiguous: " + ", ".join(exact_matches),
        partial_error_text=f"variable '{variable_text}' is ambiguous: " + ", ".join(partial_matches),
        not_found_text=f"variable '{variable_text}' did not match any resolved variable",
    )


def ui_full_paths_enabled() -> bool:
    ui_config = current_ui_config()
    if ui_config is not None:
        return ui_config.full_paths
    return False


def build_selector_match_display_fields(*, repo_name: str, selector: str, selector_kind: str) -> tuple[str, ...]:
    return cli_style.build_selector_match_display_fields(
        repo_name=repo_name,
        selector=selector,
        selector_kind=selector_kind,
        use_color=colors_enabled(),
    )


def render_selector_match_label(*, repo_name: str, selector: str, selector_kind: str) -> str:
    return cli_style.render_selector_match_label(
        repo_name=repo_name,
        selector=selector,
        selector_kind=selector_kind,
        use_color=colors_enabled(),
    )


def _effective_full_paths(full_paths: bool | None) -> bool:
    if full_paths is not None:
        return full_paths
    return ui_full_paths_enabled()


def review_menu_prompt() -> str:
    prompt_text = "Review command"
    hint_text = '("?", number, "n", "a", "l", "s", Esc; default: next)'
    if not colors_enabled():
        return f"\n{prompt_text} {hint_text}: "
    return (
        f"\n{style_text(MENU_HEADER_MARKER, *MENU_HEADER_MARKER_STYLE)} "
        f"{style_text(prompt_text, *MENU_PROMPT_STYLE)} "
        f"{style_text(hint_text, *MENU_HINT_STYLE)}: "
    )


def review_continue_prompt() -> str:
    prompt_text = "Continue?"
    hint_text = "[Y/n]"
    if not colors_enabled():
        return f"{prompt_text} {hint_text} "
    return (
        f"{style_text(MENU_HEADER_MARKER, *MENU_HEADER_MARKER_STYLE)} "
        f"{style_text(prompt_text, *MENU_PROMPT_STYLE)} "
        f"{style_text(hint_text, *MENU_HINT_STYLE)} "
    )


def print_review_command_help() -> None:
    print("Review commands:")
    print("  <number>   inspect one diff")
    print("  n          inspect next diff")
    print("  a          inspect all diffs")
    print("  l, list    list review items")
    print("  s, skip    skip remaining review")
    print("  Esc        abort")
    print("  ?          show this help")


def parse_review_command(raw_answer: str, item_count: int) -> tuple[str, int | None]:
    answer = raw_answer.strip().lower()
    if not answer or answer == "n":
        return "next", None
    if answer in {"s", "skip"}:
        return "skip_review", None
    if answer in {"l", "list"}:
        return "list", None
    if answer == "?":
        return "help", None
    if answer == "a":
        return "all", None
    if answer == ESCAPE_INPUT:
        return "abort", None
    if answer.isdigit():
        selected_index = parse_selection_index(answer, item_count)
        return "inspect", selected_index - 1
    raise ValueError(f"unsupported review command: {raw_answer.strip()}")


def resolve_tracked_package_text(
    engine: DotmanEngine,
    package_text: str,
    *,
    interaction: Interaction | None,
) -> tuple[Repository, str, str | None]:
    explicit_repo, selector, bound_profile = parse_package_ref_text(package_text)
    package_query = package_ref_text(package_id=selector, bound_profile=bound_profile)
    lookup_package_text = f"{explicit_repo}:{package_query}" if explicit_repo is not None else package_query
    selector, bound_profile, exact_matches, partial_matches = engine.find_tracked_package_matches(lookup_package_text)
    if not exact_matches and not partial_matches:
        rejection = engine.stale_tracked_row_rejection(
            subject_text=f"tracked package '{package_text}'",
            repo_name=explicit_repo,
            package_id=selector,
            profile=bound_profile,
        )
        if rejection is not None:
            raise UsageError(rejection)
    return resolve_match(
        interaction,
        exact_matches=exact_matches,
        partial_matches=partial_matches,
        query_text=package_query,
        exact_header_text=f"Select a tracked package for '{package_query}':",
        partial_header_text=f"Select a tracked package for '{package_query}':",
        option_resolver=lambda match: ResolverOption(
            display_label=render_package_label(
                repo_name=match[0].config.name,
                package_id=match[1],
                bound_profile=match[2],
                package_first=True,
                include_repo_context=True,
            ),
            match_fields=build_package_match_fields(
                repo_name=match[0].config.name,
                package_id=match[1],
                bound_profile=match[2],
            ),
            field_kinds=build_package_field_kinds(has_bound_profile=match[2] is not None),
        ),
        value_resolver=lambda match: (
            f"{match[0].config.name}:{package_ref_text(package_id=match[1], bound_profile=match[2])}"
        ),
        exact_error_text=(
            (
                f"tracked package '{package_query}' is defined in multiple repos: "
                if len({repo.config.name for repo, _package_id, _match_bound_profile in exact_matches}) > 1
                else f"tracked package '{package_query}' is ambiguous: "
            )
            + ", ".join(
                f"{repo.config.name}:{package_ref_text(package_id=package_id, bound_profile=match_bound_profile)}"
                for repo, package_id, match_bound_profile in exact_matches
            )
        ),
        partial_error_text=f"tracked package '{package_query}' is ambiguous: "
        + ", ".join(
            f"{repo.config.name}:{package_ref_text(package_id=package_id, bound_profile=match_bound_profile)}"
            for repo, package_id, match_bound_profile in partial_matches
        ),
        not_found_text=f"tracked package '{package_query}' did not match any tracked package",
    )


def resolve_trackable_selector_text(
    engine: DotmanEngine,
    query_text: str,
    *,
    interaction: Interaction | None,
) -> tuple[Repository, str, SelectorKind]:
    explicit_repo, selector, selector_profile = parse_full_spec_selector_text(query_text)
    if selector_profile is not None:
        raise UsageError("trackable lookup does not accept selector@profile syntax")
    exact_matches, partial_matches = engine.find_selector_matches(selector, explicit_repo)
    return resolve_match(
        interaction,
        exact_matches=exact_matches,
        partial_matches=partial_matches,
        query_text=selector,
        exact_header_text=f"Select a package or group for '{selector}':",
        partial_header_text=f"Select a package or group for '{selector}':",
        option_resolver=lambda match: ResolverOption(
            display_label=render_selector_match_label(
                repo_name=match[0].config.name,
                selector=match[1],
                selector_kind=match[2],
            ),
            display_fields=build_selector_match_display_fields(
                repo_name=match[0].config.name,
                selector=match[1],
                selector_kind=match[2],
            ),
            match_fields=build_selector_match_fields(
                repo_name=match[0].config.name,
                selector=match[1],
            ),
            field_kinds=build_selector_field_kinds(),
        ),
        value_resolver=lambda match: f"{match[0].config.name}:{match[1]}",
        exact_error_text=f"selector '{selector}' is defined in multiple repos: "
        + ", ".join(f"{repo.config.name}:{match}" for repo, match, _ in exact_matches),
        partial_error_text=f"selector '{selector}' is ambiguous: "
        + ", ".join(f"{repo.config.name}:{match}" for repo, match, _ in partial_matches),
        not_found_text=f"selector '{selector}' did not match any package or group",
    )


def render_review_item_label(item: ReviewItem, *, full_paths: bool, use_color: bool) -> str:
    # Snapshot manifests may omit an entry's provenance; its live path still identifies it.
    if item.selection_label is None or item.package_id is None or item.target_name is None:
        return display_cli_path(item.live_path, full_paths=full_paths)
    return cli_style.render_package_target_label(
        repo_name=repo_name_from_selection_label(item.selection_label),
        package_id=item.package_id,
        target_name=item.target_name,
        bound_profile=item.bound_profile,
        use_color=use_color,
    )


def print_review_item(index: int, item: ReviewItem, *, full_paths: bool | None = None) -> None:
    full_paths = _effective_full_paths(full_paths)
    use_color = colors_enabled()
    item_label = render_review_item_label(item, full_paths=full_paths, use_color=use_color)
    diff_badge = "[diff unavailable]" if item.diff_unavailable_reason is not None else None
    source_path = display_cli_path(item.source_path, full_paths=full_paths)
    destination_path = display_cli_path(item.destination_path, full_paths=full_paths)
    if not use_color:
        item_text = f"[{item.action}] {item_label}"
        if diff_badge is not None:
            item_text += f" {diff_badge}"
        item_text += f": {source_path} -> {destination_path}"
        print(f"  {index:>2}) {item_text}")
        return

    action_style = MENU_ACTION_STYLE_BY_NAME.get(item.action, ("1",))
    action_text = style_text(f"[{item.action}]", *action_style)
    badge_text = f" {style_text(diff_badge, *MENU_HINT_STYLE)}" if diff_badge is not None else ""
    arrow_text = style_text("->", *MENU_HINT_STYLE)
    print(
        f"  {style_text(f'{index:>2})', *MENU_INDEX_STYLE)} "
        f"{action_text} {item_label}{badge_text}: "
        f"{source_path} {arrow_text} {destination_path}"
    )


def review_diff_header(review_item: ReviewItem, *, index: int, total: int, full_paths: bool | None = None) -> str:
    item_label = render_review_item_label(review_item, full_paths=_effective_full_paths(full_paths), use_color=False)
    return f"Diff {index}/{total}: {item_label} [{review_item.action}]"


def print_review_diff_header(
    review_item: ReviewItem,
    *,
    index: int,
    total: int,
    full_paths: bool | None = None,
) -> None:
    full_paths = _effective_full_paths(full_paths)
    path_text = review_diff_path_line(review_item, full_paths=full_paths)
    separator = "-" * 5
    if not colors_enabled():
        header_text = review_diff_header(review_item, index=index, total=total, full_paths=full_paths)
        print()
        print(f"{separator} {header_text} {separator}")
        print(path_text)
        return
    prefix_text = style_text(f"Diff {index}/{total}:", *MENU_HINT_STYLE)
    item_label = render_review_item_label(review_item, full_paths=full_paths, use_color=True)
    action_text = style_text(f"[{review_item.action}]", *MENU_ACTION_STYLE_BY_NAME.get(review_item.action, ("1",)))
    print()
    print(
        f"{style_text(separator, *MENU_HINT_STYLE)} "
        f"{prefix_text} {item_label} {action_text} "
        f"{style_text(separator, *MENU_HINT_STYLE)}"
    )
    print(style_text(path_text, *MENU_HINT_STYLE))


def review_diff_path_line(review_item: ReviewItem, *, full_paths: bool | None = None) -> str:
    full_paths = _effective_full_paths(full_paths)
    destination_path = display_cli_path(review_item.destination_path, full_paths=full_paths)
    return f"file: {destination_path}"


def review_diff_footer(*, index: int, total: int) -> str:
    return f"End Diff {index}/{total}"


def print_review_diff_footer(*, index: int, total: int) -> None:
    footer_text = review_diff_footer(index=index, total=total)
    separator = "-" * 5
    if not colors_enabled():
        print(f"{separator} {footer_text} {separator}")
        return
    print(
        f"{style_text(separator, *MENU_HINT_STYLE)} "
        f"{style_text(footer_text, *MENU_HINT_STYLE)} "
        f"{style_text(separator, *MENU_HINT_STYLE)}"
    )


def print_review_menu_items(
    review_items: Sequence[ReviewItem],
    *,
    operation: str,
    full_paths: bool | None = None,
) -> None:
    full_paths = _effective_full_paths(full_paths)
    print()
    print(cli_style.render_menu_header(f"Review pending diffs for {operation}:", use_color=colors_enabled()))
    for index, item in enumerate(review_items, start=1):
        print_review_item(index, item, full_paths=full_paths)


def run_diff_review_menu(
    review_items: Sequence[ReviewItem],
    *,
    interaction: Interaction,
    operation: str,
    full_paths: bool | None = None,
) -> bool:
    full_paths = _effective_full_paths(full_paths)
    print_review_menu_items(review_items, operation=operation, full_paths=full_paths)

    last_viewed_index: int | None = None
    while True:
        try:
            command_name, selected_index = parse_review_command(
                interaction.read_text(TextInputRequest(prompt=review_menu_prompt(), escape_result=ESCAPE_INPUT)),
                len(review_items),
            )
        except ValueError as exc:
            print(f"invalid selection: {exc}", file=sys.stderr)
            continue

        if command_name == "help":
            print_review_command_help()
            continue
        if command_name == "list":
            print_review_menu_items(review_items, operation=operation, full_paths=full_paths)
            continue
        if command_name == "skip_review":
            return True
        if command_name == "abort":
            return False
        if command_name == "all":
            for item_index, item in enumerate(review_items, start=1):
                try:
                    print_review_diff_header(item, index=item_index, total=len(review_items), full_paths=full_paths)
                    run_review_item_diff(item)
                    print_review_diff_footer(index=item_index, total=len(review_items))
                except ValueError as exc:
                    print(f"review unavailable: {exc}", file=sys.stderr)
            if review_items:
                last_viewed_index = len(review_items) - 1
            continue
        if command_name == "next":
            selected_index = 0 if last_viewed_index is None else last_viewed_index + 1
            if selected_index >= len(review_items):
                if interaction.confirm(ConfirmationRequest(prompt=review_continue_prompt(), default=True)):
                    return True
                continue
        if selected_index is None:
            print("invalid selection: missing review item", file=sys.stderr)
            continue
        if command_name in {"inspect", "next"}:
            last_viewed_index = selected_index
            try:
                print_review_diff_header(
                    review_items[selected_index],
                    index=selected_index + 1,
                    total=len(review_items),
                    full_paths=full_paths,
                )
                run_review_item_diff(review_items[selected_index])
                print_review_diff_footer(index=selected_index + 1, total=len(review_items))
            except ValueError as exc:
                print(f"review unavailable: {exc}", file=sys.stderr)
            continue


def display_cli_path(reference_path: Path | str, *, full_paths: bool) -> str:
    return cli_emit.display_cli_path(reference_path, full_paths=full_paths)


def resolve_snapshot_record(
    snapshot_root: Path,
    snapshot_ref: str | None,
    *,
    interaction: Interaction | None,
) -> SnapshotRecord:
    matches = find_snapshot_matches(snapshot_root, snapshot_ref)
    if not matches:
        if snapshot_ref is None or snapshot_ref == "latest":
            raise ValueError("no snapshots are available")
        raise UsageError(f"snapshot '{snapshot_ref}' did not match any available snapshot")
    if len(matches) == 1:
        return matches[0]
    if interaction is None:
        raise InteractionRequiredError(
            f"snapshot '{snapshot_ref}' is ambiguous: " + ", ".join(snapshot.snapshot_id for snapshot in matches)
        )
    matches_by_id = {snapshot.snapshot_id: snapshot for snapshot in matches}
    selected_snapshot_id = interaction.choose(
        ChoiceRequest(
            header_text=f"Select a snapshot for '{snapshot_ref}':",
            options=tuple(
                ChoiceOption(
                    value=snapshot.snapshot_id,
                    label=(
                        f"{snapshot.snapshot_id} [{snapshot.status}] "
                        f"({snapshot.entry_count} path{'s' if snapshot.entry_count != 1 else ''})"
                    ),
                )
                for snapshot in matches
            ),
        )
    )
    return matches_by_id[selected_snapshot_id]


build_restore_review_items = cli_emit.build_restore_review_items


def review_restore_actions_for_interactive_diffs(
    *,
    snapshot: SnapshotRecord,
    actions: Sequence[RestoreAction],
    interaction: Interaction | None,
    full_paths: bool | None = None,
) -> bool:
    full_paths = _effective_full_paths(full_paths)
    if interaction is None:
        return True
    review_items = build_restore_review_items(snapshot, actions)
    if not review_items:
        return True
    return run_diff_review_menu(review_items, interaction=interaction, operation="restore", full_paths=full_paths)


@dataclass(frozen=True)
class InspectionRuntime:
    interaction: Interaction | None

    def _interaction_for(self, *, json_output: bool) -> Interaction | None:
        return None if json_output else self.interaction

    def resolve_tracked_package_text(
        self,
        engine: DotmanEngine,
        package_text: str,
        *,
        json_output: bool,
    ) -> tuple[Repository, str, str | None]:
        return resolve_tracked_package_text(engine, package_text, interaction=self._interaction_for(json_output=json_output))

    def resolve_trackable_selector_text(
        self,
        engine: DotmanEngine,
        query_text: str,
        *,
        json_output: bool,
    ) -> tuple[Repository, str, SelectorKind]:
        return resolve_trackable_selector_text(engine, query_text, interaction=self._interaction_for(json_output=json_output))

    def resolve_variable_text(
        self,
        engine: DotmanEngine,
        variable_text: str,
        *,
        json_output: bool,
    ) -> str:
        return resolve_variable_text(engine, variable_text, interaction=self._interaction_for(json_output=json_output))

    def resolve_snapshot_record(
        self,
        snapshot_root: Path,
        snapshot_ref: str | None,
        *,
        json_output: bool,
    ) -> SnapshotRecord:
        return resolve_snapshot_record(snapshot_root, snapshot_ref, interaction=self._interaction_for(json_output=json_output))


@dataclass(frozen=True)
class StateRuntime:
    interaction: Interaction | None

    def add_editor_available(self) -> bool:
        return resolve_editor_command() is not None

    def review_add_manifest(self, result: AddOperationResult) -> AddReviewResult | None:
        return review_add_manifest(result)

    def open_editor_path(self, *, path: Path, missing_editor_label: str) -> int:
        return open_editor_path(path, missing_editor_label=missing_editor_label)

    def emit_resolution_error(self, error: ValueError) -> None:
        cli_emit.emit_error(
            error,
            use_color=terminal_colors_enabled(sys.stderr),
        )

    def emit_resolution_message(self, message: str) -> None:
        sys.stdout.write(message)
