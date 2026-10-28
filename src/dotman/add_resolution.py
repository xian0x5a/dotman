from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dotman import cli_style
from dotman.engine import DotmanEngine
from dotman.errors import UsageError
from dotman.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    Interaction,
    TextInputRequest,
)
from dotman.interaction_policy import InteractionRequiredError
from dotman.manifest import validate_package_id
from dotman.package_resolution import parse_full_spec_selector_text


@dataclass(frozen=True)
class AddDestination:
    repo_name: str
    package_id: str


class AddResolver:
    def __init__(
        self,
        engine: DotmanEngine,
        *,
        interaction: Interaction | None = None,
        error_sink: Callable[[ValueError], None] | None = None,
        use_color: bool = False,
    ) -> None:
        self._engine = engine
        self._interaction = interaction
        self._error_sink = error_sink
        self._use_color = use_color

    def resolve(self, package_query: str | None) -> AddDestination:
        if package_query is None:
            if self._interaction is None:
                raise InteractionRequiredError("package query is required in non-interactive mode")
            package_destinations = [
                AddDestination(repo_name=repo_config.name, package_id=package_id)
                for repo_config in self._engine.config.ordered_repos
                for package_id in sorted(self._engine.get_repo(repo_config.name).packages)
            ]
            selected = self._interaction.choose(
                ChoiceRequest(
                    header_text="Select a package for add:",
                    options=self._add_options(package_destinations),
                )
            )
            return self._finish_selection(selected, repo_query=None, default_package_id=None)

        repo_query, package_fragment = self._parse_package_query(package_query)
        exact_matches, partial_matches = self._find_package_matches(
            repo_query=repo_query,
            package_fragment=package_fragment,
        )
        ranked_exact_matches = sorted(
            exact_matches,
            key=lambda destination: self._destination_rank(
                destination,
                repo_query=repo_query,
                package_query=package_fragment,
            ),
        )
        ranked_partial_matches = sorted(
            partial_matches,
            key=lambda destination: self._destination_rank(
                destination,
                repo_query=repo_query,
                package_query=package_fragment,
            ),
        )

        if len(ranked_exact_matches) == 1:
            return ranked_exact_matches[0]
        if self._interaction is not None:
            selected = self._interaction.choose(
                ChoiceRequest(
                    header_text=f"Select a package for '{package_query}':",
                    options=self._add_options(ranked_exact_matches or ranked_partial_matches),
                )
            )
            return self._finish_selection(
                selected,
                repo_query=repo_query,
                default_package_id=package_fragment,
            )

        if len(ranked_exact_matches) > 1:
            raise InteractionRequiredError(
                f"package '{package_query}' is ambiguous: "
                + ", ".join(self._destination_label(destination) for destination in ranked_exact_matches)
            )
        # Without a prompt a partial match never becomes the destination. A bare query
        # points at it like other commands do; an exact `repo:package` query that names
        # no existing package creates it, whatever other package IDs contain the text.
        if repo_query is None:
            if len(ranked_partial_matches) == 1:
                raise InteractionRequiredError(
                    f"no exact match for '{package_query}'; "
                    f"use exact name '{self._destination_label(ranked_partial_matches[0])}', "
                    "or a repo-qualified query to create a new package"
                )
            if len(ranked_partial_matches) > 1:
                raise InteractionRequiredError(
                    f"package '{package_query}' is ambiguous: "
                    + ", ".join(self._destination_label(destination) for destination in ranked_partial_matches)
                )
            raise InteractionRequiredError(
                f"package '{package_query}' did not match any package; "
                "use an explicit repo-qualified query to create one in non-interactive mode"
            )
        if repo_query not in self._engine.config.repos:
            raise InteractionRequiredError(
                f"package '{package_query}' did not match any package and cannot create "
                "non-interactively without an exact repo"
            )
        validate_package_id(package_fragment)
        return AddDestination(repo_name=repo_query, package_id=package_fragment)

    def confirm_manifest_write(
        self,
        *,
        repo_name: str,
        package_id: str,
    ) -> bool:
        if self._interaction is None:
            return False
        return self._interaction.confirm(
            ConfirmationRequest(
                prompt=f"Write package config changes for {repo_name}:{package_id}? [y/n] "
            )
        )

    def _parse_package_query(self, package_query: str) -> tuple[str | None, str]:
        explicit_repo, selector, profile = parse_full_spec_selector_text(package_query)
        if profile is not None:
            raise UsageError("add package query expects a package selector, not a binding")
        return explicit_repo, selector

    def _find_package_matches(
        self,
        *,
        repo_query: str | None,
        package_fragment: str,
    ) -> tuple[list[AddDestination], list[AddDestination]]:
        exact_matches: list[AddDestination] = []
        partial_matches: list[AddDestination] = []
        normalized_repo_query = None if repo_query is None else repo_query.lower()
        normalized_package_query = package_fragment.lower()

        for repo_config in self._engine.config.ordered_repos:
            repository = self._engine.get_repo(repo_config.name)
            repo_name = repository.config.name
            repo_matches_exact = repo_query is None or repo_name == repo_query
            repo_matches_partial = (
                repo_query is None
                or normalized_repo_query is not None
                and normalized_repo_query in repo_name.lower()
            )
            if not repo_matches_partial:
                continue
            for package_id in repository.packages:
                destination = AddDestination(repo_name=repo_name, package_id=package_id)
                if repo_matches_exact and package_id == package_fragment:
                    exact_matches.append(destination)
                elif normalized_package_query in package_id.lower():
                    partial_matches.append(destination)

        return exact_matches, list(dict.fromkeys(partial_matches))

    def _finish_selection(
        self,
        selected: AddDestination | None,
        *,
        repo_query: str | None,
        default_package_id: str | None,
    ) -> AddDestination:
        if selected is not None:
            return selected
        if self._interaction is None:
            raise RuntimeError("interactive add selection requires an interaction")
        repo_name = self._choose_new_package_repo(repo_query)
        package_id = self._read_valid_package_id(default_package_id)
        return AddDestination(repo_name=repo_name, package_id=package_id)

    def _choose_new_package_repo(self, repo_query: str | None) -> str:
        if repo_query is not None and repo_query in self._engine.config.repos:
            return repo_query
        matching_repo_names = [
            repo_config.name
            for repo_config in self._engine.config.ordered_repos
            if repo_query is None or repo_query.lower() in repo_config.name.lower()
        ]
        repo_names = matching_repo_names or [
            repo_config.name for repo_config in self._engine.config.ordered_repos
        ]
        assert self._interaction is not None
        return self._interaction.choose(
            ChoiceRequest(
                header_text="Select a repo for the new package:",
                options=tuple(ChoiceOption(value=repo_name, label=repo_name) for repo_name in repo_names),
            )
        )

    def _read_valid_package_id(self, default_package_id: str | None) -> str:
        assert self._interaction is not None
        prompt = "Package ID"
        if default_package_id:
            prompt += f" [{default_package_id}]"
        request = TextInputRequest(prompt=f"{prompt}: ")
        while True:
            package_id = self._interaction.read_text(request).strip() or default_package_id or ""
            try:
                validate_package_id(package_id)
            except ValueError as error:
                if self._error_sink is None:
                    raise
                self._error_sink(error)
                continue
            return package_id

    def _add_options(
        self,
        destinations: list[AddDestination],
    ) -> tuple[ChoiceOption[AddDestination | None], ...]:
        return (
            ChoiceOption(value=None, label="create a new package"),
            *(
                ChoiceOption(value=destination, label=self._render_destination_label(destination))
                for destination in destinations
            ),
        )

    @staticmethod
    def _destination_label(destination: AddDestination) -> str:
        return f"{destination.repo_name}:{destination.package_id}"

    def _render_destination_label(self, destination: AddDestination) -> str:
        return cli_style.render_package_label(
            repo_name=destination.repo_name,
            package_id=destination.package_id,
            package_first=True,
            include_repo_context=True,
            use_color=self._use_color,
        )

    @staticmethod
    def _query_fragment_rank(query: str | None, text: str) -> tuple[int, int, int]:
        if query is None or not query.strip():
            return (0, 0, len(text))
        normalized_query = query.strip().lower()
        normalized_text = text.lower()
        if normalized_text == normalized_query:
            return (0, 0, len(normalized_text))
        if normalized_text.startswith(normalized_query):
            return (1, 0, len(normalized_text))
        match_index = normalized_text.find(normalized_query)
        if match_index == -1:
            return (9, 999, len(normalized_text))
        return (2, match_index, len(normalized_text))

    @classmethod
    def _destination_rank(
        cls,
        destination: AddDestination,
        *,
        repo_query: str | None,
        package_query: str | None,
    ) -> tuple[int, int, int, int, int, int, str, str]:
        repo_rank = cls._query_fragment_rank(repo_query, destination.repo_name)
        package_rank = cls._query_fragment_rank(package_query, destination.package_id)
        return (
            *repo_rank,
            *package_rank,
            destination.repo_name.lower(),
            destination.package_id.lower(),
        )
