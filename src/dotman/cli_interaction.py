from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from dotman import cli_emit, cli_style
from dotman.add import (
    AddOperationResult,
    AddReviewResult,
    add_editor_available,
    review_add_manifest,
)
from dotman.command_runtime import (
    ArgvCommand,
    CommandRequest,
    current_command_runtime,
)
from dotman.diff_review import (
    ReviewItem,
    run_review_item_diff,
)
from dotman.engine import DotmanEngine
from dotman.interaction import Interaction
from dotman.interaction_policy import no_confirm_enabled
from dotman.models import (
    FullSpecSelector,
    SelectorKind,
    package_ref_text,
)
from dotman.package_resolution import (
    parse_full_spec_selector_text,
    parse_package_ref_text,
)
from dotman.resolver import (
    ResolverOption,
    build_package_field_kinds,
    build_package_match_fields,
    build_selector_field_kinds,
    build_selector_match_fields,
    parse_slash_qualified_query,
    rank_resolver_option,
)
from dotman.repository import Repository
from dotman.snapshot import (
    RestoreAction,
    SnapshotRecord,
    find_snapshot_matches,
)
from dotman.terminal import ESCAPE_INPUT, colors_enabled as terminal_colors_enabled, read_prompt_line
from dotman.ui_context import current_ui_config

MENU_HEADER_MARKER = cli_style.MENU_HEADER_MARKER
MENU_HEADER_MARKER_STYLE = cli_style.MENU_HEADER_MARKER_STYLE
MENU_INDEX_STYLE = cli_style.MENU_INDEX_STYLE
MENU_PROMPT_STYLE = cli_style.MENU_PROMPT_STYLE
MENU_HINT_STYLE = cli_style.MENU_HINT_STYLE
MENU_REPO_STYLE = cli_style.MENU_REPO_STYLE
MENU_ACTION_STYLE_BY_NAME = cli_style.MENU_ACTION_STYLE_BY_NAME
EXECUTION_STATUS_STYLE_BY_NAME = cli_style.EXECUTION_STATUS_STYLE_BY_NAME
MENU_SELECTION_OVERHEAD_LINES = 6
SelectableItem = TypeVar("SelectableItem")
SinglePartialResolverMode = Literal["confirm", "menu"]


def prompt(message: str, *, escape_result: str | None = None) -> str:
    return read_prompt_line(
        message,
        input_stream=sys.stdin,
        output_stream=sys.stdout,
        escape_result=escape_result,
    )


def colors_enabled() -> bool:
    return terminal_colors_enabled(sys.stdout)


def style_text(text: str, *codes: str) -> str:
    return cli_style.style_text(text, *codes)


def repo_name_from_selection_label(selection_label: str) -> str:
    return cli_style.repo_name_from_selection_label(selection_label)


def repo_qualified_selector_text(*, repo_name: str, selector: str) -> str:
    return cli_style.repo_qualified_selector_text(repo_name=repo_name, selector=selector)


def package_label_text(
    *,
    repo_name: str,
    package_id: str,
    bound_profile: str | None = None,
    target_name: str | None = None,
    package_first: bool = False,
    include_repo_context: bool = False,
) -> str:
    return cli_style.package_label_text(
        repo_name=repo_name,
        package_id=package_id,
        bound_profile=bound_profile,
        target_name=target_name,
        package_first=package_first,
        include_repo_context=include_repo_context,
    )


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


def package_profile_label_text(*, repo_name: str, package_id: str, profile: str) -> str:
    return cli_style.package_profile_label_text(repo_name=repo_name, package_id=package_id, profile=profile)


def render_package_profile_label(*, repo_name: str, package_id: str, profile: str) -> str:
    return cli_style.render_package_profile_label(
        repo_name=repo_name,
        package_id=package_id,
        profile=profile,
        use_color=colors_enabled(),
    )


def full_spec_selector_label_text(*, repo_name: str, selector: str, profile: str, selector_first: bool = False) -> str:
    return cli_style.full_spec_selector_label_text(
        repo_name=repo_name,
        selector=selector,
        profile=profile,
        selector_first=selector_first,
    )


