from __future__ import annotations

from dotman.resolver import (
    ResolverOption,
    build_full_spec_selector_field_kinds,
    build_full_spec_selector_match_fields,
    build_profile_field_kinds,
    build_profile_match_fields,
    build_selector_field_kinds,
    build_selector_match_fields,
    rank_resolver_option,
)


def test_rank_resolver_option_prefers_leftmost_selector_segment() -> None:
    sunshine = ResolverOption(
        display_label="sandbox/sunshine",
        match_fields=build_selector_match_fields(repo_name="sandbox", selector="sunshine"),
        field_kinds=build_selector_field_kinds(),
    )
    linux_meta = ResolverOption(
        display_label="sandbox/host/linux-meta",
        match_fields=build_selector_match_fields(repo_name="sandbox", selector="host/linux-meta"),
        field_kinds=build_selector_field_kinds(),
    )

    assert rank_resolver_option(query="s", option=sunshine) < rank_resolver_option(query="s", option=linux_meta)


def test_rank_resolver_option_treats_profile_matches_as_weaker_fallback() -> None:
    selector_hit = ResolverOption(
        display_label="sandbox/host/linux-meta@default",
        match_fields=build_full_spec_selector_match_fields(
            repo_name="sandbox",
            selector="host/linux-meta",
            profile="default",
        ),
        field_kinds=build_full_spec_selector_field_kinds(),
    )
    profile_only_hit = ResolverOption(
        display_label="sandbox/sunshine@host/linux",
        match_fields=build_full_spec_selector_match_fields(
            repo_name="sandbox",
            selector="sunshine",
            profile="host/linux",
        ),
        field_kinds=build_full_spec_selector_field_kinds(),
    )

    assert rank_resolver_option(query="linux", option=selector_hit) < rank_resolver_option(
        query="linux",
        option=profile_only_hit,
    )


def test_profile_ranking_uses_simple_match_instead_of_segment_scoring() -> None:
    profile_prefix = ResolverOption(
        display_label="linux/work",
        match_fields=build_profile_match_fields(profile="linux/work"),
        field_kinds=build_profile_field_kinds(),
    )
    profile_substring = ResolverOption(
        display_label="host/linux",
        match_fields=build_profile_match_fields(profile="host/linux"),
        field_kinds=build_profile_field_kinds(),
    )

    assert rank_resolver_option(query="linux", option=profile_prefix) < rank_resolver_option(
        query="linux",
        option=profile_substring,
    )
