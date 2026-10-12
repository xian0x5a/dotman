from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from dotman.command_runtime import (
    CommandRequest,
    CommandRuntime,
    ShellCommand,
    first_output_line,
    raise_for_command_interruption,
)
from dotman.models import (
    GuardSkip,
    HookSpec,
    PackageSpec,
    TargetPathRule,
    resolved_package_identity_key,
    package_ref_text,
    repo_qualified_target_text,
    target_path_rule_matches,
)
from dotman.projection import TargetMetadata, build_package_hook_env, build_repo_hook_env
from dotman.templates import render_template_string

if TYPE_CHECKING:
    from dotman.planning import PackagePlanningInput
    from dotman.progress import ProgressSink


PLANNING_GUARD_EXCLUDED_ENV_KEYS = frozenset({"DOTMAN_NO_CONFIRM"})


class GuardPlanningError(ValueError):
    def __init__(
        self,
        *,
        scope_kind: str,
        repo_name: str,
        package_id: str | None,
        bound_profile: str | None,
        target_name: str | None,
        path_rule_pattern: str | None,
        hook_name: str,
        exit_code: int,
        detail: str | None,
    ) -> None:
        self.scope_kind = scope_kind
        self.repo_name = repo_name
        self.package_id = package_id
        self.bound_profile = bound_profile
        self.target_name = target_name
        self.path_rule_pattern = path_rule_pattern
        self.hook_name = hook_name
        self.exit_code = exit_code
        self.output_line = detail
        message = f"{hook_name} failed with exit {exit_code}"
        if detail:
            message = f"{message}: {detail}"
        self.detail = message
        super().__init__(message)


def _selected_package(planning_input: "PackagePlanningInput") -> PackageSpec:
    package_id = planning_input.selection.identity.package_id
    return next(
        package
        for package in planning_input.package_context.resolved_packages
        if package.id == package_id
    )


def _package_has_potential_work(
    planning_input: "PackagePlanningInput",
    *,
    operation: str,
    run_noop: bool,
) -> bool:
    if planning_input.target_metadata:
        return True
    package = _selected_package(planning_input)
    for hook_name in (f"pre_{operation}", f"post_{operation}"):
        hook_spec = (package.hooks or {}).get(hook_name)
        if hook_spec is None or not hook_spec.commands:
            continue
        if run_noop or hook_spec.run_noop or any(command.run_noop for command in hook_spec.commands):
            return True
    return False


def _run_planning_guard(
    *,
    command_runtime: CommandRuntime,
    guard_spec: HookSpec,
    guard_name: str,
    context: dict[str, Any],
    env: dict[str, str],
    scope_kind: str,
    repo_name: str,
    package_id: str | None = None,
    bound_profile: str | None = None,
    target_name: str | None = None,
    path_rule_pattern: str | None = None,
) -> GuardSkip | None:
    for command_spec in guard_spec.commands:
        command = render_template_string(
            command_spec.run,
            context,
            base_dir=guard_spec.declared_in,
            source_path=guard_spec.declared_in,
        ).strip()
        result = command_runtime.run(
            CommandRequest(
                command=ShellCommand(command),
                cwd=guard_spec.declared_in,
                env=dict(env),
                elevation=command_spec.elevation,
                excluded_env_keys=PLANNING_GUARD_EXCLUDED_ENV_KEYS,
            )
        )
        raise_for_command_interruption(result)
        exit_code = result.exit_code
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if exit_code == 0:
            continue
        detail = first_output_line(stderr, stdout)
        if exit_code == 100:
            return GuardSkip(
                scope_kind=scope_kind,
                repo_name=repo_name,
                package_id=package_id,
                bound_profile=bound_profile,
                target_name=target_name,
                path_rule_pattern=path_rule_pattern,
                reason=detail,
            )
        raise GuardPlanningError(
            scope_kind=scope_kind,
            repo_name=repo_name,
            package_id=package_id,
            bound_profile=bound_profile,
            target_name=target_name,
            path_rule_pattern=path_rule_pattern,
            hook_name=guard_name,
            exit_code=exit_code,
            detail=detail,
        )
    return None


def _repo_has_potential_work(
    repo_inputs: list["PackagePlanningInput"],
    *,
    operation: str,
    run_noop: bool,
) -> bool:
    if any(
        _package_has_potential_work(
            planning_input,
            operation=operation,
            run_noop=run_noop,
        )
        for planning_input in repo_inputs
    ):
        return True
    repo = repo_inputs[0].repo
    for hook_name in (f"pre_{operation}", f"post_{operation}"):
        hook_spec = (repo.hooks or {}).get(hook_name)
        if hook_spec is None or not hook_spec.commands:
            continue
        if run_noop or hook_spec.run_noop or any(command.run_noop for command in hook_spec.commands):
            return True
    return False


