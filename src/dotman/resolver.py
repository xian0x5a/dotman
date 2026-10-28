from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from dotman import cli_style
from dotman.errors import UsageError
from dotman.interaction import ChoiceOption, ChoiceRequest, Interaction
from dotman.interaction_policy import InteractionRequiredError

Match = TypeVar("Match")


# `@profile` is intentionally a weaker fallback field, not a peer selector segment.
SEGMENT_SEPARATOR_PATTERN = re.compile(r"[/:]")


@dataclass(frozen=True)
class ResolverOption:
    display_label: str
    match_fields: tuple[str, ...]
    field_kinds: tuple[str, ...] | None = None
    display_fields: tuple[str, ...] | None = None


def normalize_match_fields(*fields: str) -> tuple[str, ...]:
    normalized_fields: list[str] = []
    for field in fields:
        normalized_field = field.strip()
        if not normalized_field or normalized_field in normalized_fields:
            continue
        normalized_fields.append(normalized_field)
    if not normalized_fields:
        raise ValueError("resolver candidates require at least one match field")
    return tuple(normalized_fields)


def normalize_field_kinds(*field_kinds: str) -> tuple[str, ...]:
    if not field_kinds:
        raise ValueError("resolver candidates require at least one field kind")
    return tuple(field_kinds)


def build_selector_match_fields(*, repo_name: str, selector: str) -> tuple[str, ...]:
    return normalize_match_fields(
        selector,
        f"{repo_name}:{selector}",
    )


def build_selector_field_kinds() -> tuple[str, ...]:
    return normalize_field_kinds(
        "selector",
        "selector",
    )


def build_full_spec_selector_match_fields(*, repo_name: str, selector: str, profile: str) -> tuple[str, ...]:
    return normalize_match_fields(
        selector,
        f"{repo_name}:{selector}",
        f"{selector}@{profile}",
        f"{repo_name}:{selector}@{profile}",
    )


def build_full_spec_selector_field_kinds() -> tuple[str, ...]:
    return normalize_field_kinds(
        "selector",
        "selector",
        "profile",
        "profile",
    )


def build_package_match_fields(
    *,
    repo_name: str,
    package_id: str,
    bound_profile: str | None = None,
) -> tuple[str, ...]:
    package_ref = package_id if bound_profile is None else f"{package_id}<{bound_profile}>"
    if bound_profile is None:
        return normalize_match_fields(
            package_id,
            f"{repo_name}:{package_id}",
        )
    return normalize_match_fields(
        package_id,
        package_ref,
        f"{repo_name}:{package_id}",
        f"{repo_name}:{package_ref}",
        bound_profile,
    )


def build_package_field_kinds(*, has_bound_profile: bool = False) -> tuple[str, ...]:
    if not has_bound_profile:
        return normalize_field_kinds(
            "selector",
            "selector",
        )
    return normalize_field_kinds(
        "selector",
        "selector",
        "selector",
        "selector",
        "profile",
    )


def build_target_match_fields(
    *,
    repo_name: str,
    package_id: str,
    target_name: str,
    bound_profile: str | None = None,
) -> tuple[str, ...]:
    package_ref = package_id if bound_profile is None else f"{package_id}<{bound_profile}>"
    if bound_profile is None:
        return normalize_match_fields(
            target_name,
            f"{package_ref}.{target_name}",
            f"{repo_name}:{package_ref}.{target_name}",
        )
    return normalize_match_fields(
        target_name,
        f"{package_ref}.{target_name}",
        f"{repo_name}:{package_ref}.{target_name}",
        bound_profile,
    )


def build_target_field_kinds(*, has_bound_profile: bool = False) -> tuple[str, ...]:
    if not has_bound_profile:
        return normalize_field_kinds(
            "selector",
            "selector",
            "selector",
        )
    return normalize_field_kinds(
        "selector",
        "selector",
        "selector",
        "profile",
    )


def build_profile_match_fields(*, profile: str) -> tuple[str, ...]:
    return normalize_match_fields(profile)


def build_profile_field_kinds() -> tuple[str, ...]:
    return normalize_field_kinds("profile")


def build_profile_option(profile: str) -> ResolverOption:
    return ResolverOption(
        display_label=profile,
        match_fields=build_profile_match_fields(profile=profile),
        field_kinds=build_profile_field_kinds(),
    )


def build_selector_option(
    *,
    repo_name: str,
    selector: str,
    selector_kind: str,
    use_color: bool,
) -> ResolverOption:
    display_fields = cli_style.build_selector_match_display_fields(
        repo_name=repo_name,
        selector=selector,
        selector_kind=selector_kind,
        use_color=use_color,
    )
    return ResolverOption(
        display_label=cli_style.join_menu_display_fields(*display_fields),
        display_fields=display_fields,
        match_fields=build_selector_match_fields(repo_name=repo_name, selector=selector),
        field_kinds=build_selector_field_kinds(),
    )


