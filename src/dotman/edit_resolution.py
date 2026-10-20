from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from dotman import cli_style
from dotman.engine import DotmanEngine
from dotman.errors import UsageError
from dotman.interaction import ChoiceOption, ChoiceRequest, Interaction
from dotman.interaction_policy import InteractionRequiredError
from dotman.models import ManagerConfig, package_ref_text
from dotman.package_resolution import (
    parse_full_spec_selector_text,
    parse_package_ref_text,
)
from dotman.resolver import (
    ResolverOption,
    build_package_field_kinds,
    build_package_match_fields,
    build_target_field_kinds,
    build_target_match_fields,
    rank_resolver_option,
)

CandidateValue = TypeVar("CandidateValue")


@dataclass(frozen=True)
class _Candidate(Generic[CandidateValue]):
    value: CandidateValue
    label: str
    resolver_option: ResolverOption


@dataclass(frozen=True)
class _EditQueryCandidate:
    kind: str
    repo_name: str
    package_id: str
    target_name: str | None
    bound_profile: str | None
    path: Path
    resolver_option: ResolverOption

    @property
    def ref_text(self) -> str:
        package_ref = package_ref_text(
            package_id=self.package_id,
            bound_profile=self.bound_profile,
        )
        if self.target_name is None:
            return f"{self.repo_name}:{package_ref}"
        return f"{self.repo_name}:{package_ref}.{self.target_name}"

    @property
    def label(self) -> str:
        return f"{self.ref_text} [{self.kind}]"