def _evaluate_repo_guards(
    planning_inputs: list["PackagePlanningInput"],
    *,
    command_runtime: CommandRuntime,
    operation: str,
    run_noop: bool,
    sink: "ProgressSink | None" = None,
) -> tuple[list["PackagePlanningInput"], tuple[GuardSkip, ...]]:
    inputs_by_repo: dict[str, list["PackagePlanningInput"]] = defaultdict(list)
    for planning_input in planning_inputs:
        inputs_by_repo[planning_input.repo.config.name].append(planning_input)

    admitted_inputs: list["PackagePlanningInput"] = []
    guard_skips: list[GuardSkip] = []
    guard_name = f"guard_{operation}"
    for repo_name, repo_inputs in inputs_by_repo.items():
        if not _repo_has_potential_work(repo_inputs, operation=operation, run_noop=run_noop):
            admitted_inputs.extend(repo_inputs)
            continue
        repo = repo_inputs[0].repo
        guard_spec = (repo.hooks or {}).get(guard_name)
        if guard_spec is None or not guard_spec.commands:
            admitted_inputs.extend(repo_inputs)
            continue
        context = {"vars": repo.local_vars, "repo_name": repo_name, "operation": operation}
        skip = _run_planning_guard(
            command_runtime=command_runtime,
            guard_spec=guard_spec,
            guard_name=guard_name,
            context=context,
            env=build_repo_hook_env(repo=repo, operation=operation, context=context),
            scope_kind="repo",
            repo_name=repo_name,
        )
        if skip is None:
            admitted_inputs.extend(repo_inputs)
            continue
        guard_skips.append(skip)
        if sink is not None:
            sink.update(len(repo_inputs))
    return admitted_inputs, tuple(guard_skips)


def _evaluate_package_guards(
    planning_inputs: list["PackagePlanningInput"],
    *,
    command_runtime: CommandRuntime,
    operation: str,
    run_noop: bool,
    sink: "ProgressSink | None" = None,
) -> tuple[list["PackagePlanningInput"], tuple[GuardSkip, ...]]:
    admitted_inputs: list["PackagePlanningInput"] = []
    guard_skips: list[GuardSkip] = []
    outcomes_by_identity: dict[tuple[str, str, str | None], GuardSkip | None] = {}
    guard_name = f"guard_{operation}"

    for planning_input in planning_inputs:
        if not _package_has_potential_work(planning_input, operation=operation, run_noop=run_noop):
            admitted_inputs.append(planning_input)
            continue

        identity_key = resolved_package_identity_key(planning_input.selection.identity)
        if identity_key in outcomes_by_identity:
            if outcomes_by_identity[identity_key] is None:
                admitted_inputs.append(planning_input)
            elif sink is not None:
                sink.update(1)
            continue

        package = _selected_package(planning_input)
        guard_spec = (package.hooks or {}).get(guard_name)
        if guard_spec is None or not guard_spec.commands:
            outcomes_by_identity[identity_key] = None
            admitted_inputs.append(planning_input)
            continue

        package_env = build_package_hook_env(
            repo=planning_input.repo,
            package=package,
            selection=planning_input.selection,
            operation=operation,
            inferred_os=planning_input.package_context.inferred_os,
            context=planning_input.package_context.context,
        )
        skip = _run_planning_guard(
            command_runtime=command_runtime,
            guard_spec=guard_spec,
            guard_name=guard_name,
            context=planning_input.package_context.context,
            env=package_env,
            scope_kind="package",
            repo_name=planning_input.repo.config.name,
            package_id=package.id,
            bound_profile=planning_input.selection.identity.bound_profile,
        )
        outcomes_by_identity[identity_key] = skip
        if skip is None:
            admitted_inputs.append(planning_input)
        else:
            guard_skips.append(skip)
            if sink is not None:
                sink.update(1)

    return admitted_inputs, tuple(guard_skips)