def render_full_spec_selector_label(*, repo_name: str, selector: str, profile: str, selector_first: bool = False) -> str:
    return cli_style.render_full_spec_selector_label(
        repo_name=repo_name,
        selector=selector,
        profile=profile,
        selector_first=selector_first,
        use_color=colors_enabled(),
    )


def render_full_spec_selector_reference(binding: FullSpecSelector) -> str:
    return cli_style.render_full_spec_selector_reference(binding, use_color=colors_enabled())


def open_editor_path(path: Path, *, missing_editor_label: str = "path") -> int:
    if not add_editor_available():
        print(f"No editor configured. {missing_editor_label}: {path}")
        return 0

    editor_command = _resolve_editor_command()
    try:
        result = current_command_runtime().run(
            CommandRequest(
                command=ArgvCommand((*editor_command, str(path))),
                io="tty",
            )
        )
    except FileNotFoundError as exc:
        raise ValueError("editor command was not found") from exc
    return result.exit_code


def _resolve_editor_command() -> list[str]:
    editor_value = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor_value:
        raise ValueError("edit requires $VISUAL or $EDITOR")
    editor_command = [argument for argument in shlex.split(editor_value) if argument != "-d"]
    if not editor_command:
        raise ValueError("edit requires a non-empty $VISUAL or $EDITOR")
    return editor_command


def render_tracked_reason(reason: str) -> str:
    return cli_style.render_tracked_reason(reason, use_color=colors_enabled())


def render_tracked_state(state: str) -> str:
    return cli_style.render_tracked_state(state, use_color=colors_enabled())


def render_info_section_header(label: str) -> str:
    return cli_style.render_info_section_header(label, use_color=colors_enabled())


def resolve_variable_text(
    engine: DotmanEngine,
    variable_text: str,
    *,
    json_output: bool,
) -> str:
    query_text = variable_text.strip().removeprefix("vars.")
    exact_matches, partial_matches = engine.find_variable_matches(variable_text)
    return resolve_candidate_match(
        exact_matches=exact_matches,
        partial_matches=partial_matches,
        query_text=query_text,
        interactive=interactive_mode_enabled(json_output=json_output),
        exact_header_text=f"Select a variable for '{variable_text}':",
        partial_header_text=f"Select a variable for '{variable_text}':",
        option_resolver=lambda match: ResolverOption(
            display_label=match,
            match_fields=(match,),
            field_kinds=("variable",),
        ),
        exact_name_resolver=lambda match: match,
        exact_error_text=f"variable '{variable_text}' is ambiguous: " + ", ".join(exact_matches),
        partial_error_text=f"variable '{variable_text}' is ambiguous: " + ", ".join(partial_matches),
        not_found_text=f"variable '{variable_text}' did not match any resolved variable",
    )


def format_snapshot_timestamp(timestamp: str | None) -> str:
    return cli_style.format_snapshot_timestamp(timestamp)


def render_snapshot_status(status: str) -> str:
    return cli_style.render_snapshot_status(status, use_color=colors_enabled())


def render_snapshot_ref(snapshot_id: str) -> str:
    return cli_style.render_snapshot_ref(snapshot_id, use_color=colors_enabled())


def render_snapshot_metadata_label(label: str) -> str:
    return cli_style.render_snapshot_metadata_label(label, use_color=colors_enabled())


def render_snapshot_provenance(*, repo_name: str | None, package_id: str | None, target_name: str | None, selection_label: str | None) -> str | None:
    return cli_style.render_snapshot_provenance(
        repo_name=repo_name,
        package_id=package_id,
        target_name=target_name,
        selection_label=selection_label,
        use_color=colors_enabled(),
    )


def render_snapshot_reason(action: str) -> str:
    return cli_style.render_snapshot_reason(action, use_color=colors_enabled())


def render_menu_badge(text: str) -> str:
    return cli_style.render_menu_badge(text, use_color=colors_enabled())


