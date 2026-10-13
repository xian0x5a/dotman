from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeAlias, TypeVar

from dotman import cli_style
from dotman.engine import DotmanEngine
from dotman.interaction import ChoiceOption, ChoiceRequest, Interaction
from dotman.models import FullSpecSelector, TrackedPackageDetail, package_ref_text
from dotman.package_resolution import parse_full_spec_selector_text
from dotman.resolver import (
    ResolverOption,
    build_full_spec_selector_field_kinds,
    build_full_spec_selector_match_fields,
    build_package_field_kinds,
    build_package_match_fields,
    build_profile_field_kinds,
    build_profile_match_fields,
    build_selector_field_kinds,
    build_selector_match_fields,
    parse_slash_qualified_query,
    rank_resolver_matches,
)

Match = TypeVar("Match")


@dataclass(frozen=True)
class UntrackEntryRequest:
    binding: FullSpecSelector


@dataclass(frozen=True)
class UntrackGroupRequest:
    repo: str
    selector: str
    selector_kind: str
    profile: str | None
    removal_bindings: tuple[FullSpecSelector, ...]

    @property
    def label(self) -> str:
        base_label = f"{self.repo}:{self.selector}"
        if self.profile is None:
            return base_label
        return f"{base_label}@{self.profile}"


UntrackRequest: TypeAlias = UntrackEntryRequest | UntrackGroupRequest