def _evaluate_target_guards(
    planning_inputs: list["PackagePlanningInput"],
    *,
    command_runtime: CommandRuntime,
    operation: str,
) -> tuple[list["PackagePlanningInput"], tuple[GuardSkip, ...]]:
    admitted_inputs: list["PackagePlanningInput"] = []
    guard_skips: list[GuardSkip] = []
    outcomes_by_identity: dict[tuple[str, str, str | None, str], GuardSkip | None] = {}
    guard_name = f"guard_{operation}"
    for planning_input in planning_inputs:
        admitted_metadata: list[TargetMetadata] = []
        for metadata in planning_input.target_metadata:
            identity_key = (
                planning_input.repo.config.name,
                metadata.package_id,
                planning_input.selection.identity.bound_profile,
                metadata.target_name,
            )
            if identity_key in outcomes_by_identity:
                if outcomes_by_identity[identity_key] is None:
                    admitted_metadata.append(metadata)
                continue
            guard_spec = (metadata.target.hooks or {}).get(guard_name)
            if guard_spec is None or not guard_spec.commands:
                outcomes_by_identity[identity_key] = None
                admitted_metadata.append(metadata)
                continue
            skip = _run_planning_guard(
                command_runtime=command_runtime,
                guard_spec=guard_spec,
                guard_name=guard_name,
                context=planning_input.package_context.context,
                env=metadata.command_env,
                scope_kind="target",
                repo_name=planning_input.repo.config.name,
                package_id=metadata.package_id,
                bound_profile=planning_input.selection.identity.bound_profile,
                target_name=metadata.target_name,
            )
            outcomes_by_identity[identity_key] = skip
            if skip is None:
                admitted_metadata.append(metadata)
            else:
                guard_skips.append(skip)
        admitted_inputs.append(replace(planning_input, target_metadata=admitted_metadata))
    return admitted_inputs, tuple(guard_skips)


def evaluate_directory_path_rule_guards(
    *,
    command_runtime: CommandRuntime,
    path_rules: tuple[TargetPathRule, ...],
    candidate_paths: set[str],
    operation: str,
    context: dict[str, Any],
    target_env: dict[str, str],
    repo_name: str,
    package_id: str,
    bound_profile: str | None,
    target_name: str,
) -> tuple[set[str], tuple[GuardSkip, ...]]:
    remaining_paths = set(candidate_paths)
    guard_skips: list[GuardSkip] = []
    guard_name = f"guard_{operation}"
    for rule in path_rules:
        guard_spec = (rule.hooks or {}).get(guard_name)
        if guard_spec is None or not guard_spec.commands:
            continue
        matching_paths = {
            relative_path
            for relative_path in remaining_paths
            if target_path_rule_matches(relative_path, rule.pattern)
        }
        if not matching_paths:
            continue
        rule_env = dict(target_env)
        rule_env["DOTMAN_PATH_RULE_PATTERN"] = rule.pattern
        skip = _run_planning_guard(
            command_runtime=command_runtime,
            guard_spec=guard_spec,
            guard_name=guard_name,
            context=context,
            env=rule_env,
            scope_kind="path_rule",
            repo_name=repo_name,
            package_id=package_id,
            bound_profile=bound_profile,
            target_name=target_name,
            path_rule_pattern=rule.pattern,
        )
        if skip is None:
            continue
        guard_skips.append(skip)
        remaining_paths.difference_update(matching_paths)
    return remaining_paths, tuple(guard_skips)



@dataclass(frozen=True)
class DirectionalEligibility:
    inputs: list["PackagePlanningInput"]
    hook_scopes: frozenset[str]
    guard_skips: tuple[GuardSkip, ...] = ()


def evaluate_directional_guards(
    directional: dict[str, list["PackagePlanningInput"]],
    *,
    command_runtime: CommandRuntime,
    run_noop: bool,
) -> dict[str, DirectionalEligibility]:
    """Narrow both families scope-first, never granting an absent capability."""
    survivors = dict(directional)
    scopes = {direction: set() for direction in directional}
    skips = {direction: [] for direction in directional}
    for evaluate in (_evaluate_repo_guards, _evaluate_package_guards, _evaluate_target_guards):
        for direction, inputs in survivors.items():
            kwargs = {} if evaluate is _evaluate_target_guards else {"run_noop": run_noop}
            survivors[direction], scope_skips = evaluate(
                inputs, command_runtime=command_runtime, operation=direction, **kwargs,
            )
            skips[direction].extend(scope_skips)
            # Lower-scope removals must not suppress independently retained
            # noop hooks at an already admitted ancestor scope.
            for item in survivors[direction]:
                identity = item.selection.identity
                if evaluate is _evaluate_repo_guards:
                    scopes[direction].add(identity.repo)
                elif evaluate is _evaluate_package_guards:
                    package = package_ref_text(
                        package_id=identity.package_id, bound_profile=identity.bound_profile,
                    )
                    scopes[direction].add(f"{identity.repo}:{package}")
                else:
                    scopes[direction].update(
                        repo_qualified_target_text(
                            repo_name=identity.repo, package_id=target.package_id,
                            bound_profile=identity.bound_profile, target_name=target.target_name,
                        )
                        for target in item.target_metadata
                    )
    return {
        direction: DirectionalEligibility(inputs, frozenset(scopes[direction]), tuple(skips[direction]))
        for direction, inputs in survivors.items()
    }