def ui_full_paths_enabled() -> bool:
    ui_config = current_ui_config()
    if ui_config is not None:
        return ui_config.full_paths
    return False


def join_menu_display_fields(*fields: str) -> str:
    return cli_style.join_menu_display_fields(*fields)


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


def print_selection_header(header_text: str) -> None:
    print()
    if not colors_enabled():
        print(header_text)
        return
    print(
        f"{style_text(MENU_HEADER_MARKER, *MENU_HEADER_MARKER_STYLE)} "
        f"{style_text(header_text, '1')}"
    )


def print_selection_item(index: int, label: str) -> None:
    if not colors_enabled():
        print(f"  {index:>2}) {label}")
        return
    print(f"  {style_text(f'{index:>2})', *MENU_INDEX_STYLE)} {label}")


def parse_selection_index(raw_answer: str, item_count: int) -> int:
    answer = raw_answer.strip()
    if not answer:
        return 1
    if not answer.isdigit():
        raise ValueError(f"unsupported selection: {answer}")
    selected_index = int(answer)
    if not 1 <= selected_index <= item_count:
        raise ValueError(f"selection index out of range: {selected_index}")
    return selected_index


def _select_menu_option_with_prompt(*, header_text: str, option_labels: Sequence[str]) -> int:
    print_selection_header(header_text)
    indexed_labels = list(enumerate(option_labels, start=1))
    if ui_menus_bottom_up_enabled():
        indexed_labels.reverse()
    for index, option_label in indexed_labels:
        print_selection_item(index, option_label)
    while True:
        try:
            answer = prompt(selection_prompt())
            if answer.strip() == "?":
                print_selection_help()
                continue
            return parse_selection_index(answer, len(option_labels)) - 1
        except ValueError as exc:
            print(f"invalid selection: {exc}", file=sys.stderr)


def _fzf_available() -> bool:
    return shutil.which("fzf") is not None


def ui_menus_bottom_up_enabled() -> bool:
    raw_value = os.environ.get("DOTMAN_MENU_BOTTOM_UP")
    if raw_value is not None:
        return raw_value.strip().lower() not in {"0", "false", "no", "off"}
    ui_config = current_ui_config()
    if ui_config is not None:
        return ui_config.menus.bottom_up
    return True


def _effective_full_paths(full_paths: bool | None) -> bool:
    if full_paths is not None:
        return full_paths
    return ui_full_paths_enabled()


def _should_use_fzf_for_selection(option_labels: Sequence[str]) -> bool:
    terminal_lines = shutil.get_terminal_size((80, 24)).lines
    return len(option_labels) > max(1, terminal_lines - MENU_SELECTION_OVERHEAD_LINES)


def _select_menu_option_with_fzf(
    *,
    header_text: str,
    option_labels: Sequence[str],
    option_display_fields: Sequence[Sequence[str]] | None = None,
) -> int:
    if option_display_fields is not None and len(option_display_fields) != len(option_labels):
        raise ValueError("fzf display fields must align with option labels")
    display_fields_by_option = [
        tuple(field for field in fields if field)
        for fields in (option_display_fields or [(label,) for label in option_labels])
    ]
    entries = [
        " ".join([str(index), *display_fields])
        for index, display_fields in enumerate(display_fields_by_option, start=1)
    ]
    result = current_command_runtime().run(
        CommandRequest(
            command=ArgvCommand(
                (
                    "fzf",
                    "--prompt=Select> ",
                    f"--header={header_text}",
                    "--ansi",
                    "--wrap",
                    "--with-nth=2..",
                    "--accept-nth=1",
                    "--no-sort",
                )
            ),
            input=("\n".join(entries) + "\n").encode("utf-8"),
            # fzf reads its UI from the controlling terminal while selection
            # data uses pipes, so it must stay in Dotman's process group.
            isolate_process_group=False,
        )
    )
    if result.exit_code != 0:
        raise KeyboardInterrupt
    return parse_selection_index(result.stdout_text.strip(), len(option_labels)) - 1


