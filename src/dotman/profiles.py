from __future__ import annotations

from dotman.errors import UsageError
from collections.abc import Mapping, Sequence


def compute_profile_heights(profiles: Mapping[str, Sequence[str]]) -> dict[str, int]:
    cache: dict[str, int] = {}

    def visit(profile_name: str, stack: tuple[str, ...]) -> int:
        if profile_name in cache:
            return cache[profile_name]
        if profile_name in stack:
            cycle = " -> ".join([*stack, profile_name])
            raise UsageError(f"profile include cycle detected: {cycle}")
        includes = tuple(profiles.get(profile_name, ()))
        if not includes:
            cache[profile_name] = 0
            return 0
        height = 1 + max(visit(include_name, (*stack, profile_name)) for include_name in includes)
        cache[profile_name] = height
        return height

    for profile_name in profiles:
        visit(profile_name, ())
    return cache


def rank_profiles(profiles: Mapping[str, Sequence[str]]) -> list[str]:
    heights = compute_profile_heights(profiles)
    return sorted(profiles, key=lambda name: (-heights[name], name))
