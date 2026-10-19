from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

from dotman import cli_style
from dotman.collisions import TrackedTargetConflictError
from dotman.engine import DotmanEngine
from dotman.errors import UsageError
from dotman.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    Interaction,
)
from dotman.interaction_policy import InteractionRequiredError
from dotman.models import FullSpecSelector, ResolvedSelector, SelectorKind
from dotman.package_resolution import parse_full_spec_selector_text
from dotman.repository import Repository
from dotman.resolver import (
    ResolverOption,
    build_profile_field_kinds,
    build_profile_match_fields,
    build_selector_field_kinds,
    build_selector_match_fields,
    rank_resolver_matches,
)

Match = TypeVar("Match")
TrackDisposition = Literal["ready", "kept", "skipped"]


@dataclass(frozen=True)
class TrackResolution:
    disposition: TrackDisposition
    binding: FullSpecSelector


class TrackResolver:
    def __init__(
        self,
        engine: DotmanEngine,
        *,
        interaction: Interaction | None = None,
        message_sink: Callable[[str], None] | None = None,
        use_color: bool = False,
    ) -> None:
        self._engine = engine
        self._interaction = interaction
        self._message_sink = message_sink
        self._use_color = use_color

    def resolve(self, binding_text: str, *, no_confirm: bool = False) -> TrackResolution:
        binding = self._resolve_selector(binding_text)
        while True:
            replacement_result = self._confirm_replacements(
                binding,
                no_confirm=no_confirm,
            )
            if replacement_result is not None:
                return replacement_result
            try:
                self._engine.validate_tracked_package_entry(binding)
            except TrackedTargetConflictError as conflict:
                promoted_binding = self._resolve_implicit_conflict(binding, conflict)
                if promoted_binding is not None:
                    binding = promoted_binding
                    continue
                alternative_profile = self._select_non_conflicting_profile(binding)
                if alternative_profile is None:
                    raise
                binding = binding.with_profile(alternative_profile)
                continue

            if not self._confirm_implicit_overrides(binding, no_confirm=no_confirm):
                existing_binding = self._find_recorded_entry_exact(binding)
                if existing_binding is not None:
                    return TrackResolution(disposition="kept", binding=existing_binding)
                return TrackResolution(disposition="skipped", binding=binding)
            return TrackResolution(disposition="ready", binding=binding)

    def _replacement_scope(
        self,
        binding: FullSpecSelector,
    ) -> tuple[str, str, str | None]:
        repo = self._engine.get_repo(binding.repo)
        if (
            binding.selector in repo.packages
            and repo.resolve_package(binding.selector).binding_mode == "multi_instance"
        ):
            return (binding.repo, binding.selector, binding.profile)
        return (binding.repo, binding.selector, None)

    def _recorded_entries_for_scope(
        self,
        binding: FullSpecSelector,
    ) -> list[FullSpecSelector]:
        repo = self._engine.get_repo(binding.repo)
        existing_by_scope = {
            self._replacement_scope(existing): existing
            for existing in self._engine.read_effective_tracked_package_entries(repo)
        }
        matches: list[FullSpecSelector] = []
        for expanded_binding in self._engine.expand_tracked_package_entry(binding):
            existing = existing_by_scope.get(self._replacement_scope(expanded_binding))
            if existing is not None and existing not in matches:
                matches.append(existing)
        return matches

    def _confirm_replacements(
        self,
        binding: FullSpecSelector,
        *,
        no_confirm: bool,
    ) -> TrackResolution | None:
        expanded_bindings = self._engine.expand_tracked_package_entry(binding)
        existing_bindings = self._recorded_entries_for_scope(binding)
        replacements = [
            (existing_binding, expanded_binding)
            for expanded_binding in expanded_bindings
            for existing_binding in existing_bindings
            if self._replacement_scope(existing_binding)
            == self._replacement_scope(expanded_binding)
            and existing_binding.profile != expanded_binding.profile
        ]
        if not replacements:
            return None
        if len(replacements) == 1:
            existing, replacement = replacements[0]
            message = "\n".join(
                [
                    f"Confirm tracked package entry replacement for {replacement.repo}:{replacement.selector}:",
                    "  existing: " + self._binding_label(existing),
                    "  new:      " + self._binding_label(replacement),
                ]
            )
        else:
            message_lines = [
                f"Confirm tracked package entry replacements for {binding.repo}:{binding.selector}@{binding.profile}:"
            ]
            for existing, replacement in replacements:
                message_lines.extend(
                    [
                        "  existing: " + self._binding_label(existing),
                        "  new:      " + self._binding_label(replacement),
                    ]
                )
            message = "\n".join(message_lines)
        rendered_message = f"\n{message}\n"
        if no_confirm:
            if self._message_sink is not None:
                self._message_sink(rendered_message)
            return None
        if self._interaction is None:
            if len(replacements) == 1:
                existing, replacement = replacements[0]
                raise InteractionRequiredError(
                    f"refusing to replace tracked package entry '{existing.repo}:{existing.selector}@"
                    f"{existing.profile}' with '{replacement.repo}:{replacement.selector}@{replacement.profile}' "
                    "in non-interactive mode"
                )
            replacement_labels = ", ".join(
                f"{existing.repo}:{existing.selector}@{existing.profile} -> "
                f"{replacement.repo}:{replacement.selector}@{replacement.profile}"
                for existing, replacement in replacements
            )
            raise InteractionRequiredError(
                f"refusing to replace tracked package entries for "
                f"'{binding.repo}:{binding.selector}@{binding.profile}' in non-interactive mode: "
                f"{replacement_labels}"
            )
        if self._interaction.confirm(
            ConfirmationRequest(
                prompt="Confirm replacement? [y/n] ",
                message=rendered_message,
            )
        ):
            return None
        if len(existing_bindings) == 1:
            return TrackResolution(disposition="kept", binding=existing_bindings[0])
        return TrackResolution(disposition="skipped", binding=binding)

    def _find_recorded_entry_exact(
        self,
        binding: FullSpecSelector,
    ) -> FullSpecSelector | None:
        repo = self._engine.get_repo(binding.repo)
        expanded_bindings = self._engine.expand_tracked_package_entry(binding)
        if len(expanded_bindings) != 1:
            return None
        expanded_binding = expanded_bindings[0]
        return next(
            (
                existing
                for existing in self._engine.read_effective_tracked_package_entries(repo)
                if existing.repo == expanded_binding.repo
                and existing.selector == expanded_binding.selector
                and existing.profile == expanded_binding.profile
            ),
            None,
        )

    def _resolve_implicit_conflict(
        self,
        binding: FullSpecSelector,
        conflict: TrackedTargetConflictError,
    ) -> FullSpecSelector | None:
        if conflict.precedence != "implicit" or self._interaction is None:
            return None
        candidate_bindings = set(self._engine.expand_tracked_package_entry(binding))

        def candidate_binding(candidate) -> FullSpecSelector:
            return FullSpecSelector(
                repo=candidate.selection.identity.repo,
                selector=candidate.selection.source_selector or candidate.package_id,
                selector_kind="package",
                profile=candidate.selection.requested_profile,
            )

        package_ids = sorted(
            {
                candidate.package_id
                for candidate in conflict.candidates
                if candidate_binding(candidate) in candidate_bindings
            }
        )
        if not package_ids:
            package_ids = sorted({candidate.package_id for candidate in conflict.candidates})
        if not package_ids:
            return None
        binding_label = self._binding_label(binding)
        if len(package_ids) == 1:
            promoted = FullSpecSelector(
                repo=binding.repo,
                selector=package_ids[0],
                selector_kind="package",
                profile=binding.profile,
            )
            message = "\n".join(
                [
                    f"Resolve implicit conflict for {binding_label}:",
                    f"  target path: {conflict.live_path}",
                    f"  requested: {binding_label}",
                    f"  promote:   {self._binding_label(promoted)}",
                    "  explicit tracking can break the implicit tie for this package.",
                ]
            )
            if self._interaction.confirm(
                ConfirmationRequest(
                    prompt="Confirm replacement? [y/n] ",
                    message=f"\n{message}\n",
                )
            ):
                return promoted
            return None
        selected_package = self._interaction.choose(
            ChoiceRequest(
                header_text=(
                    f"Select a conflicting package to track explicitly for {binding_label}:"
                ),
                options=tuple(
                    ChoiceOption(value=package_id, label=package_id)
                    for package_id in package_ids
                ),
            )
        )
        return FullSpecSelector(
            repo=binding.repo,
            selector=selected_package,
            selector_kind="package",
            profile=binding.profile,
        )

    def _select_non_conflicting_profile(
        self,
        binding: FullSpecSelector,
    ) -> str | None:
        if self._interaction is None:
            return None
        valid_profiles: list[str] = []
        for candidate_profile in self._engine.list_profiles(binding.repo):
            if candidate_profile == binding.profile:
                continue
            try:
                self._engine.validate_tracked_package_entry(
                    binding.with_profile(candidate_profile)
                )
            except ValueError:
                continue
            valid_profiles.append(candidate_profile)
        if not valid_profiles:
            return None
        return self._interaction.choose(
            ChoiceRequest(
                header_text=(
                    f"Select a non-conflicting profile for {self._binding_label(binding)}:"
                ),
                options=tuple(
                    ChoiceOption(value=profile, label=profile)
                    for profile in valid_profiles
                ),
            )
        )

    @staticmethod
    def _explicit_override_needs_confirmation(override) -> bool:
        return any(
            contender.selection.requested_profile
            != override.winner.selection.requested_profile
            for contender in override.overridden
        )

    def _confirm_implicit_overrides(
        self,
        binding: FullSpecSelector,
        *,
        no_confirm: bool,
    ) -> bool:
        overrides = [
            override
            for override in self._engine.preview_tracked_package_entry_implicit_overrides(
                binding
            )
            if self._explicit_override_needs_confirmation(override)
        ]
        if not overrides:
            return True
        message_lines = [
            f"Confirm explicit override for {self._binding_label(binding)}:",
            "  this track request will replace implicitly tracked package owners:",
        ]
        for override in overrides:
            message_lines.append(
                "    " + self._override_candidate_label(override.winner, role="new")
            )
            message_lines.extend(
                "      " + self._override_candidate_label(contender, role="implicit")
                for contender in override.overridden
            )
        rendered_message = "\n" + "\n".join(message_lines) + "\n"
        if no_confirm:
            if self._message_sink is not None:
                self._message_sink(rendered_message)
            return True
        if self._interaction is None:
            raise InteractionRequiredError(
                f"refusing to let '{binding.repo}:{binding.selector}@{binding.profile}' "
                "explicitly override implicitly tracked targets in non-interactive mode"
            )
        return self._interaction.confirm(
            ConfirmationRequest(
                prompt="Confirm replacement? [y/n] ",
                message=rendered_message,
            )
        )

    def _override_candidate_label(self, candidate, *, role: str) -> str:
        candidate_label = cli_style.render_full_spec_selector_label_text(
            candidate.selection_label,
            use_color=self._use_color,
        )
        return f"{role}: {candidate_label} ({candidate.package_id})"

    def _binding_label(self, binding: FullSpecSelector) -> str:
        return cli_style.render_full_spec_selector_reference(
            binding,
            use_color=self._use_color,
        )

    def _resolve_selector(self, binding_text: str) -> FullSpecSelector:
        explicit_repo, selector, selector_profile = parse_full_spec_selector_text(binding_text)
        exact_matches, partial_matches = self._engine.find_selector_matches(
            selector,
            explicit_repo,
        )

        def selector_option(
            match: tuple[Repository, str, SelectorKind],
        ) -> ResolverOption:
            repo, matched_selector, selector_kind = match
            return ResolverOption(
                display_label=cli_style.render_selector_match_label(
                    repo_name=repo.config.name,
                    selector=matched_selector,
                    selector_kind=selector_kind,
                    use_color=self._use_color,
                ),
                display_fields=cli_style.build_selector_match_display_fields(
                    repo_name=repo.config.name,
                    selector=matched_selector,
                    selector_kind=selector_kind,
                    use_color=self._use_color,
                ),
                match_fields=build_selector_match_fields(
                    repo_name=repo.config.name,
                    selector=matched_selector,
                ),
                field_kinds=build_selector_field_kinds(),
            )

        repo, resolved_selector, selector_kind = self._resolve_match(
            exact_matches=exact_matches,
            partial_matches=partial_matches,
            query_text=selector,
            exact_header_text=f"Select a repo for exact selector '{selector}':",
            partial_header_text=f"Select a selector match for '{selector}':",
            option_resolver=selector_option,
            value_resolver=lambda match: f"{match[0].config.name}:{match[1]}",
            exact_error_text=f"selector '{selector}' is defined in multiple repos: "
            + ", ".join(f"{repo.config.name}:{match}" for repo, match, _ in exact_matches),
            partial_error_text=f"selector '{selector}' is ambiguous: "
            + ", ".join(f"{repo.config.name}:{match}" for repo, match, _ in partial_matches),
            not_found_text=f"selector '{selector}' did not match any package or group",
        )
        resolved_selector_ref = ResolvedSelector(
            repo=repo.config.name,
            selector=resolved_selector,
            selector_kind=selector_kind,
        )
        resolved_profile = self._resolve_profile(
            repo_name=repo.config.name,
            selector=resolved_selector,
            requested_profile=selector_profile,
        )
        return resolved_selector_ref.with_profile(resolved_profile)

    def _resolve_profile(
        self,
        *,
        repo_name: str,
        selector: str,
        requested_profile: str | None,
    ) -> str:
        available_profiles = self._engine.list_profiles(repo_name)
        if not available_profiles:
            raise UsageError(f"repo '{repo_name}' does not define any profiles")
        if requested_profile is None:
            if len(available_profiles) == 1:
                return available_profiles[0]
            if self._interaction is None:
                raise InteractionRequiredError("profile is required in non-interactive mode")
            return self._interaction.choose(
                ChoiceRequest(
                    header_text=f"Select a profile for {repo_name}:{selector}:",
                    options=tuple(
                        ChoiceOption(value=profile, label=profile)
                        for profile in available_profiles
                    ),
                )
            )

        exact_matches = [
            profile for profile in available_profiles if profile == requested_profile
        ]
        partial_matches = [
            profile for profile in available_profiles if requested_profile in profile
        ]
        selection_matches = partial_matches
        partial_header_text = (
            f"Select a profile match for '{requested_profile}' in {repo_name}:{selector}:"
        )
        if self._interaction is not None and not exact_matches and not partial_matches:
            selection_matches = list(available_profiles)
            partial_header_text = f"Select a profile for {repo_name}:{selector}:"
        return self._resolve_match(
            exact_matches=exact_matches,
            partial_matches=selection_matches,
            query_text=requested_profile,
            exact_header_text=f"Select a profile for {repo_name}:{selector}:",
            partial_header_text=partial_header_text,
            option_resolver=lambda profile: ResolverOption(
                display_label=profile,
                match_fields=build_profile_match_fields(profile=profile),
                field_kinds=build_profile_field_kinds(),
            ),
            value_resolver=lambda profile: profile,
            exact_error_text=(
                f"profile '{requested_profile}' is defined multiple times in repo '{repo_name}'"
            ),
            partial_error_text=(
                f"profile '{requested_profile}' is ambiguous in repo '{repo_name}': "
                + ", ".join(partial_matches)
            ),
            not_found_text=(
                f"profile '{requested_profile}' did not match any profile in repo '{repo_name}'"
            ),
        )

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
                    raise InteractionRequiredError(
                        f"no exact match for '{query_text}'; use exact name '{value_resolver(ranked_partial[0])}'"
                    )
                raise InteractionRequiredError(partial_error_text)
            return self._choose(
                ranked_partial,
                header_text=partial_header_text,
                option_resolver=option_resolver,
                value_resolver=value_resolver,
                noninteractive_error=partial_error_text,
            )
        raise UsageError(not_found_text)

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
            raise InteractionRequiredError(noninteractive_error)
        values = {value_resolver(match): match for match in matches}
        selected_value = self._interaction.choose(
            ChoiceRequest(
                header_text=header_text,
                options=tuple(
                    ChoiceOption(
                        value=value_resolver(match),
                        label=option_resolver(match).display_label,
                        display_fields=(
                            option_resolver(match).display_fields
                            or (option_resolver(match).display_label,)
                        ),
                    )
                    for match in matches
                ),
            )
        )
        return values[selected_value]