def select_menu_option(
    *,
    header_text: str,
    option_labels: Sequence[str],
    option_display_fields: Sequence[Sequence[str]] | None = None,
) -> int:
    if _fzf_available() and _should_use_fzf_for_selection(option_labels):
        return _select_menu_option_with_fzf(
            header_text=header_text,
            option_labels=option_labels,
            option_display_fields=option_display_fields,
        )
    return _select_menu_option_with_prompt(header_text=header_text, option_labels=option_labels)


def selection_prompt() -> str:
    prompt_text = "Select a number"
    hint_text = '("?"; default: 1)'
    if not colors_enabled():
        return f"{prompt_text} {hint_text}: "
    return (
        f"{style_text(MENU_HEADER_MARKER, *MENU_HEADER_MARKER_STYLE)} "
        f"{style_text(prompt_text, *MENU_PROMPT_STYLE)} "
        f"{style_text(hint_text, *MENU_HINT_STYLE)}: "
    )


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


def partial_match_confirmation_prompt(*, candidate_label: str) -> str:
    prompt_text = f"Did you mean '{candidate_label}'?"
    hint_text = "[y/n]"
    if not colors_enabled():
        return f"{prompt_text} {hint_text} "
    return (
        f"{style_text(prompt_text, *MENU_PROMPT_STYLE)} "
        f"{style_text(hint_text, *MENU_HINT_STYLE)} "
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


def _prompt_yes_no(message: str, *, default: bool | None = None) -> bool:
    while True:
        answer = prompt(message).strip().lower()
        if answer == "" and default is not None:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("invalid confirmation: enter 'y' or 'n'", file=sys.stderr)


def confirm_review_continue(*, no_confirm: bool = False) -> bool:
    if no_confirm:
        return True
    return _prompt_yes_no(review_continue_prompt(), default=True)


def print_selection_help() -> None:
    print("Selection help:")
    print("  <number>  choose that item")


def print_review_command_help() -> None:
    print("Review commands:")
    print("  <number>   inspect one diff")
    print("  n          inspect next diff")
    print("  a          inspect all diffs")
    print("  l, list    list review items")
    print("  s, skip    skip remaining review")
    print("  Esc        abort")
    print("  ?          show this help")


def interactive_mode_enabled(*, json_output: bool) -> bool:
    return not no_confirm_enabled() and not json_output and sys.stdin.isatty()


def confirm_partial_candidate_match(*, candidate_label: str) -> bool:
    return _prompt_yes_no(partial_match_confirmation_prompt(candidate_label=candidate_label))


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


def resolve_candidate_match(
    *,
    exact_matches: Sequence[SelectableItem],
    partial_matches: Sequence[SelectableItem],
    query_text: str,
    interactive: bool,
    exact_header_text: str,
    partial_header_text: str,
    option_resolver: Callable[[SelectableItem], ResolverOption],
    exact_name_resolver: Callable[[SelectableItem], str],
    exact_error_text: str,
    partial_error_text: str,
    not_found_text: str,
    single_partial_mode: SinglePartialResolverMode = "menu",
    single_partial_error_text: str | None = None,
    rank_matches: bool = True,
) -> SelectableItem:
    if rank_matches:
        ranked_exact_matches = sorted(
            exact_matches,
            key=lambda match: rank_resolver_option(
                query=query_text,
                option=option_resolver(match),
            ),
        )
        ranked_partial_matches = sorted(
            partial_matches,
            key=lambda match: rank_resolver_option(
                query=query_text,
                option=option_resolver(match),
            ),
        )
    else:
        ranked_exact_matches = list(exact_matches)
        ranked_partial_matches = list(partial_matches)
    if len(exact_matches) == 1:
        return ranked_exact_matches[0]
    if len(exact_matches) > 1:
        if not interactive:
            raise ValueError(exact_error_text)
        resolved_options = [option_resolver(match) for match in ranked_exact_matches]
        selected_index = select_menu_option(
            header_text=exact_header_text,
            option_labels=[option.display_label for option in resolved_options],
            option_display_fields=[option.display_fields or (option.display_label,) for option in resolved_options],
        )
        return ranked_exact_matches[selected_index]
    if len(partial_matches) == 1:
        partial_match = ranked_partial_matches[0]
        partial_option = option_resolver(partial_match)
        if not interactive:
            if single_partial_error_text is not None:
                raise ValueError(single_partial_error_text)
            raise ValueError(
                f"no exact match for '{query_text}'; use exact name '{exact_name_resolver(partial_match)}'"
            )
        if single_partial_mode == "menu":
            selected_index = select_menu_option(
                header_text=partial_header_text,
                option_labels=[partial_option.display_label],
                option_display_fields=[partial_option.display_fields or (partial_option.display_label,)],
            )
            return ranked_partial_matches[selected_index]
        if single_partial_mode != "confirm":
            raise ValueError(f"unsupported single partial resolver mode: {single_partial_mode}")
        if not confirm_partial_candidate_match(candidate_label=partial_option.display_label):
            raise ValueError(f"confirmation required for partial match '{query_text}'")
        return partial_match
    if len(partial_matches) > 1:
        if not interactive:
            raise ValueError(partial_error_text)
        resolved_options = [option_resolver(match) for match in ranked_partial_matches]
        selected_index = select_menu_option(
            header_text=partial_header_text,
            option_labels=[option.display_label for option in resolved_options],
            option_display_fields=[option.display_fields or (option.display_label,) for option in resolved_options],
        )
        return ranked_partial_matches[selected_index]
    raise ValueError(not_found_text)


def resolve_tracked_package_text(
    engine: DotmanEngine,
    package_text: str,
    *,
    json_output: bool,
) -> tuple[Repository, str, str | None]:
    explicit_repo, selector, bound_profile = parse_package_ref_text(package_text)
    package_query = package_ref_text(package_id=selector, bound_profile=bound_profile)
    repo_names = [repo_config.name for repo_config in engine.config.ordered_repos]
    lookup_repo, lookup_selector = parse_slash_qualified_query(
        repo_names=repo_names,
        explicit_repo=explicit_repo,
        selector=selector,
    )
    lookup_package_ref = package_ref_text(package_id=lookup_selector, bound_profile=bound_profile)
    lookup_package_text = f"{lookup_repo}:{lookup_package_ref}" if lookup_repo is not None else lookup_package_ref
    selector, bound_profile, exact_matches, partial_matches = engine.find_tracked_package_matches(lookup_package_text)
    return resolve_candidate_match(
        exact_matches=exact_matches,
        partial_matches=partial_matches,
        query_text=package_query,
        interactive=interactive_mode_enabled(json_output=json_output),
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
        exact_name_resolver=lambda match: (
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
    json_output: bool,
) -> tuple[Repository, str, SelectorKind]:
    explicit_repo, selector, selector_profile = parse_full_spec_selector_text(query_text)
    if selector_profile is not None:
        raise ValueError("trackable lookup does not accept selector@profile syntax")
    repo_names = [repo_config.name for repo_config in engine.config.ordered_repos]
    lookup_repo, lookup_selector = parse_slash_qualified_query(
        repo_names=repo_names,
        explicit_repo=explicit_repo,
        selector=selector,
    )
    exact_matches, partial_matches = engine.find_selector_matches(lookup_selector, lookup_repo)
    return resolve_candidate_match(
        exact_matches=exact_matches,
        partial_matches=partial_matches,
        query_text=selector,
        interactive=interactive_mode_enabled(json_output=json_output),
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
        exact_name_resolver=lambda match: f"{match[0].config.name}:{match[1]}",
        exact_error_text=f"selector '{selector}' is defined in multiple repos: "
        + ", ".join(f"{repo.config.name}:{match}" for repo, match, _ in exact_matches),
        partial_error_text=f"selector '{selector}' is ambiguous: "
        + ", ".join(f"{repo.config.name}:{match}" for repo, match, _ in partial_matches),
        not_found_text=f"selector '{selector}' did not match any package or group",
    )


def print_review_item(index: int, item: ReviewItem, *, full_paths: bool | None = None) -> None:
    full_paths = _effective_full_paths(full_paths)
    repo_name = repo_name_from_selection_label(item.selection_label)
    bound_profile = getattr(item, "bound_profile", None)
    package_target = package_label_text(
        repo_name=repo_name,
        package_id=item.package_id,
        bound_profile=bound_profile,
        target_name=item.target_name,
    )
    if item.is_probe:
        if not colors_enabled():
            print(f"  {index:>2}) [{item.action}] {package_target} [probe]")
            return
        action_text = style_text(f"[{item.action}]", *MENU_ACTION_STYLE_BY_NAME.get(item.action, ("1",)))
        package_label = render_package_target_label(
            repo_name=repo_name,
            package_id=item.package_id,
            target_name=item.target_name,
            bound_profile=bound_profile,
        )
        probe_badge = cli_style.render_menu_badge("[probe]", use_color=True)
        print(
            f"  {style_text(f'{index:>2})', *MENU_INDEX_STYLE)} "
            f"{action_text} {package_label} {probe_badge}"
        )
        return

    diff_badge = "[diff unavailable]" if item.diff_unavailable_reason is not None else None
    source_path = display_cli_path(item.source_path, full_paths=full_paths)
    destination_path = display_cli_path(item.destination_path, full_paths=full_paths)
    if not colors_enabled():
        item_text = f"[{item.action}] {package_target}"
        if diff_badge is not None:
            item_text += f" {diff_badge}"
        item_text += f": {source_path} -> {destination_path}"
        print(f"  {index:>2}) {item_text}")
        return

    action_style = MENU_ACTION_STYLE_BY_NAME.get(item.action, ("1",))
    action_text = style_text(f"[{item.action}]", *action_style)
    package_label = render_package_target_label(
        repo_name=repo_name,
        package_id=item.package_id,
        target_name=item.target_name,
        bound_profile=bound_profile,
    )
    badge_text = f" {style_text(diff_badge, *MENU_HINT_STYLE)}" if diff_badge is not None else ""
    arrow_text = style_text("->", *MENU_HINT_STYLE)
    print(
        f"  {style_text(f'{index:>2})', *MENU_INDEX_STYLE)} "
        f"{action_text} {package_label}{badge_text}: "
        f"{source_path} {arrow_text} {destination_path}"
    )


def review_diff_header(review_item: ReviewItem, *, index: int, total: int) -> str:
    return (
        f"Diff {index}/{total}: "
        f"{package_label_text(
            repo_name=repo_name_from_selection_label(review_item.selection_label),
            package_id=review_item.package_id,
            bound_profile=review_item.bound_profile,
            target_name=review_item.target_name,
        )} "
        f"[{review_item.action}]"
    )