class EditResolver:
    def __init__(
        self,
        config: ManagerConfig,
        *,
        engine: DotmanEngine | None = None,
        interaction: Interaction | None = None,
        use_color: bool = False,
    ) -> None:
        self._config = config
        self._engine = engine
        self._interaction = interaction
        self._use_color = use_color

    def resolve_local_path(self, repo_query: str | None = None) -> Path:
        return self._resolve_repo_config(
            repo_query=repo_query,
            command_label="edit local repo",
            selection_header="Select a repo for local overrides:",
        ).local_override_path

    def resolve_repo_path(self, repo_query: str) -> Path:
        return self._resolve_repo_config(
            repo_query=repo_query,
            command_label="edit repo",
            selection_header="Select a repo to edit:",
        ).path

    def resolve_package_path(self, package_text: str) -> Path:
        engine = self._require_engine()
        explicit_repo, selector, bound_profile = parse_package_ref_text(package_text)
        package_query = package_ref_text(package_id=selector, bound_profile=bound_profile)
        lookup_text = f"{explicit_repo}:{package_query}" if explicit_repo is not None else package_query
        _selector, _profile, exact_matches, partial_matches = engine.find_tracked_package_matches(
            lookup_text
        )
        exact_candidates = [self._package_candidate(*match) for match in exact_matches]
        partial_candidates = [self._package_candidate(*match) for match in partial_matches]
        selected = self._resolve_candidates(
            exact_candidates=exact_candidates,
            partial_candidates=partial_candidates,
            query_text=package_query,
            header_text=f"Select a tracked package for '{package_query}':",
            exact_error=self._tracked_package_exact_error(package_query, exact_matches),
            partial_error=(
                f"tracked package '{package_query}' is ambiguous: "
                + ", ".join(
                    self._package_label(repo.config.name, package_id, match_bound_profile)
                    for repo, package_id, match_bound_profile in partial_matches
                )
            ),
            not_found_error=lambda: self._not_found_error(
                "tracked package", lookup_text, f"tracked package '{package_query}' did not match any tracked package"
            ),
            single_partial_error=(
                f"no exact match for '{package_query}'; use exact name '"
                f"{self._package_label(*self._package_identity(partial_matches[0]))}'"
                if len(partial_matches) == 1
                else None
            ),
        )
        return selected

    def resolve_target_path(self, target_text: str) -> Path:
        engine = self._require_engine()
        query_text, exact_matches, partial_matches = engine.find_tracked_target_matches(target_text)
        target_not_found = f"tracked target '{query_text}' did not match any tracked target"
        exact_candidates = [self._target_candidate(match) for match in exact_matches]
        partial_candidates = [self._target_candidate(match) for match in partial_matches]
        return self._resolve_candidates(
            exact_candidates=exact_candidates,
            partial_candidates=partial_candidates,
            query_text=query_text,
            header_text=f"Select a tracked target for '{query_text}':",
            exact_error=(
                f"tracked target '{query_text}' is ambiguous: "
                + ", ".join(self._target_label(match) for match in exact_matches)
            ),
            partial_error=(
                f"tracked target '{query_text}' is ambiguous: "
                + ", ".join(self._target_label(match) for match in partial_matches)
            ),
            not_found_error=lambda: (
                self._not_found_error("tracked target", target_text, target_not_found)
                # A bare target name names no package, so it cannot name a stale row.
                if "." in target_text
                else target_not_found
            ),
            single_partial_error=(
                f"no exact match for '{query_text}'; use exact name '"
                f"{self._target_label(partial_matches[0])}'"
                if len(partial_matches) == 1
                else None
            ),
        )

    def resolve_query_path(self, query_text: str) -> Path:
        intent, explicit_repo, selector = self._parse_query_text(query_text)
        if intent == "target":
            return self.resolve_target_path(query_text)
        query = selector if explicit_repo is None else f"{explicit_repo}:{selector}"
        return self._resolve_tracked_query(
            query,
            query_text=query_text,
            subject="edit query",
            header_text=f"Select an edit target for '{query_text}':",
            value_of=lambda candidate: candidate.path,
        )

    def resolve_tracked_identity(self, query_text: str, *, subject: str) -> str:
        """Resolve a package, package-instance or target query to its canonical identity."""
        return self._resolve_tracked_query(
            query_text,
            query_text=query_text,
            subject=subject,
            header_text=f"Select a {subject} for '{query_text}':",
            value_of=lambda candidate: candidate.ref_text,
            include_probes=True,
        )

    def _resolve_tracked_query(
        self,
        query: str,
        *,
        query_text: str,
        subject: str,
        header_text: str,
        value_of: Callable[[_EditQueryCandidate], CandidateValue],
        include_probes: bool = False,
    ) -> CandidateValue:
        engine = self._require_engine()
        _package_query, _bound_profile, package_exact, package_partial = (
            engine.find_tracked_package_matches(query)
        )
        _target_query, target_exact, target_partial = engine.find_tracked_target_matches(
            query, include_probes=include_probes,
        )
        exact_candidates = [
            self._edit_package_candidate(*match) for match in package_exact
        ] + [self._edit_target_candidate(match) for match in target_exact]
        partial_candidates = [
            self._edit_package_candidate(*match) for match in package_partial
        ] + [self._edit_target_candidate(match) for match in target_partial]

        return self._resolve_candidates(
            exact_candidates=[self._query_candidate(candidate, value_of) for candidate in exact_candidates],
            partial_candidates=[self._query_candidate(candidate, value_of) for candidate in partial_candidates],
            query_text=query_text,
            header_text=header_text,
            exact_error=(
                f"{subject} '{query_text}' is ambiguous: "
                + self._format_edit_query_candidates(exact_candidates)
            ),
            partial_error=(
                f"{subject} '{query_text}' is ambiguous: "
                + self._format_edit_query_candidates(partial_candidates)
            ),
            not_found_error=lambda: self._not_found_error(
                subject, query_text, f"{subject} '{query_text}' did not match any tracked package or target"
            ),
            single_partial_error=(
                f"no exact match for '{query_text}'; use exact name "
                f"'{partial_candidates[0].ref_text}'"
                if len(partial_candidates) == 1
                else None
            ),
        )

    def _not_found_error(self, subject: str, query_text: str, not_found: str) -> str:
        """Explain a miss; a query naming a stale tracked row says why that row cannot match."""
        repo_name, package_id, bound_profile = parse_package_ref_text(query_text.partition(".")[0])
        return self._require_engine().stale_tracked_row_rejection(
            subject_text=f"{subject} '{query_text}'", repo_name=repo_name, package_id=package_id, profile=bound_profile,
        ) or not_found

    def _resolve_repo_config(
        self,
        *,
        repo_query: str | None,
        command_label: str,
        selection_header: str,
    ):
        exact_repos = [
            repo_config
            for repo_config in self._config.ordered_repos
            if repo_query is not None and repo_config.name == repo_query
        ]
        matching_repos = [
            repo_config
            for repo_config in self._config.ordered_repos
            if repo_query is None or repo_query.lower() in repo_config.name.lower()
        ]
        if repo_query is None and len(matching_repos) == 1:
            return matching_repos[0]

        repo_names = ", ".join(repo_config.name for repo_config in self._config.ordered_repos)
        selectable_repos = matching_repos or (
            list(self._config.ordered_repos) if self._interaction is not None else []
        )
        candidates = [
            _Candidate(
                value=repo_config.name,
                label=self._repo_label(repo_config.name),
                resolver_option=ResolverOption(
                    display_label=self._repo_label(repo_config.name),
                    display_fields=(self._repo_label(repo_config.name),),
                    match_fields=(repo_config.name,),
                    field_kinds=("repo",),
                ),
            )
            for repo_config in selectable_repos
        ]
        selected = self._resolve_candidates(
            exact_candidates=[
                candidate
                for candidate in candidates
                if candidate.value in {repo_config.name for repo_config in exact_repos}
            ],
            partial_candidates=[] if exact_repos else candidates,
            query_text=repo_query or "",
            header_text=selection_header,
            exact_error=(
                f"{command_label} '{repo_query}' is ambiguous: "
                + ", ".join(repo_config.name for repo_config in exact_repos)
            ),
            partial_error=(
                f"{command_label} is required in non-interactive mode: {repo_names}"
                if repo_query is None
                else f"{command_label} '{repo_query}' is ambiguous: "
                + ", ".join(repo_config.name for repo_config in matching_repos)
            ),
            not_found_error=lambda: (
                f"{command_label} is required in non-interactive mode: {repo_names}"
                if repo_query is None
                else f"{command_label} '{repo_query}' did not match any configured repo: {repo_names}"
            ),
            single_partial_error=(
                f"{command_label} '{repo_query}' is not exact; use '{matching_repos[0].name}'"
                if repo_query is not None and len(matching_repos) == 1
                else None
            ),
            rank_candidates=repo_query is not None and bool(matching_repos),
        )
        return self._config.repos[selected]

    def _resolve_candidates(
        self,
        *,
        exact_candidates: Sequence[_Candidate[CandidateValue]],
        partial_candidates: Sequence[_Candidate[CandidateValue]],
        query_text: str,
        header_text: str,
        exact_error: str,
        partial_error: str,
        # Built only on a miss: it may mark a stale row shown, which must not happen on a match.
        not_found_error: Callable[[], str],
        single_partial_error: str | None,
        rank_candidates: bool = True,
    ) -> CandidateValue:
        ranked_exact = self._rank_candidates(exact_candidates, query_text, rank_candidates)
        ranked_partial = self._rank_candidates(partial_candidates, query_text, rank_candidates)
        if len(ranked_exact) == 1:
            return ranked_exact[0].value
        if len(ranked_exact) > 1:
            if self._interaction is None:
                raise InteractionRequiredError(exact_error)
            return self._choose_candidate(header_text, ranked_exact)
        if len(ranked_partial) == 1:
            if self._interaction is None:
                raise InteractionRequiredError(single_partial_error)
            return self._choose_candidate(header_text, ranked_partial)
        if len(ranked_partial) > 1:
            if self._interaction is None:
                raise InteractionRequiredError(partial_error)
            return self._choose_candidate(header_text, ranked_partial)
        raise UsageError(not_found_error())

    @staticmethod
    def _rank_candidates(
        candidates: Sequence[_Candidate[CandidateValue]],
        query_text: str,
        enabled: bool,
    ) -> list[_Candidate[CandidateValue]]:
        if not enabled:
            return list(candidates)
        return sorted(
            candidates,
            key=lambda candidate: rank_resolver_option(
                query=query_text,
                option=candidate.resolver_option,
            ),
        )

    def _choose_candidate(
        self,
        header_text: str,
        candidates: Sequence[_Candidate[CandidateValue]],
    ) -> CandidateValue:
        assert self._interaction is not None
        return self._interaction.choose(
            ChoiceRequest(
                header_text=header_text,
                options=tuple(
                    ChoiceOption(
                        value=candidate.value,
                        label=candidate.label,
                        display_fields=candidate.resolver_option.display_fields,
                    )
                    for candidate in candidates
                ),
            )
        )

    def _package_candidate(
        self,
        repo,
        package_id: str,
        bound_profile: str | None,
    ) -> _Candidate[Path]:
        label = self._render_package_label(repo.config.name, package_id, bound_profile)
        return _Candidate(
            value=repo.resolve_package(package_id).package_root,
            label=label,
            resolver_option=ResolverOption(
                display_label=label,
                match_fields=build_package_match_fields(
                    repo_name=repo.config.name,
                    package_id=package_id,
                    bound_profile=bound_profile,
                ),
                field_kinds=build_package_field_kinds(
                    has_bound_profile=bound_profile is not None
                ),
            ),
        )

    def _target_candidate(self, match) -> _Candidate[Path]:
        label = self._render_target_label(match)
        return _Candidate(
            value=match.repo_path,
            label=label,
            resolver_option=ResolverOption(
                display_label=label,
                match_fields=build_target_match_fields(
                    repo_name=match.repo_name,
                    package_id=match.package_id,
                    target_name=match.target_name,
                    bound_profile=match.bound_profile,
                ),
                field_kinds=build_target_field_kinds(
                    has_bound_profile=match.bound_profile is not None
                ),
            ),
        )

    def _edit_package_candidate(
        self,
        repo,
        package_id: str,
        bound_profile: str | None,
    ) -> _EditQueryCandidate:
        ref_text = self._package_label(repo.config.name, package_id, bound_profile)
        rendered_label = cli_style.join_menu_display_fields(
            self._render_package_label(repo.config.name, package_id, bound_profile),
            cli_style.render_menu_badge("[package]", use_color=self._use_color),
        )
        return _EditQueryCandidate(
            kind="package",
            repo_name=repo.config.name,
            package_id=package_id,
            target_name=None,
            bound_profile=bound_profile,
            path=repo.resolve_package(package_id).package_root,
            resolver_option=ResolverOption(
                display_label=f"{ref_text} [package]",
                display_fields=(rendered_label,),
                match_fields=build_package_match_fields(
                    repo_name=repo.config.name,
                    package_id=package_id,
                    bound_profile=bound_profile,
                ),
                field_kinds=build_package_field_kinds(
                    has_bound_profile=bound_profile is not None
                ),
            ),
        )

    def _edit_target_candidate(self, match) -> _EditQueryCandidate:
        ref_text = self._target_label(match)
        rendered_label = cli_style.join_menu_display_fields(
            self._render_target_label(match),
            cli_style.render_menu_badge("[target]", use_color=self._use_color),
        )
        return _EditQueryCandidate(
            kind="target",
            repo_name=match.repo_name,
            package_id=match.package_id,
            target_name=match.target_name,
            bound_profile=match.bound_profile,
            path=match.repo_path,
            resolver_option=ResolverOption(
                display_label=f"{ref_text} [target]",
                display_fields=(rendered_label,),
                match_fields=build_target_match_fields(
                    repo_name=match.repo_name,
                    package_id=match.package_id,
                    target_name=match.target_name,
                    bound_profile=match.bound_profile,
                ),
                field_kinds=build_target_field_kinds(
                    has_bound_profile=match.bound_profile is not None
                ),
            ),
        )

    @staticmethod
    def _query_candidate(
        candidate: _EditQueryCandidate,
        value_of: Callable[[_EditQueryCandidate], CandidateValue],
    ) -> _Candidate[CandidateValue]:
        return _Candidate(
            value=value_of(candidate),
            label=candidate.label,
            resolver_option=candidate.resolver_option,
        )

    @staticmethod
    def _parse_query_text(query_text: str) -> tuple[str, str | None, str]:
        if any(marker in query_text for marker in ("@", "<", ">")):
            raise UsageError(
                "edit query does not accept selector@profile syntax; "
                "use explicit edit package or edit target"
            )
        explicit_repo, selector, selector_profile = parse_full_spec_selector_text(query_text)
        if selector_profile is not None:
            raise UsageError(
                "edit query does not accept selector@profile syntax; "
                "use explicit edit package or edit target"
            )
        if "." not in selector:
            return "package", explicit_repo, selector
        package_id, separator, target_name = selector.partition(".")
        if not separator or not package_id or not target_name:
            raise UsageError(
                f"invalid edit target query '{query_text}'; "
                "expected [<repo>:]<package>.<target>"
            )
        return "target", explicit_repo, package_id

    @staticmethod
    def _package_label(
        repo_name: str,
        package_id: str,
        bound_profile: str | None,
    ) -> str:
        return f"{repo_name}:{package_ref_text(package_id=package_id, bound_profile=bound_profile)}"

    @staticmethod
    def _package_identity(match) -> tuple[str, str, str | None]:
        repo, package_id, bound_profile = match
        return repo.config.name, package_id, bound_profile

    @staticmethod
    def _target_label(match) -> str:
        package_ref = package_ref_text(
            package_id=match.package_id,
            bound_profile=match.bound_profile,
        )
        return f"{match.repo_name}:{package_ref}.{match.target_name}"

    def _render_package_label(
        self,
        repo_name: str,
        package_id: str,
        bound_profile: str | None,
    ) -> str:
        return cli_style.render_package_label(
            repo_name=repo_name,
            package_id=package_id,
            bound_profile=bound_profile,
            package_first=True,
            include_repo_context=True,
            use_color=self._use_color,
        )

    def _render_target_label(self, match) -> str:
        return cli_style.render_package_label(
            repo_name=match.repo_name,
            package_id=match.package_id,
            bound_profile=match.bound_profile,
            target_name=match.target_name,
            package_first=True,
            include_repo_context=True,
            use_color=self._use_color,
        )

    def _repo_label(self, repo_name: str) -> str:
        if not self._use_color:
            return repo_name
        return cli_style.style_text(repo_name, *cli_style.MENU_REPO_STYLE)

    @staticmethod
    def _tracked_package_exact_error(
        package_query: str,
        matches: Sequence,
    ) -> str:
        repo_names = {repo.config.name for repo, _package_id, _bound_profile in matches}
        prefix = (
            f"tracked package '{package_query}' is defined in multiple repos: "
            if len(repo_names) > 1
            else f"tracked package '{package_query}' is ambiguous: "
        )
        return prefix + ", ".join(
            EditResolver._package_label(repo.config.name, package_id, bound_profile)
            for repo, package_id, bound_profile in matches
        )

    @staticmethod
    def _format_edit_query_candidates(candidates: Sequence[_EditQueryCandidate]) -> str:
        return ", ".join(
            f"{candidate.kind} {candidate.ref_text}" for candidate in candidates
        )

    def _require_engine(self) -> DotmanEngine:
        if self._engine is None:
            raise ValueError("tracked edit resolution requires a DotmanEngine")
        return self._engine
