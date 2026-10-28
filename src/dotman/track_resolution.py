from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

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
from dotman.package_resolution import UnresolvedSelectorError, parse_full_spec_selector_text
from dotman.repository import MissingExtendsParentError, Repository
from dotman.resolver import (
    ResolverOption,
    build_profile_option,
    build_selector_option,
    resolve_match,
)

TrackDisposition = Literal["ready", "kept", "skipped"]


@dataclass(frozen=True)
class TrackResolution:
    disposition: TrackDisposition
    binding: FullSpecSelector
    # Conflicting packages promoted to explicit entries so the binding can be
    # tracked; recorded together with a "ready" binding.
    promotions: tuple[FullSpecSelector, ...] = ()


def _blocking_conflict(conflicts: Sequence[TrackedTargetConflictError]) -> TrackedTargetConflictError:
    """The conflict that blocks tracking: an explicit clash, which no promotion settles, before any implicit tie."""
    return next((conflict for conflict in conflicts if conflict.precedence != "implicit"), conflicts[0])


def _notice_heading(*, no_confirm: bool) -> str:
    """Under --no-confirm the change is reported, not asked about."""
    return "Applying" if no_confirm else "Confirm"


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
        named_repo, named_package, named_profile = parse_full_spec_selector_text(binding_text)
        try:
            binding = self._resolve_selector(binding_text)
        except UsageError as exc:
            # A deleted package or profile fails the name lookup itself.
            self._raise_if_named_row_is_stale(binding_text, named_repo, named_package, named_profile, exc)
            raise
        try:
            return self._resolve_binding(binding, no_confirm=no_confirm)
        except (MissingExtendsParentError, UnresolvedSelectorError) as exc:
            # The package exists but no longer resolves.
            self._raise_if_named_row_is_stale(binding_text, binding.repo, binding.selector, binding.profile, exc)
            raise

    def _raise_if_named_row_is_stale(
        self, binding_text: str, repo_name: str | None, package_id: str, profile: str | None, cause: Exception,
    ) -> None:
        """When the named package's tracked row is stale, fail naming the row and its fix instead of `cause`."""
        rejection = self._engine.stale_tracked_row_rejection(
            subject_text=f"track '{binding_text}'", repo_name=repo_name, package_id=package_id, profile=profile,
        )
        if rejection is not None:
            raise UsageError(rejection) from cause

    def _resolve_binding(self, binding: FullSpecSelector, *, no_confirm: bool) -> TrackResolution:
        promotions: list[FullSpecSelector] = []
        replacements_confirmed_for: FullSpecSelector | None = None
        while True:
            if binding != replacements_confirmed_for:
                replacement_result = self._confirm_replacements(
                    binding,
                    no_confirm=no_confirm,
                )
                if replacement_result is not None:
                    return replacement_result
                replacements_confirmed_for = binding
            conflicts = self._engine.tracked_package_entry_addition_conflicts([*promotions, binding])
            if not conflicts:
                break
            conflict = _blocking_conflict(conflicts)
            promotion = self._resolve_implicit_conflict(binding, conflict, held_promotions=promotions)
            if promotion is not None:
                promotions.append(promotion)
                continue
            alternative_profile = self._select_non_conflicting_profile(binding)
            if alternative_profile is None:
                raise conflict
            # Held promotions settled conflicts of the old profile; conflicts
            # that remain at the new one offer promotions afresh.
            promotions.clear()
            binding = binding.with_profile(alternative_profile)

        # Nothing is recorded until every prompt is accepted, so declining one
        # leaves both the binding and its promotions untracked.
        if not self._confirm_implicit_overrides(binding, promotions, no_confirm=no_confirm):
            existing_binding = self._find_recorded_entry_exact(binding)
            if existing_binding is not None:
                return TrackResolution(disposition="kept", binding=existing_binding)
            return TrackResolution(disposition="skipped", binding=binding)
        return TrackResolution(disposition="ready", binding=binding, promotions=tuple(promotions))

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
        heading = _notice_heading(no_confirm=no_confirm)
        if len(replacements) == 1:
            existing, replacement = replacements[0]
            message = "\n".join(
                [
                    f"{heading} tracked package entry replacement for {replacement.repo}:{replacement.selector}:",
                    "  existing: " + self._binding_label(existing),
                    "  new:      " + self._binding_label(replacement),
                ]
            )
        else:
            message_lines = [
                f"{heading} tracked package entry replacements for {binding.repo}:{binding.selector}@{binding.profile}:"
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
        *,
        held_promotions: list[FullSpecSelector],
    ) -> FullSpecSelector | None:
        if conflict.precedence != "implicit" or self._interaction is None:
            return None

        def promoted_binding(candidate) -> FullSpecSelector:
            # Promote the candidate as it is tracked: a candidate outside the request
            # may come from another repo or profile than the binding being tracked.
            return FullSpecSelector(
                repo=candidate.selection.identity.repo,
                selector=candidate.package_id,
                selector_kind="package",
                profile=candidate.selection.requested_profile,
            )

        promotions = sorted(
            (
                promotion
                for promotion in {promoted_binding(candidate) for candidate in conflict.candidates}
                if self._promotion_fits(promotion, binding, held_promotions)
            ),
            key=lambda promotion: (promotion.repo, promotion.selector, promotion.profile),
        )
        if not promotions:
            return None
        binding_label = self._binding_label(binding)
        if len(promotions) == 1:
            promoted = promotions[0]
            message = "\n".join(
                [
                    f"Resolve implicit conflict for {binding_label}:",
                    f"  target path: {conflict.live_path}",
                    f"  requested: {binding_label}",
                    f"  promote:   {self._binding_label(promoted)}",
                    "  tracking it explicitly, alongside the request, breaks the implicit tie.",
                ]
            )
            if self._interaction.confirm(
                ConfirmationRequest(
                    prompt="Confirm promotion? [y/n] ",
                    message=f"\n{message}\n",
                )
            ):
                return promoted
            return None
        return self._interaction.choose(
            ChoiceRequest(
                header_text=(
                    f"Select a conflicting package to track explicitly alongside {binding_label}:"
                ),
                options=tuple(
                    ChoiceOption(value=promotion, label=self._binding_label(promotion))
                    for promotion in promotions
                ),
            )
        )

    def _promotion_fits(
        self,
        promotion: FullSpecSelector,
        binding: FullSpecSelector,
        held_promotions: list[FullSpecSelector],
    ) -> bool:
        """Whether `promotion` can be tracked with the request without an explicit clash at any path.

        An implicit conflict that remains is fine: the next round offers another promotion.
        """
        conflicts = self._engine.tracked_package_entry_addition_conflicts([*held_promotions, promotion, binding])
        return all(conflict.precedence == "implicit" for conflict in conflicts)

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
                self._engine.validate_tracked_package_entry_additions(
                    [binding.with_profile(candidate_profile)]
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
        promotions: list[FullSpecSelector],
        *,
        no_confirm: bool,
    ) -> bool:
        # Preview the request as a whole, so the binding's own implicit packages
        # count as contenders a promotion can override.
        overrides = [
            override
            for override in self._engine.preview_tracked_package_entry_implicit_overrides(
                [*promotions, binding]
            )
            if self._explicit_override_needs_confirmation(override)
        ]
        if not overrides:
            return True
        message_lines = [
            f"{_notice_heading(no_confirm=no_confirm)} explicit override for {self._binding_label(binding)}:",
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
            return build_selector_option(
                repo_name=repo.config.name,
                selector=matched_selector,
                selector_kind=selector_kind,
                use_color=self._use_color,
            )

        repo, resolved_selector, selector_kind = resolve_match(
            self._interaction,
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
        return resolve_match(
            self._interaction,
            exact_matches=exact_matches,
            partial_matches=selection_matches,
            query_text=requested_profile,
            exact_header_text=f"Select a profile for {repo_name}:{selector}:",
            partial_header_text=partial_header_text,
            option_resolver=build_profile_option,
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
