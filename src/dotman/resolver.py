from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar


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
