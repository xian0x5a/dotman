from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from dotman import cli_style
from dotman.diff_review import ReviewItem, display_review_path
from dotman.operation_runner import (
    RestoreActionFinished,
    RestoreActionStarted,
    RestoreExecutionEvent,
    RestoreOperationFinished,
    RestoreOperationStarted,
)


def _format_variable_value(value: Any) -> str:
    if isinstance(value, str):
        if not value or value.strip() != value or "\n" in value or "\t" in value:
            return json.dumps(value)
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return str(value)


def effective_execution_mode(*, dry_run_requested: bool) -> str:
    return "dry-run" if dry_run_requested else "execute"


def display_cli_path(reference_path: Path | str, *, full_paths: bool) -> str:
    return display_review_path(reference_path, compact=not full_paths)


def _print_payload_header(header_text: str, *, use_color: bool, file=None) -> None:
    output_file = sys.stdout if file is None else file
    print(file=output_file)
    if not use_color:
        print(f"{cli_style.MENU_HEADER_MARKER} {header_text}", file=output_file)
        return
    print(
        f"{cli_style.style_text(cli_style.MENU_HEADER_MARKER, *cli_style.MENU_HEADER_MARKER_STYLE)} "
        f"{cli_style.style_text(header_text, '1')}",
        file=output_file,
    )


def _render_payload_action(action: str, *, use_color: bool) -> str:
    return cli_style.render_payload_action(action, use_color=use_color)


@dataclass(frozen=True)
class HumanExecutionRenderer:
    full_paths: bool
    use_color: bool
    stream_output: bool = True

    def render_restore_event(self, event: RestoreExecutionEvent) -> None:
        if isinstance(event, RestoreOperationStarted):
            _print_restore_header(
                "executing restore",
                snapshot=event.snapshot,
                action_count=event.action_count,
                use_color=self.use_color,
            )
        elif isinstance(event, RestoreActionStarted):
            _print_restore_execution_step(
                event.index,
                event.total,
                event.action,
                full_paths=self.full_paths,
                use_color=self.use_color,
            )
        elif isinstance(event, RestoreActionFinished):
            action_result = event.result
            if action_result.status == "ok":
                print(f"      {cli_style.render_execution_status('ok', use_color=self.use_color)}")
            else:
                if action_result.error:
                    print(f"      {action_result.error}")
                print(f"      {cli_style.render_execution_status(action_result.status, use_color=self.use_color)}")
        elif isinstance(event, RestoreOperationFinished):
            return

    @staticmethod
    def render_restore_result(result: Any) -> int:
        return result.exit_code


@dataclass(frozen=True)
class JsonExecutionRenderer:
    stream_output: bool = False

    @staticmethod
    def render_restore_event(_event: RestoreExecutionEvent) -> None:
        return None

    @staticmethod
    def render_restore_result(result: Any) -> int:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return result.exit_code


def _render_tracked_issue_label(engine: Any, issue: Any, *, use_color: bool) -> str:
    bound_profile: str | None = None
    try:
        repo = engine.get_repo(issue.repo)
    except ValueError:
        repo = None
    if repo is not None and issue.selector in repo.packages:
        try:
            binding_mode = repo.package_binding_mode(issue.selector)
        except ValueError:
            # The row is listed because its manifest does not load; the issue message
            # carries that error, so label it like an unknown package instead of failing.
            binding_mode = None
        if binding_mode == "multi_instance":
            bound_profile = issue.profile
    return cli_style.render_package_label(
        repo_name=issue.repo,
        package_id=issue.selector,
        bound_profile=bound_profile,
        use_color=use_color,
    )