def print_review_diff_header(
    review_item: ReviewItem,
    *,
    index: int,
    total: int,
    full_paths: bool | None = None,
) -> None:
    full_paths = _effective_full_paths(full_paths)
    header_text = review_diff_header(review_item, index=index, total=total)
    path_text = review_diff_path_line(review_item, full_paths=full_paths)
    separator = "-" * 5
    if not colors_enabled():
        print()
        print(f"{separator} {header_text} {separator}")
        print(path_text)
        return
    repo_name = repo_name_from_selection_label(review_item.selection_label)
    prefix_text = style_text(f"Diff {index}/{total}:", *MENU_HINT_STYLE)
    package_label = render_package_target_label(
        repo_name=repo_name,
        package_id=review_item.package_id,
        target_name=review_item.target_name,
        bound_profile=review_item.bound_profile,
    )
    action_text = style_text(f"[{review_item.action}]", *MENU_ACTION_STYLE_BY_NAME.get(review_item.action, ("1",)))
    print()
    print(
        f"{style_text(separator, *MENU_HINT_STYLE)} "
        f"{prefix_text} {package_label} {action_text} "
        f"{style_text(separator, *MENU_HINT_STYLE)}"
    )
    print(style_text(path_text, *MENU_HINT_STYLE))


def review_diff_path_line(review_item: ReviewItem, *, full_paths: bool | None = None) -> str:
    full_paths = _effective_full_paths(full_paths)
    if review_item.is_probe:
        return "probe target: no files"
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
    print_selection_header(f"Review pending diffs for {operation}:")
    for index, item in enumerate(review_items, start=1):
        print_review_item(index, item, full_paths=full_paths)


