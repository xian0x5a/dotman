from __future__ import annotations

from dotman.errors import UsageError
from dotman.models import (
    PackageSelectionSourceKind,
    ResolvedPackageIdentity,
    ResolvedPackageSelection,
    SelectorKind,
)
from dotman.repository import Repository


class UnresolvedSelectorError(UsageError):
    """A selector, dependency, or group member names nothing in the repo."""


def parse_full_spec_selector_text(selector_text: str) -> tuple[str | None, str, str | None]:
    repo_name: str | None = None
    selector_and_profile = selector_text
    if ":" in selector_text:
        potential_repo, remainder = selector_text.split(":", 1)
        if "/" not in potential_repo:
            repo_name = potential_repo
            selector_and_profile = remainder
    selector, _, profile = selector_and_profile.partition("@")
    if not selector:
        raise UsageError("selector must not be empty")
    return repo_name, selector, profile or None


def parse_package_ref_text(package_text: str) -> tuple[str | None, str, str | None]:
    repo_name, selector, profile = parse_full_spec_selector_text(package_text)
    if profile is not None:
        raise UsageError("tracked package lookup expects a package selector, not a binding")
    bound_profile: str | None = None
    if selector.endswith(">"):
        open_index = selector.rfind("<")
        if open_index == -1:
            raise UsageError(f"invalid tracked package selector '{selector}'")
        bound_profile = selector[open_index + 1 : -1]
        selector = selector[:open_index]
        if not selector or not bound_profile:
            raise UsageError(f"invalid tracked package selector '{package_text}'")
    return repo_name, selector, bound_profile


def selected_package_ids(
    repo: Repository,
    selector: str,
    selector_kind: SelectorKind,
) -> list[str]:
    return [selector] if selector_kind == "package" else repo.expand_group(selector)


def resolve_package_ids(
    repo: Repository,
    selector: str,
    selector_kind: SelectorKind,
) -> list[str]:
    roots = selected_package_ids(repo, selector, selector_kind)
    ordered: list[str] = []
    seen_packages: set[str] = set()
    completed_nodes: set[tuple[str, str]] = set()

    def visit_selector(
        current_selector: str,
        stack: tuple[tuple[str, str], ...],
        *,
        source: str,
    ) -> None:
        package_exists = current_selector in repo.packages
        group_exists = current_selector in repo.groups
        if not package_exists and not group_exists:
            raise UnresolvedSelectorError(
                f"{source} '{current_selector}' does not resolve in repo '{repo.config.name}'"
            )

        node_kind = "package" if package_exists else "group"
        node = (node_kind, current_selector)
        if node in stack:
            # Dependency graphs may be cyclic. Active-node revisits are back-edges;
            # stopping here keeps resolution finite while collecting each package once.
            return
        if node in completed_nodes:
            return

        next_stack = (*stack, node)
        if group_exists:
            for member in repo.groups[current_selector].members:
                visit_selector(member, next_stack, source="group member")
            completed_nodes.add(node)
            return

        for dependency in repo.resolve_package(current_selector).depends or ():
            visit_selector(dependency, next_stack, source="dependency")
        # Post-order keeps operation plans and execution dependency-first.
        if current_selector not in seen_packages:
            seen_packages.add(current_selector)
            ordered.append(current_selector)
        completed_nodes.add(node)

    for root_package in roots:
        visit_selector(root_package, (), source="package")
    return ordered


def bound_profile_for_package(
    repo: Repository,
    package_id: str,
    requested_profile: str,
) -> str | None:
    if repo.package_binding_mode(package_id) == "multi_instance":
        return requested_profile
    return None


def resolved_package_identity(
    repo: Repository,
    package_id: str,
    requested_profile: str,
) -> ResolvedPackageIdentity:
    return ResolvedPackageIdentity(
        repo=repo.config.name,
        package_id=package_id,
        bound_profile=bound_profile_for_package(repo, package_id, requested_profile),
    )


def resolved_package_selection(
    *,
    repo: Repository,
    package_id: str,
    requested_profile: str,
    explicit: bool,
    source_kind: PackageSelectionSourceKind,
    source_selector: str | None = None,
    owner_identity: ResolvedPackageIdentity | None = None,
    owner_selection_label: str | None = None,
) -> ResolvedPackageSelection:
    return ResolvedPackageSelection(
        identity=resolved_package_identity(repo, package_id, requested_profile),
        requested_profile=requested_profile,
        explicit=explicit,
        source_kind=source_kind,
        source_selector=source_selector,
        owner_identity=owner_identity,
        owner_selection_label=owner_selection_label,
    )