def _segment_match_rank(query: str, text: str) -> tuple[int, int, int, int] | None:
    normalized_query = query.strip().lower()
    normalized_text = text.lower()
    if not normalized_query:
        return (0, 0, 0, len(normalized_text))
    segments = SEGMENT_SEPARATOR_PATTERN.split(normalized_text)
    best_rank: tuple[int, int, int, int] | None = None
    for segment_index, segment in enumerate(segments):
        match_index = segment.find(normalized_query)
        if match_index == -1:
            continue
        candidate_rank = (segment_index, match_index, len(segment), len(normalized_text))
        if best_rank is None or candidate_rank < best_rank:
            best_rank = candidate_rank
    return best_rank


def _simple_match_rank(query: str, text: str) -> tuple[int, int, int, int] | None:
    normalized_query = query.strip().lower()
    normalized_text = text.lower()
    if not normalized_query:
        return (0, 0, 0, len(normalized_text))
    if normalized_text == normalized_query:
        return (0, 0, 0, len(normalized_text))
    if normalized_text.startswith(normalized_query):
        return (1, 0, 0, len(normalized_text))
    match_index = normalized_text.find(normalized_query)
    if match_index == -1:
        return None
    return (2, match_index, 0, len(normalized_text))


def _field_match_rank(*, kind: str, query: str, text: str) -> tuple[int, int, int, int] | None:
    if kind == "profile":
        return _simple_match_rank(query, text)
    return _segment_match_rank(query, text)


def rank_resolver_option(
    *,
    query: str,
    option: ResolverOption,
) -> tuple[int, int, int, int, int, str, str]:
    field_kinds = option.field_kinds or ("selector",) * len(option.match_fields)
    if len(field_kinds) != len(option.match_fields):
        raise ValueError("resolver option field kinds must align with match fields")
    for field_index, (match_field, field_kind) in enumerate(zip(option.match_fields, field_kinds, strict=True)):
        field_rank = _field_match_rank(kind=field_kind, query=query, text=match_field)
        if field_rank is None:
            continue
        return (field_index, *field_rank, match_field.lower(), option.display_label.lower())
    return (
        len(option.match_fields),
        999,
        999,
        999,
        999,
        "",
        option.display_label.lower(),
    )


def rank_resolver_matches(
    matches: Sequence[Match],
    *,
    query: str,
    option_resolver: Callable[[Match], ResolverOption],
) -> list[Match]:
    return sorted(
        matches,
        key=lambda match: rank_resolver_option(
            query=query,
            option=option_resolver(match),
        ),
    )



def resolve_match(
    interaction: Interaction | None,
    *,
    exact_matches: Sequence[Match],
    partial_matches: Sequence[Match],
    query_text: str,
    option_resolver: Callable[[Match], ResolverOption],
    value_resolver: Callable[[Match], str],
    exact_header_text: str,
    partial_header_text: str,
    not_found_text: str,
    exact_error_text: str | None = None,
    partial_error_text: str | None = None,
) -> Match:
    """Pick one match: a sole exact match wins, otherwise ask, or fail without an interaction.

    `value_resolver` gives each match's exact name, which keys the menu and is the
    name a non-interactive run is told to pass back.
    """
    ranked_exact = rank_resolver_matches(exact_matches, query=query_text, option_resolver=option_resolver)
    ranked_partial = rank_resolver_matches(partial_matches, query=query_text, option_resolver=option_resolver)
    if len(ranked_exact) == 1:
        return ranked_exact[0]
    if ranked_exact:
        return _choose_match(
            interaction,
            ranked_exact,
            header_text=exact_header_text,
            option_resolver=option_resolver,
            value_resolver=value_resolver,
            noninteractive_error=exact_error_text or _ambiguous_text(query_text, ranked_exact, value_resolver),
        )
    if ranked_partial:
        if interaction is None and len(ranked_partial) == 1:
            raise InteractionRequiredError(
                f"no exact match for '{query_text}'; use exact name '{value_resolver(ranked_partial[0])}'"
            )
        return _choose_match(
            interaction,
            ranked_partial,
            header_text=partial_header_text,
            option_resolver=option_resolver,
            value_resolver=value_resolver,
            noninteractive_error=partial_error_text or _ambiguous_text(query_text, ranked_partial, value_resolver),
        )
    raise UsageError(not_found_text)


def _ambiguous_text(query_text: str, matches: Sequence[Match], value_resolver: Callable[[Match], str]) -> str:
    return f"'{query_text}' is ambiguous: " + ", ".join(value_resolver(match) for match in matches)


def _choose_match(
    interaction: Interaction | None,
    matches: Sequence[Match],
    *,
    header_text: str,
    option_resolver: Callable[[Match], ResolverOption],
    value_resolver: Callable[[Match], str],
    noninteractive_error: str,
) -> Match:
    if interaction is None:
        raise InteractionRequiredError(noninteractive_error)
    matches_by_value = {value_resolver(match): match for match in matches}
    options = []
    for match in matches:
        option = option_resolver(match)
        options.append(
            ChoiceOption(
                value=value_resolver(match),
                label=option.display_label,
                display_fields=option.display_fields or (option.display_label,),
            )
        )
    selected_value = interaction.choose(ChoiceRequest(header_text=header_text, options=tuple(options)))
    return matches_by_value[selected_value]