def emit_tracked_packages(
    *,
    engine: Any,
    packages: Sequence[Any],
    invalid_package_entries: Sequence[Any],
    json_output: bool,
    use_color: bool,
) -> int:
    payload = {
        "mode": "dry-run",
        "operation": "list-tracked",
        "packages": [package.to_dict() for package in packages],
        "invalid_package_entries": [binding.to_dict() for binding in invalid_package_entries],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    for package in packages:
        print(
            cli_style.render_package_label(
                repo_name=package.repo,
                package_id=package.package_id,
                bound_profile=package.bound_profile,
                use_color=use_color,
            )
            + f" {cli_style.render_tracked_state(package.state, use_color=use_color)}"
        )
    for binding in invalid_package_entries:
        print(
            f"{_render_tracked_issue_label(engine, binding, use_color=use_color)} "
            f"{cli_style.render_tracked_state(binding.state, use_color=use_color)}"
        )
    return 0


def emit_trackables(*, trackables: Sequence[Any], json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "dry-run",
        "operation": "list-trackables",
        "trackables": [trackable.to_dict() for trackable in trackables],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    for trackable in trackables:
        selector_label = cli_style.render_package_label(
            repo_name=trackable.repo,
            package_id=trackable.selector,
            package_first=True,
            include_repo_context=True,
            use_color=use_color,
        )
        kind_badge = cli_style.render_menu_badge(f"[{trackable.kind}]", use_color=use_color)
        meta_badge = _render_trackable_meta_badge(
            kind=trackable.kind,
            binding_mode=trackable.binding_mode,
            member_count=trackable.member_count,
            use_color=use_color,
        )
        description = cli_style.render_annotation_parentheses(trackable.description or "", use_color=use_color)
        print(f"{cli_style.join_menu_display_fields(selector_label, kind_badge, meta_badge)}{description}")
    return 0


def _repo_config_to_dict(repo: Any) -> dict[str, Any]:
    return {
        "name": repo.name,
        "path": str(repo.path),
        "order": repo.order,
        "state_key": repo.state_key,
        "state_path": str(repo.state_path),
        "local_override_path": str(repo.local_override_path),
    }


def emit_repos(*, repos: Sequence[Any], json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "dry-run",
        "operation": "list-repos",
        "repos": [_repo_config_to_dict(repo) for repo in repos],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    for repo in repos:
        repo_name = cli_style.style_text(repo.name, "1") if use_color else repo.name
        order_badge = cli_style.render_menu_badge(f"[order {repo.order}]", use_color=use_color)
        state_annotation = "" if repo.state_key == repo.name else f"state_key: {repo.state_key}"
        print(
            f"{cli_style.join_menu_display_fields(repo_name, order_badge, str(repo.path))}"
            f"{cli_style.render_annotation_parentheses(state_annotation, use_color=use_color)}"
        )
    return 0


_DOCTOR_CHECK_CATEGORY_ORDER = {
    "dependencies": 0,
    "environment": 1,
    "repository": 2,
    "state": 3,
    "other": 4,
}


def _doctor_check_category(check: Any) -> str:
    if check.key.startswith("dependency_"):
        return "dependencies"
    if check.key == "editor":
        return "environment"
    if check.key in {"repo_path", "profiles"}:
        return "repository"
    if check.key.startswith("sync_bases_") or check.key in {"state_dir", "tracked_packages_file", "orphan_tracked_packages_file", "snapshots"}:
        return "state"
    return "other"


def _group_doctor_checks(checks: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    grouped: dict[str, list[Any]] = {}
    for check in checks:
        grouped.setdefault(_doctor_check_category(check), []).append(check)
    return [
        (category, grouped[category])
        for category in sorted(grouped, key=lambda category: (_DOCTOR_CHECK_CATEGORY_ORDER.get(category, 999), category))
    ]


def _print_doctor_check_list(*, checks: Sequence[Any], use_color: bool) -> None:
    for category, category_checks in _group_doctor_checks(checks):
        print(f"    {cli_style.render_payload_section_label(f'{category}:', use_color=use_color)}")
        for check in category_checks:
            owner_label = f"[{check.repo_name}] " if check.repo_name is not None else ""
            print(f"    - {owner_label}{check.detail}")
            if check.path is not None:
                print(f"        {cli_style.render_error_metadata_label('path:', use_color=use_color)} {check.path}")
            if check.hint is not None:
                print(f"        {cli_style.render_error_metadata_label('hint:', use_color=use_color)} {check.hint}")


def emit_doctor_summary(*, engine: Any, summary: Any, json_output: bool, use_color: bool) -> int:
    payload = summary.to_dict()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if summary.ok else 2

    _print_payload_header("Doctor", use_color=use_color)
    print(f"  {cli_style.render_error_metadata_label('status:', use_color=use_color)} {'ok' if summary.ok else 'failed'}")
    print(f"  {cli_style.render_error_metadata_label('config:', use_color=use_color)} {summary.config_path}")
    print(f"  {cli_style.render_error_metadata_label('repos:', use_color=use_color)} {summary.repo_count}")
    print(f"  {cli_style.render_error_metadata_label('checks:', use_color=use_color)} {len(summary.checks)}")
    if summary.failed_checks:
        print(
            f"  {cli_style.render_error_metadata_label('failed checks:', use_color=use_color)} {len(summary.failed_checks)}"
        )
        _print_doctor_check_list(checks=summary.failed_checks, use_color=use_color)
    if summary.warning_checks:
        print(
            f"  {cli_style.render_error_metadata_label('warnings:', use_color=use_color)} {len(summary.warning_checks)}"
        )
        _print_doctor_check_list(checks=summary.warning_checks, use_color=use_color)
    if summary.invalid_package_entries:
        print(
            f"  {cli_style.render_error_metadata_label('invalid package entries:', use_color=use_color)} {len(summary.invalid_package_entries)}"
        )
        print(
            "  issues:"
        )
        for issue in summary.invalid_package_entries:
            print(
                "  - "
                f"{_render_tracked_issue_label(engine, issue, use_color=use_color)} "
                f"{cli_style.render_tracked_state(issue.state, use_color=use_color)} — {issue.message}"
            )
    if summary.ok:
        print("  no issues found")
    return 0 if summary.ok else 2


def emit_search_matches(*, matches: Sequence[Any], query: str, json_output: bool, use_color: bool) -> int:
    payload = {
        "operation": "search",
        "query": query,
        "matches": [match.to_dict() for match in matches],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not matches:
        print(f"no packages or groups matched '{query}'")
        return 0

    for match in matches:
        kind_badge = cli_style.render_menu_badge(f"[{match.kind}]", use_color=use_color)
        meta_badge = _render_trackable_meta_badge(
            kind=match.kind,
            binding_mode=match.binding_mode,
            member_count=match.member_count,
            use_color=use_color,
        )
        description = cli_style.render_annotation_parentheses(match.description or "", use_color=use_color)
        package_label = cli_style.render_package_label(
            repo_name=match.repo,
            package_id=match.selector,
            package_first=True,
            include_repo_context=True,
            use_color=use_color,
        )
        print(f"{cli_style.join_menu_display_fields(package_label, kind_badge, meta_badge)}{description}")
    return 0


def _render_trackable_meta_badge(*, kind: str, binding_mode: str | None, member_count: int | None, use_color: bool) -> str:
    if kind == "package":
        return cli_style.render_menu_badge(f"[{binding_mode}]", use_color=use_color)
    return cli_style.render_menu_badge(f"[{member_count} members]", use_color=use_color)


def emit_variables(*, variables: Sequence[Any], json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "dry-run",
        "operation": "list-vars",
        "variables": [variable.to_dict() for variable in variables],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    for variable in variables:
        binding_label = cli_style.render_full_spec_selector_label(
            repo_name=variable.repo,
            selector=variable.selector,
            profile=variable.profile,
            use_color=use_color,
        )
        print(f"{cli_style.render_variable_name(variable.variable, use_color=use_color)} ({binding_label})")
    return 0


def emit_variable_detail(*, variable_detail: Any, json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "dry-run",
        "operation": "info-var",
        "variable": variable_detail.to_dict(),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    header_text = variable_detail.variable
    if use_color:
        print(cli_style.style_text(header_text, "1"))
    else:
        print(header_text)

    for index, occurrence in enumerate(variable_detail.occurrences):
        if index > 0:
            print()
        binding_label = cli_style.render_full_spec_selector_label(
            repo_name=occurrence.repo,
            selector=occurrence.selector,
            profile=occurrence.profile,
            use_color=use_color,
        )
        print()
        print(cli_style.render_info_section_header("reason", use_color=use_color))
        print(f"      {binding_label}")
        print()
        print(cli_style.render_info_section_header("resolved value", use_color=use_color))
        print(f"      {_format_variable_value(occurrence.value)}")
        print()
        print(cli_style.render_info_section_header("provenance", use_color=use_color))
        provenance_label = occurrence.provenance.source_label
        if occurrence.provenance.source_kind in {"package", "profile"}:
            provenance_label = f"{occurrence.provenance.source_kind} {provenance_label}"
        print(
            f"      {cli_style.render_payload_section_label(provenance_label, use_color=use_color)}: "
            f"{occurrence.provenance.source_path}"
        )
    return 0


def emit_untracked_package_entry(*, binding: Any, still_tracked_package: Any, json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "state-only",
        "operation": "untrack",
        "package_entry": {
            "repo": binding.repo,
            "package_id": binding.selector,
            "profile": binding.profile,
        },
    }
    if still_tracked_package is not None:
        payload["still_tracked_package"] = {
            "repo": still_tracked_package.repo,
            "package_id": still_tracked_package.package_id,
            "package_entries": [
                {
                    **binding_detail.package_entry.to_dict(),
                    "tracked_reason": binding_detail.tracked_reason,
                }
                for binding_detail in still_tracked_package.package_entries
            ],
        }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(
        "untracked "
        + cli_style.render_full_spec_selector_reference(binding, use_color=use_color)
    )
    if still_tracked_package is not None:
        print(
            f"{cli_style.render_package_label(repo_name=still_tracked_package.repo, package_id=still_tracked_package.package_id, bound_profile=still_tracked_package.bound_profile, use_color=use_color)} "
            "remains tracked via:"
        )
        for binding_detail in still_tracked_package.package_entries:
            print(
                f"  {cli_style.render_tracked_reason(binding_detail.tracked_reason, use_color=use_color)}: "
                + cli_style.render_full_spec_selector_label(
                    repo_name=binding_detail.package_entry.repo,
                    selector=binding_detail.package_entry.selector,
                    profile=binding_detail.package_entry.profile,
                    use_color=use_color,
                )
            )
    return 0


def emit_untracked_package_entries(
    *,
    request_binding: Any,
    bindings: Sequence[Any],
    still_tracked_packages: Sequence[Any],
    json_output: bool,
    use_color: bool,
) -> int:
    request_label = request_binding.label if hasattr(request_binding, "label") else cli_style.full_spec_selector_label_text(
        repo_name=request_binding.repo,
        selector=request_binding.selector,
        profile=request_binding.profile,
    )
    payload = {
        "mode": "state-only",
        "operation": "untrack",
        "request": {
            "repo": request_binding.repo,
            "selector": request_binding.selector,
            "selector_kind": request_binding.selector_kind,
            "profile": request_binding.profile,
        },
        "package_entries": [
            {
                "repo": binding.repo,
                "package_id": binding.selector,
                "profile": binding.profile,
            }
            for binding in bindings
        ],
    }
    remaining_packages = [package for package in still_tracked_packages if package is not None]
    if remaining_packages:
        payload["still_tracked_packages"] = [
            {
                "repo": package.repo,
                "package_id": package.package_id,
                "package_entries": [
                    {
                        **binding_detail.package_entry.to_dict(),
                        "tracked_reason": binding_detail.tracked_reason,
                    }
                    for binding_detail in package.package_entries
                ],
            }
            for package in remaining_packages
        ]
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    entry_word = "entry" if len(bindings) == 1 else "entries"
    print(
        f"untracked {len(bindings)} package {entry_word} from "
        + request_label
    )
    for binding in bindings:
        print("  " + cli_style.render_full_spec_selector_reference(binding, use_color=use_color))
    for package in remaining_packages:
        print(
            f"{cli_style.render_package_label(repo_name=package.repo, package_id=package.package_id, bound_profile=package.bound_profile, use_color=use_color)} "
            "remains tracked via:"
        )
        for binding_detail in package.package_entries:
            print(
                f"  {cli_style.render_tracked_reason(binding_detail.tracked_reason, use_color=use_color)}: "
                + cli_style.render_full_spec_selector_label(
                    repo_name=binding_detail.package_entry.repo,
                    selector=binding_detail.package_entry.selector,
                    profile=binding_detail.package_entry.profile,
                    use_color=use_color,
                )
            )
    return 0


def emit_tracked_package_entry(*, binding: Any, json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "state-only",
        "operation": "track",
        "package_entry": {
            "repo": binding.repo,
            "package_id": binding.selector,
            "profile": binding.profile,
        },
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"tracked {cli_style.render_full_spec_selector_reference(binding, use_color=use_color)}")
    return 0


def emit_add_result(*, result: Any, json_output: bool, use_color: bool) -> int:
    if json_output:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0

    package_label = cli_style.render_package_label(
        repo_name=result.repo_name,
        package_id=result.package_id,
        package_first=True,
        include_repo_context=True,
        use_color=use_color,
    )
    action = "created" if result.created_package else "updated"
    print(f"{action} package config {package_label}")
    print(f"  manifest: {result.manifest_path}")
    print(f"  target:   {result.target_name} [{result.target_kind}]")
    print(f"  source:   {result.source_path}")
    print(f"  path:     {result.config_path}")
    if result.chmod is not None:
        print(f"  chmod:    {result.chmod}")
    manifest_only_note = "manifest only; repo source files were not copied"
    if use_color:
        manifest_only_note = cli_style.style_text(manifest_only_note, *cli_style.MENU_HINT_STYLE)
    print(f"  {manifest_only_note}")
    return 0


def emit_kept_add_result(*, repo_name: str, package_id: str, json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "config-only",
        "operation": "add",
        "repo": repo_name,
        "package_id": package_id,
        "written": False,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        "kept package config unchanged "
        + cli_style.render_package_label(
            repo_name=repo_name,
            package_id=package_id,
            package_first=True,
            include_repo_context=True,
            use_color=use_color,
        )
    )
    return 0


def emit_noop_add_result(*, json_output: bool) -> int:
    payload = {
        "mode": "config-only",
        "operation": "add",
        "written": False,
        "changed": False,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("No package config changes.")
    return 0


def emit_kept_package_entry(*, binding: Any, json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "state-only",
        "operation": "track",
        "package_entry": {
            "repo": binding.repo,
            "package_id": binding.selector,
            "profile": binding.profile,
        },
        "recorded": False,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"kept existing tracked package entry {cli_style.render_full_spec_selector_reference(binding, use_color=use_color)}")
    return 0


def emit_skipped_tracking(*, binding: Any, json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "state-only",
        "operation": "track",
        "package_entry": {
            "repo": binding.repo,
            "package_id": binding.selector,
            "profile": binding.profile,
        },
        "recorded": False,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"skipped tracking {cli_style.render_full_spec_selector_reference(binding, use_color=use_color)}")
    return 0


def render_hook_command_lines(
    command: str,
    *,
    command_count: int,
    index: int,
    io: str = "pipe",
    elevation: str = "none",
) -> list[str]:
    command_lines = command.splitlines() or [""]
    # Number multi-command hooks so users can tell distinct commands apart without cluttering single-command hooks.
    first_prefix = f"      [{index}] " if command_count > 1 else "      "
    continuation_prefix = " " * len(first_prefix)
    elevation_badge = "" if elevation == "none" else f"[{elevation}] "
    metadata_prefix = "".join(("[tty] " if io == "tty" else "", elevation_badge))
    return [
        f"{first_prefix}{metadata_prefix}{command_lines[0]}",
        *[f"{continuation_prefix}{line}" for line in command_lines[1:]],
    ]


def _render_trackable_header(*, repo_name: str, selector: str, selector_kind: str, use_color: bool) -> str:
    return cli_style.join_menu_display_fields(
        cli_style.render_package_label(
            repo_name=repo_name,
            package_id=selector,
            use_color=use_color,
        ),
        cli_style.render_menu_badge(f"[{selector_kind}]", use_color=use_color),
    )


def _tracked_entry_reason_for_package(*, package_id: str, package_entry: Any) -> str:
    if package_entry.selector_kind == "package" and package_entry.selector == package_id:
        return "explicit"
    return "implicit"


def emit_trackable_detail(*, trackable_detail: Any, json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "dry-run",
        "operation": "info-trackable",
        "trackable": trackable_detail.to_dict(),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(
        _render_trackable_header(
            repo_name=trackable_detail.repo,
            selector=trackable_detail.selector,
            selector_kind=trackable_detail.kind,
            use_color=use_color,
        )
    )
    if getattr(trackable_detail, "description", None):
        print(f"  {trackable_detail.description}")

    print()
    print(cli_style.render_info_section_header("status", use_color=use_color))
    if trackable_detail.kind == "package":
        print(f"    tracked: {'yes' if trackable_detail.tracked else 'no'}")
        show_tracked_instances = trackable_detail.binding_mode == "multi_instance" and bool(trackable_detail.tracked_instances)
        if show_tracked_instances:
            print("    tracked instances:")
        for tracked_instance in trackable_detail.tracked_instances:
            if not show_tracked_instances:
                break
            instance_label = cli_style.render_package_label(
                repo_name=tracked_instance.repo,
                package_id=tracked_instance.package_id,
                bound_profile=tracked_instance.bound_profile,
                use_color=use_color,
            )
            print(
                f"      {cli_style.render_tracked_reason(tracked_instance.state, use_color=use_color)}: "
                f"{instance_label}"
            )

        if trackable_detail.tracked_instances:
            print()
            print(cli_style.render_info_section_header("provenance", use_color=use_color))
        show_instance_headers = len(trackable_detail.tracked_instances) > 1
        for tracked_instance in trackable_detail.tracked_instances:
            if show_instance_headers:
                instance_label = cli_style.render_package_label(
                    repo_name=tracked_instance.repo,
                    package_id=tracked_instance.package_id,
                    bound_profile=tracked_instance.bound_profile,
                    use_color=use_color,
                )
                print(f"    {instance_label}")
            for package_entry in tracked_instance.package_entries:
                package_entry_label = cli_style.render_full_spec_selector_label(
                    repo_name=package_entry.repo,
                    selector=package_entry.selector,
                    profile=package_entry.profile,
                    use_color=use_color,
                )
                indent = "      " if show_instance_headers else "    "
                reason = _tracked_entry_reason_for_package(
                    package_id=tracked_instance.package_id,
                    package_entry=package_entry,
                )
                print(f"{indent}{cli_style.render_tracked_reason(reason, use_color=use_color)}: {package_entry_label}")

        if trackable_detail.targets:
            print()
            print(cli_style.render_info_section_header("targets", use_color=use_color))
        for target in trackable_detail.targets:
            target_name = cli_style.style_text(target.target_name, "1") if use_color else target.target_name
            if getattr(target, "probe_command", None) is not None:
                badge = cli_style.render_menu_badge("[probe]", use_color=use_color)
                print(f"    {cli_style.join_menu_display_fields(target_name, badge)}")
                continue
            if target.path is None:
                print(f"    {target_name}")
                continue
            print(f"    {target_name} -> {target.path}")
        return 0

    tracked_state = trackable_detail.tracked_state.replace("_", " ")
    print(f"    tracked: {tracked_state}")
    print(f"    tracked members: {trackable_detail.tracked_member_count}/{len(trackable_detail.members)}")
    if trackable_detail.members:
        print()
        print(cli_style.render_info_section_header("members", use_color=use_color))
    for member in trackable_detail.members:
        badge = cli_style.render_menu_badge("[tracked]" if member.tracked else "[untracked]", use_color=use_color)
        print(f"    {cli_style.join_menu_display_fields(member.package_id, badge)}")
    return 0


def emit_tracked_package_detail(*, package_detail: Any, json_output: bool, use_color: bool) -> int:
    payload = {
        "mode": "dry-run",
        "operation": "info-tracked",
        "package": package_detail.to_dict(),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(
        cli_style.render_package_label(
            repo_name=package_detail.repo,
            package_id=package_detail.package_id,
            bound_profile=package_detail.bound_profile,
            use_color=use_color,
        )
    )
    if package_detail.description:
        print(f"  {package_detail.description}")
    if package_detail.package_entries:
        print()
        print(cli_style.render_info_section_header("provenance", use_color=use_color))
    for package_entry_detail in package_detail.package_entries:
        package_entry_label = cli_style.render_full_spec_selector_label(
            repo_name=package_entry_detail.package_entry.repo,
            selector=package_entry_detail.package_entry.selector,
            profile=package_entry_detail.package_entry.profile,
            use_color=use_color,
        )
        print(
            f"    {cli_style.render_tracked_reason(package_entry_detail.tracked_reason, use_color=use_color)}: "
            f"{package_entry_label}"
        )

    package_entries_with_hooks = [package_entry_detail for package_entry_detail in package_detail.package_entries if package_entry_detail.hooks]
    if package_entries_with_hooks:
        print()
        print(cli_style.render_info_section_header("hooks", use_color=use_color))
    # Hook output stays package-centric here. Under the current tracked-winner model,
    # a package instance has one effective hook-bearing package entry, so repeating the
    # provenance package entry under ::hooks only adds noise.
    for package_entry_detail in package_entries_with_hooks:
        for hook_name, hook_plans in package_entry_detail.hooks.items():
            hook_label = f"[{hook_name}]"
            if use_color:
                hook_label = cli_style.style_text(hook_label, *cli_style.MENU_HINT_STYLE)
            print(f"    {hook_label}")
            for index, hook_plan in enumerate(hook_plans, start=1):
                for line in render_hook_command_lines(
                    hook_plan.command,
                    command_count=len(hook_plans),
                    index=index,
                    io=getattr(hook_plan, "io", "pipe"),
                    elevation=getattr(hook_plan, "elevation", "none"),
                ):
                    print(line)

    if package_detail.owned_targets:
        print()
        print(cli_style.render_info_section_header("owned targets", use_color=use_color))
    for target in package_detail.owned_targets:
        target_name = cli_style.style_text(target.target.target_name, '1') if use_color else target.target.target_name
        print(f"    {target_name} -> {target.target.live_path}")
    return 0


def visible_restore_actions(actions: Sequence[Any]) -> list[Any]:
    return [action for action in actions if action.action != "noop"]


def build_restore_review_items(snapshot: Any, actions: Sequence[Any]) -> list[ReviewItem]:
    # Snapshot live paths are unique, and each restore action comes from one entry.
    entries_by_live_path = {entry.live_path: entry for entry in snapshot.entries}
    review_items: list[ReviewItem] = []
    for action in actions:
        if action.action == "noop":
            continue
        entry = entries_by_live_path[action.live_path]
        review_items.append(
            ReviewItem(
                selection_label=entry.selection_label,
                package_id=entry.package_id,
                bound_profile=entry.bound_profile,
                target_name=entry.target_name,
                action=action.action,
                operation="restore",
                repo_path=action.snapshot_path,
                live_path=action.live_path,
                source_path=str(action.snapshot_path),
                destination_path=str(action.live_path),
                before_bytes=action.before_bytes,
                after_bytes=action.after_bytes,
            )
        )
    return review_items


def emit_snapshot_list(
    *,
    snapshots: Sequence[Any],
    json_output: bool,
    max_generations: int | None = None,
    use_color: bool,
) -> int:
    payload = {
        "mode": "dry-run",
        "operation": "list-snapshots",
        "snapshots": [snapshot.to_dict() for snapshot in snapshots],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    _print_payload_header("snapshots", use_color=use_color)
    if max_generations is not None:
        print(
            "  "
            + " · ".join(
                [
                    cli_style.render_summary_stat(label="retained", value=len(snapshots), use_color=use_color),
                    cli_style.render_summary_stat(label="limit", value=max_generations, use_color=use_color),
                ]
            )
        )
    else:
        print(f"  {cli_style.render_summary_stat(label='snapshots', value=len(snapshots), use_color=use_color)}")

    if not snapshots:
        print()
        print(f"  {cli_style.render_payload_section_label('no snapshots', use_color=use_color)}")
        return 0

    for index, snapshot in enumerate(snapshots, start=1):
        print()
        title = cli_style.format_snapshot_timestamp(snapshot.created_at)
        if use_color:
            title = cli_style.style_text(title, "1")
        index_label = cli_style.style_text(f"{index})", *cli_style.MENU_INDEX_STYLE) if use_color else f"{index})"
        print(f"  {index_label} {title}")
        print(f"     {cli_style.render_snapshot_metadata_label('ref:', use_color=use_color)}          {cli_style.render_snapshot_ref(snapshot.snapshot_id, use_color=use_color)}")
        print(f"     {cli_style.render_snapshot_metadata_label('status:', use_color=use_color)}       {cli_style.render_snapshot_status(snapshot.status, use_color=use_color)}")
        print(f"     {cli_style.render_snapshot_metadata_label('paths:', use_color=use_color)}        {snapshot.entry_count}")
        if snapshot.restore_count > 0:
            restore_summary = f"{snapshot.restore_count}x"
            if snapshot.last_restored_at is not None:
                restore_summary += f" · {cli_style.format_snapshot_timestamp(snapshot.last_restored_at)}"
            print(f"     {cli_style.render_snapshot_metadata_label('restored:', use_color=use_color)}     {restore_summary}")
    return 0


def emit_snapshot_detail(*, snapshot: Any, json_output: bool, full_paths: bool = False, use_color: bool) -> int:
    payload = {
        "mode": "dry-run",
        "operation": "info-snapshot",
        "snapshot": snapshot.to_dict(),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    header_text = f"snapshot {snapshot.snapshot_id}"
    if use_color:
        print(cli_style.style_text(header_text, "1"))
    else:
        print(header_text)
    print(f"  {cli_style.render_snapshot_metadata_label('created:', use_color=use_color)}       {cli_style.format_snapshot_timestamp(snapshot.created_at)}")
    print(f"  {cli_style.render_snapshot_metadata_label('status:', use_color=use_color)}        {cli_style.render_snapshot_status(snapshot.status, use_color=use_color)}")
    print(f"  {cli_style.render_snapshot_metadata_label('paths:', use_color=use_color)}         {snapshot.entry_count}")
    print(f"  {cli_style.render_snapshot_metadata_label('restore count:', use_color=use_color)} {snapshot.restore_count}")
    if snapshot.last_restored_at is not None:
        print(
            f"  {cli_style.render_snapshot_metadata_label('last restored:', use_color=use_color)} "
            f"{cli_style.format_snapshot_timestamp(snapshot.last_restored_at)}"
        )

    if snapshot.entries:
        print()
        print(cli_style.render_info_section_header("paths", use_color=use_color))
    for entry in snapshot.entries:
        path_text = display_cli_path(entry.live_path, full_paths=full_paths)
        print(f"    {path_text}")
        print(
            f"      {cli_style.render_snapshot_metadata_label('reason:', use_color=use_color)} "
            f"{cli_style.render_snapshot_reason(entry.push_action, use_color=use_color)}"
        )
        provenance = cli_style.render_snapshot_provenance(
            repo_name=entry.repo_name,
            package_id=entry.package_id,
            target_name=entry.target_name,
            selection_label=entry.selection_label,
            bound_profile=entry.bound_profile,
            use_color=use_color,
        )
        if provenance is not None:
            print(
                f"      {cli_style.render_snapshot_metadata_label('provenance:', use_color=use_color)} "
                f"{provenance}"
            )
    return 0


def emit_restore_payload(
    *,
    snapshot: Any,
    actions: Sequence[Any],
    json_output: bool,
    mode: str,
    full_paths: bool = False,
    use_color: bool,
) -> int:
    visible_actions = visible_restore_actions(actions)
    payload = {
        "mode": mode,
        "operation": "restore",
        "snapshot": snapshot.to_dict(),
        "actions": [action.to_dict() for action in visible_actions],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    _print_restore_header(f"{mode} restore", snapshot=snapshot, action_count=len(visible_actions), use_color=use_color)
    if not visible_actions:
        return 0
    for action in visible_actions:
        print(
            f"  [{_render_payload_action(action.action, use_color=use_color)}] "
            f"{display_cli_path(action.snapshot_path, full_paths=full_paths)} -> "
            f"{display_cli_path(action.live_path, full_paths=full_paths)}"
        )
    return 0


def _print_restore_header(title: str, *, snapshot: Any, action_count: int, use_color: bool) -> None:
    def label(text: str) -> str:
        return cli_style.render_snapshot_metadata_label(text, use_color=use_color)

    _print_payload_header(title, use_color=use_color)
    print(f"  {label('snapshot:')} {cli_style.render_snapshot_ref(snapshot.snapshot_id, use_color=use_color)}")
    print(f"  {label('created:')}  {cli_style.format_snapshot_timestamp(snapshot.created_at)}")
    print(f"  {label('status:')}   {cli_style.render_snapshot_status(snapshot.status, use_color=use_color)}")
    print(f"  {cli_style.render_summary_stat(label='paths', value=action_count, use_color=use_color)}")
    if action_count == 0:
        print()
        print(f"  {cli_style.render_payload_section_label('no pending target actions', use_color=use_color)}")


def _print_restore_execution_step(index: int, total: int, action: Any, *, full_paths: bool, use_color: bool) -> None:
    print(
        f"    [{index}/{total}] "
        f"{cli_style.render_execution_action_column(action.action, use_color=use_color)} "
        f"{display_cli_path(action.live_path, full_paths=full_paths)}"
    )


def _emit_error_block(*, header_text: str, fields: Sequence[tuple[str, str]], use_color: bool) -> None:
    _print_payload_header(header_text, use_color=use_color, file=sys.stderr)
    for label, value in fields:
        print(f"  {cli_style.render_error_metadata_label(label, use_color=use_color)} {value}", file=sys.stderr)


def _structured_error_fields(error: Any, *, use_color: bool) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    scope_kind = getattr(error, "scope_kind", None)
    repo_name = getattr(error, "repo_name", None)
    package_id = getattr(error, "package_id", None)
    bound_profile = getattr(error, "bound_profile", None)
    target_name = getattr(error, "target_name", None)
    path_rule_pattern = getattr(error, "path_rule_pattern", None)
    if scope_kind == "repo" and repo_name is not None:
        fields.append(("repo:", str(repo_name)))
    elif scope_kind in {"package", "target", "path_rule"} and repo_name is not None and package_id is not None:
        fields.append(
            (
                "target:" if scope_kind == "path_rule" else f"{scope_kind}:",
                cli_style.render_package_label(
                    repo_name=repo_name,
                    package_id=package_id,
                    bound_profile=bound_profile,
                    target_name=target_name,
                    use_color=use_color,
                ),
            )
        )
        if path_rule_pattern is not None:
            fields.append(("path rule:", str(path_rule_pattern)))
    else:
        package_repo = getattr(error, "package_repo", None)
        if package_repo is not None and package_id is not None:
            fields.append(
                (
                    "package:",
                    cli_style.render_package_label(
                        repo_name=package_repo,
                        package_id=package_id,
                        use_color=use_color,
                    ),
                )
            )
    path = getattr(error, "path", None)
    if path is not None:
        fields.append(("path:", str(path)))
    detail = getattr(error, "detail", None)
    if detail is None:
        if hasattr(error, "package_identity") and hasattr(error, "conflict_kind") and hasattr(error, "contenders"):
            detail = cli_style.render_profile_conflict_detail(error, use_color=use_color)
        else:
            detail = str(error) or error.__class__.__name__
    fields.append(("detail:", str(detail)))
    hint = getattr(error, "hint", None)
    if hint is not None:
        fields.append(("hint:", str(hint)))
    return fields


def emit_error(error: Exception, *, use_color: bool) -> None:
    _emit_error_block(
        header_text=error.__class__.__name__,
        fields=_structured_error_fields(error, use_color=use_color),
        use_color=use_color,
    )

_SYNC_BASE_REASONS = {
    "absent": "absent", "ineligible": "ineligible",
    "record_corrupt": "corrupt", "payload_corrupt": "corrupt",
}


def emit_sync_base(*, detail, operation: str, json_output: bool, use_color: bool) -> int:
    if json_output:
        print(json.dumps({"operation": operation, **detail}, indent=2, sort_keys=True))
        return 0
    status = detail["status"].replace("not-applicable", "not applicable")
    print(f'{detail["identity"]} {cli_style.render_sync_term(status, use_color=use_color)}')
    if detail.get("reason"):
        print(f'  Reason: {_SYNC_BASE_REASONS[detail["reason"]]}')
    if "policy" in detail:
        print(f'  Policy: {detail["policy"]}')
        print(f'  Eligible: {"yes" if detail["eligibility"] else "no"}')
    if detail.get("payload"):
        payload = detail["payload"]
        print(f'  Payload: {payload["kind"]}, {payload["size"]} bytes')
        if payload["digest"]:
            print(f'  Digest: {payload["digest"]}')
        if payload["executable"] is not None:
            print(f'  Executable: {"yes" if payload["executable"] else "no"}')
        print("  Integrity: valid")
    return 0


def emit_unusable_sync_bases_reset(*, result, json_output: bool, use_color: bool) -> int:
    if json_output:
        print(json.dumps({"operation": "reset-sync-base", **result}, indent=2, sort_keys=True))
        return 0
    # Identities may hold control characters, so only the count is reported.
    count = result["dropped"]
    noun = "unusable Sync Base" if count == 1 else "unusable Sync Bases"
    print(f'{count} {noun} {cli_style.render_sync_term(result["status"], use_color=use_color)}')
    return 0


def emit_sync_bases(*, entries, json_output: bool, use_color: bool) -> int:
    if json_output:
        print(json.dumps({"operation": "list-sync-bases", "sync_bases": entries}, indent=2, sort_keys=True))
        return 0
    for detail in entries:
        emit_sync_base(detail=detail, operation="info-sync-base", json_output=False, use_color=use_color)
    return 0