class UntrackResolver:
    def __init__(
        self,
        engine: DotmanEngine,
        *,
        interaction: Interaction | None = None,
        use_color: bool = False,
    ) -> None:
        self._engine = engine
        self._interaction = interaction
        self._use_color = use_color

    def resolve(self, binding_text: str) -> UntrackRequest:
        explicit_repo, selector, profile = parse_full_spec_selector_text(binding_text)
        _, _, exact_entries, partial_entries = (
            self._engine.find_persisted_tracked_package_entry_matches(binding_text)
        )
        if exact_entries:
            return self._resolve_entry(
                binding_text,
                exact_entries=exact_entries,
                partial_entries=partial_entries,
                package_matches=(),
            )
        package_matches, _owner_entries = (
            self._engine.find_tracked_package_matches_for_untrack(
                selector=selector,
                profile=profile,
                repo_name=explicit_repo,
            )
        )
        exact_groups, partial_groups = self._group_matches(
            explicit_repo=explicit_repo,
            selector=selector,
        )

        # Persisted entries include invalid and orphan state. An exact persisted
        # identity remains the strongest untrack request even when the catalog moved.
        if self._interaction is not None and (partial_entries or package_matches):
            return self._resolve_entry(
                binding_text,
                exact_entries=exact_entries,
                partial_entries=partial_entries,
                package_matches=package_matches,
            )
        if exact_groups:
            return self._resolve_group(
                selector=selector,
                selector_profile=profile,
                exact_matches=exact_groups,
                partial_matches=partial_groups,
            )
        if partial_entries or package_matches:
            return self._resolve_entry(
                binding_text,
                exact_entries=exact_entries,
                partial_entries=partial_entries,
                package_matches=package_matches,
            )
        if self._interaction is not None and partial_groups:
            return self._resolve_group(
                selector=selector,
                selector_profile=profile,
                exact_matches=exact_groups,
                partial_matches=partial_groups,
            )
        return self._resolve_entry(
            binding_text,
            exact_entries=exact_entries,
            partial_entries=partial_entries,
            package_matches=package_matches,
        )

    def remaining_tracked_package(
        self,
        binding: FullSpecSelector,
    ) -> TrackedPackageDetail | None:
        try:
            repo = self._engine.get_repo(binding.repo)
        except ValueError:
            return None
        if binding.selector not in repo.packages:
            return None
        if repo.resolve_package(binding.selector).binding_mode == "multi_instance":
            return None
        try:
            return self._engine.describe_tracked_package(
                f"{binding.repo}:{binding.selector}"
            )
        except ValueError:
            return None

    def _resolve_entry(
        self,
        binding_text: str,
        *,
        exact_entries: Sequence,
        partial_entries: Sequence,
        package_matches: Sequence,
    ) -> UntrackEntryRequest:
        explicit_repo, selector, profile = parse_full_spec_selector_text(binding_text)
        binding_label = selector if profile is None else f"{selector}@{profile}"
        filtered_package_matches = [
            package
            for package in package_matches
            if not any(
                record.package_entry.repo == package.repo
                and record.package_entry.selector == package.package_id
                for record in partial_entries
            )
        ]

        if self._interaction is not None and (
            exact_entries or partial_entries or filtered_package_matches
        ):
            selected_kind, selected_item = self._resolve_match(
                exact_matches=[("entry", record) for record in exact_entries],
                partial_matches=[("entry", record) for record in partial_entries]
                + [("package", package) for package in filtered_package_matches],
                query_text=binding_label,
                exact_header_text=(
                    f"Select a tracked package entry for '{binding_label}':"
                ),
                partial_header_text=(
                    f"Select an untrack target for '{binding_label}':"
                    if filtered_package_matches
                    else f"Select a tracked package entry for '{binding_label}':"
                ),
                option_resolver=lambda match: (
                    self._persisted_option(match[1])
                    if match[0] == "entry"
                    else self._package_option(match[1])
                ),
                value_resolver=lambda match: self._entry_choice_value(match),
                exact_error_text="unused",
                partial_error_text="unused",
                not_found_text=(
                    f"tracked package entry '{binding_label}' is not currently tracked"
                ),
            )
            if selected_kind == "entry":
                return UntrackEntryRequest(binding=selected_item.package_entry)
            raise self._package_owner_error(
                selected_item,
                explicit_repo=explicit_repo,
                profile=profile,
            )

        if len(exact_entries) == 1:
            return UntrackEntryRequest(binding=exact_entries[0].package_entry)
        if len(exact_entries) > 1:
            raise ValueError(
                f"tracked package entry '{binding_label}' is ambiguous: "
                + ", ".join(self._binding_text(record.package_entry) for record in exact_entries)
            )
        if partial_entries:
            if filtered_package_matches:
                package_candidates = ", ".join(
                    f"{package.repo}:{package.package_ref}"
                    for package in filtered_package_matches
                )
                raise ValueError(
                    f"tracked package entry '{binding_label}' is ambiguous: tracked packages: "
                    f"{package_candidates}"
                )
            if len(partial_entries) == 1:
                raise ValueError(
                    f"no exact match for '{binding_label}'; use exact name "
                    f"'{self._binding_text(partial_entries[0].package_entry)}'"
                )
            raise ValueError(
                f"tracked package entry '{binding_label}' is ambiguous: "
                + ", ".join(
                    self._binding_text(record.package_entry) for record in partial_entries
                )
            )
        if filtered_package_matches:
            if len(filtered_package_matches) > 1:
                raise ValueError(
                    f"tracked package entry '{binding_label}' is ambiguous: tracked packages: "
                    + ", ".join(
                        f"{package.repo}:{package.package_ref}"
                        for package in filtered_package_matches
                    )
                )
            raise self._package_owner_error(
                filtered_package_matches[0],
                explicit_repo=explicit_repo,
                profile=profile,
            )
        raise ValueError(
            f"tracked package entry '{binding_label}' is not currently tracked"
        )

    def _group_matches(
        self,
        *,
        explicit_repo: str | None,
        selector: str,
    ) -> tuple[list, list]:
        if explicit_repo is not None and explicit_repo not in self._engine.config.repos:
            return [], []
        repo_names = [repo_config.name for repo_config in self._engine.config.ordered_repos]
        lookup_repo, lookup_selector = parse_slash_qualified_query(
            repo_names=repo_names,
            explicit_repo=explicit_repo,
            selector=selector,
        )
        exact_matches, partial_matches = self._engine.find_selector_matches(
            lookup_selector,
            lookup_repo,
        )
        return (
            [match for match in exact_matches if match[2] == "group"],
            [match for match in partial_matches if match[2] == "group"],
        )

    def _resolve_group(
        self,
        *,
        selector: str,
        selector_profile: str | None,
        exact_matches: Sequence,
        partial_matches: Sequence,
    ) -> UntrackGroupRequest:
        repo, resolved_selector, selector_kind = self._resolve_match(
            exact_matches=exact_matches,
            partial_matches=partial_matches,
            query_text=selector,
            exact_header_text=f"Select a group for '{selector}':",
            partial_header_text=f"Select a group for '{selector}':",
            option_resolver=self._selector_option,
            value_resolver=lambda match: f"{match[0].config.name}:{match[1]}",
            exact_error_text=f"group '{selector}' is defined in multiple repos: "
            + ", ".join(
                f"{candidate_repo.config.name}:{match}"
                for candidate_repo, match, _ in exact_matches
            ),
            partial_error_text=f"group '{selector}' is ambiguous: "
            + ", ".join(
                f"{candidate_repo.config.name}:{match}"
                for candidate_repo, match, _ in partial_matches
            ),
            not_found_text=f"group '{selector}' did not match any tracked group",
        )
        group_package_ids = set(repo.expand_group(resolved_selector))
        tracked_group_bindings = tuple(
            binding
            for binding in self._engine.read_effective_tracked_package_entries(repo)
            if binding.repo == repo.config.name and binding.selector in group_package_ids
        )
        resolved_profile = self._resolve_group_profile(
            repo_name=repo.config.name,
            selector=resolved_selector,
            requested_profile=selector_profile,
            tracked_bindings=tracked_group_bindings,
        )
        removal_bindings = tuple(
            binding
            for binding in tracked_group_bindings
            if resolved_profile is None or binding.profile == resolved_profile
        )
        if not removal_bindings:
            raise ValueError(
                f"tracked group '{repo.config.name}:{resolved_selector}@{resolved_profile}' "
                "has no tracked package entries"
            )
        return UntrackGroupRequest(
            repo=repo.config.name,
            selector=resolved_selector,
            selector_kind=selector_kind,
            profile=resolved_profile,
            removal_bindings=removal_bindings,
        )

    def _resolve_group_profile(
        self,
        *,
        repo_name: str,
        selector: str,
        requested_profile: str | None,
        tracked_bindings: Sequence[FullSpecSelector],
    ) -> str | None:
        if requested_profile is not None:
            available_profiles = self._engine.list_profiles(repo_name)
            if not available_profiles:
                raise ValueError(f"repo '{repo_name}' does not define any profiles")
            partial_profiles = [
                profile for profile in available_profiles if requested_profile in profile
            ]
            return self._resolve_match(
                exact_matches=[
                    profile for profile in available_profiles if profile == requested_profile
                ],
                partial_matches=partial_profiles,
                query_text=requested_profile,
                exact_header_text=f"Select a profile for {repo_name}:{selector}:",
                partial_header_text=(
                    f"Select a profile match for '{requested_profile}' in "
                    f"{repo_name}:{selector}:"
                ),
                option_resolver=lambda profile: ResolverOption(
                    display_label=profile,
                    match_fields=build_profile_match_fields(profile=profile),
                    field_kinds=build_profile_field_kinds(),
                ),
                value_resolver=lambda profile: profile,
                exact_error_text=(
                    f"profile '{requested_profile}' is defined multiple times in repo "
                    f"'{repo_name}'"
                ),
                partial_error_text=(
                    f"profile '{requested_profile}' is ambiguous in repo '{repo_name}': "
                    + ", ".join(partial_profiles)
                ),
                not_found_text=(
                    f"profile '{requested_profile}' did not match any profile in repo "
                    f"'{repo_name}'"
                ),
            )
        if not tracked_bindings:
            raise ValueError(
                f"tracked group '{repo_name}:{selector}' has no tracked package entries"
            )
        profiles_by_package: dict[str, set[str]] = {}
        for binding in tracked_bindings:
            profiles_by_package.setdefault(binding.selector, set()).add(binding.profile)
        tracked_profiles = sorted(
            {binding.profile for binding in tracked_bindings}
        )
        ambiguous_packages = {
            package_id: sorted(profiles)
            for package_id, profiles in profiles_by_package.items()
            if len(profiles) > 1
        }
        if len(tracked_profiles) == 1:
            return tracked_profiles[0]
        if not ambiguous_packages:
            return None
        if self._interaction is not None:
            return self._interaction.choose(
                ChoiceRequest(
                    header_text=f"Select a tracked profile for {repo_name}:{selector}:",
                    options=tuple(
                        ChoiceOption(value=profile, label=profile)
                        for profile in tracked_profiles
                    ),
                )
            )
        ambiguous_package_text = ", ".join(
            ", ".join(
                package_ref_text(package_id=package_id, bound_profile=profile)
                for profile in profiles
            )
            for package_id, profiles in sorted(ambiguous_packages.items())
        )
        raise ValueError(
            f"tracked group '{repo_name}:{selector}' is ambiguous across package instances: "
            f"{ambiguous_package_text}"
        )

    def _package_owner_error(
        self,
        package,
        *,
        explicit_repo: str | None,
        profile: str | None,
    ) -> ValueError:
        matching_owners = [
            binding
            for binding in package.package_entries
            if profile is None or binding.profile == profile
        ]
        owners = ", ".join(self._binding_text(binding) for binding in matching_owners)
        required_repo = explicit_repo or package.repo
        required_ref = cli_style.package_label_text(
            repo_name=required_repo,
            package_id=package.package_id,
            bound_profile=package.bound_profile,
            package_first=True,
            include_repo_context=True,
        )
        return ValueError(
            f"cannot untrack '{required_ref}': required by tracked package entries: {owners}"
        )

    def _persisted_option(self, record) -> ResolverOption:
        base_label = cli_style.render_full_spec_selector_label(
            repo_name=record.package_entry.repo,
            selector=record.package_entry.selector,
            profile=record.package_entry.profile,
            selector_first=True,
            use_color=self._use_color,
        )
        state_badge = ""
        if record.repo is None or record.state_key != record.package_entry.repo:
            state_badge = cli_style.render_menu_badge(
                f"[{record.state_key}]",
                use_color=self._use_color,
            )
        display_fields = (base_label, state_badge) if state_badge else (base_label,)
        return ResolverOption(
            display_label=cli_style.join_menu_display_fields(*display_fields),
            display_fields=display_fields,
            match_fields=build_full_spec_selector_match_fields(
                repo_name=record.package_entry.repo,
                selector=record.package_entry.selector,
                profile=record.package_entry.profile,
            ),
            field_kinds=build_full_spec_selector_field_kinds(),
        )

    def _package_option(self, package) -> ResolverOption:
        display_label = cli_style.render_package_label(
            repo_name=package.repo,
            package_id=package.package_id,
            bound_profile=package.bound_profile,
            package_first=True,
            include_repo_context=True,
            use_color=self._use_color,
        )
        return ResolverOption(
            display_label=display_label,
            match_fields=build_package_match_fields(
                repo_name=package.repo,
                package_id=package.package_id,
                bound_profile=package.bound_profile,
            ),
            field_kinds=build_package_field_kinds(
                has_bound_profile=package.bound_profile is not None
            ),
        )

    def _selector_option(self, match) -> ResolverOption:
        repo, selector, selector_kind = match
        display_fields = cli_style.build_selector_match_display_fields(
            repo_name=repo.config.name,
            selector=selector,
            selector_kind=selector_kind,
            use_color=self._use_color,
        )
        return ResolverOption(
            display_label=cli_style.join_menu_display_fields(*display_fields),
            display_fields=display_fields,
            match_fields=build_selector_match_fields(
                repo_name=repo.config.name,
                selector=selector,
            ),
            field_kinds=build_selector_field_kinds(),
        )

    @staticmethod
    def _entry_choice_value(match) -> str:
        kind, item = match
        if kind == "entry":
            binding = item.package_entry
            return f"entry:{item.state_key}:{binding.repo}:{binding.selector}@{binding.profile}"
        return f"package:{item.repo}:{item.package_ref}"

    @staticmethod
    def _binding_text(binding: FullSpecSelector) -> str:
        return f"{binding.repo}:{binding.selector}@{binding.profile}"

    def _resolve_match(
        self,
        *,
        exact_matches: Sequence[Match],
        partial_matches: Sequence[Match],
        query_text: str,
        exact_header_text: str,
        partial_header_text: str,
        option_resolver,
        value_resolver,
        exact_error_text: str,
        partial_error_text: str,
        not_found_text: str,
    ) -> Match:
        ranked_exact = rank_resolver_matches(
            exact_matches,
            query=query_text,
            option_resolver=option_resolver,
        )
        ranked_partial = rank_resolver_matches(
            partial_matches,
            query=query_text,
            option_resolver=option_resolver,
        )
        if len(ranked_exact) == 1:
            return ranked_exact[0]
        if ranked_exact:
            return self._choose(
                ranked_exact,
                header_text=exact_header_text,
                option_resolver=option_resolver,
                value_resolver=value_resolver,
                noninteractive_error=exact_error_text,
            )
        if ranked_partial:
            if self._interaction is None:
                if len(ranked_partial) == 1:
                    raise ValueError(
                        f"no exact match for '{query_text}'; use exact name "
                        f"'{value_resolver(ranked_partial[0])}'"
                    )
                raise ValueError(partial_error_text)
            return self._choose(
                ranked_partial,
                header_text=partial_header_text,
                option_resolver=option_resolver,
                value_resolver=value_resolver,
                noninteractive_error=partial_error_text,
            )
        raise ValueError(not_found_text)

    def _choose(
        self,
        matches: Sequence[Match],
        *,
        header_text: str,
        option_resolver,
        value_resolver,
        noninteractive_error: str,
    ) -> Match:
        if self._interaction is None:
            raise ValueError(noninteractive_error)
        values = {value_resolver(match): match for match in matches}
        options: list[ChoiceOption[str]] = []
        for match in matches:
            resolved_option = option_resolver(match)
            options.append(
                ChoiceOption(
                    value=value_resolver(match),
                    label=resolved_option.display_label,
                    display_fields=(
                        resolved_option.display_fields
                        or (resolved_option.display_label,)
                    ),
                )
            )
        selected_value = self._interaction.choose(
            ChoiceRequest(header_text=header_text, options=tuple(options))
        )
        return values[selected_value]