def run_diff_review_menu(
    review_items: Sequence[ReviewItem],
    *,
    operation: str,
    full_paths: bool | None = None,
    no_confirm: bool = False,
) -> bool:
    full_paths = _effective_full_paths(full_paths)
    print_review_menu_items(review_items, operation=operation, full_paths=full_paths)

    last_viewed_index: int | None = None
    while True:
        try:
            command_name, selected_index = parse_review_command(
                prompt(review_menu_prompt(), escape_result=ESCAPE_INPUT),
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
                if confirm_review_continue(no_confirm=no_confirm):
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


def resolve_snapshot_record(snapshot_root: Path, snapshot_ref: str | None, *, json_output: bool) -> SnapshotRecord:
    matches = find_snapshot_matches(snapshot_root, snapshot_ref)
    if not matches:
        if snapshot_ref is None or snapshot_ref == "latest":
            raise ValueError("no snapshots are available")
        raise ValueError(f"snapshot '{snapshot_ref}' did not match any available snapshot")
    if len(matches) == 1:
        return matches[0]
    if not interactive_mode_enabled(json_output=json_output):
        raise ValueError(
            f"snapshot '{snapshot_ref}' is ambiguous: " + ", ".join(snapshot.snapshot_id for snapshot in matches)
        )
    selected_index = select_menu_option(
        header_text=f"Select a snapshot for '{snapshot_ref}':",
        option_labels=[
            f"{snapshot.snapshot_id} [{snapshot.status}] ({snapshot.entry_count} path{'s' if snapshot.entry_count != 1 else ''})"
            for snapshot in matches
        ],
    )
    return matches[selected_index]


visible_restore_actions = cli_emit.visible_restore_actions
build_restore_review_items = cli_emit.build_restore_review_items


def review_restore_actions_for_interactive_diffs(
    *,
    snapshot: SnapshotRecord,
    actions: Sequence[RestoreAction],
    json_output: bool,
    full_paths: bool | None = None,
    no_confirm: bool = False,
) -> bool:
    full_paths = _effective_full_paths(full_paths)
    if not interactive_mode_enabled(json_output=json_output):
        return True
    review_items = build_restore_review_items(snapshot, actions)
    if not review_items:
        return True
    return run_diff_review_menu(review_items, operation="restore", full_paths=full_paths, no_confirm=no_confirm)


@dataclass(frozen=True)
class InspectionRuntime:
    def resolve_tracked_package_text(
        self,
        engine: DotmanEngine,
        package_text: str,
        *,
        json_output: bool,
    ) -> tuple[Repository, str, str | None]:
        return resolve_tracked_package_text(engine, package_text, json_output=json_output)

    def resolve_trackable_selector_text(
        self,
        engine: DotmanEngine,
        query_text: str,
        *,
        json_output: bool,
    ) -> tuple[Repository, str, SelectorKind]:
        return resolve_trackable_selector_text(engine, query_text, json_output=json_output)

    def resolve_variable_text(
        self,
        engine: DotmanEngine,
        variable_text: str,
        *,
        json_output: bool,
    ) -> str:
        return resolve_variable_text(engine, variable_text, json_output=json_output)

    def resolve_snapshot_record(
        self,
        snapshot_root: Path,
        snapshot_ref: str | None,
        *,
        json_output: bool,
    ) -> SnapshotRecord:
        return resolve_snapshot_record(snapshot_root, snapshot_ref, json_output=json_output)


@dataclass(frozen=True)
class StateRuntime:
    interaction: Interaction | None

    def add_editor_available(self) -> bool:
        return add_editor_available()

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
