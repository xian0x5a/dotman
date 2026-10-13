from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from dotman import cli_emit
from dotman.config import load_manager_config
from dotman.engine import DotmanEngine
from dotman.models import ManagerConfig, SelectorKind, package_ref_text
from dotman.repository import Repository
from dotman.snapshot import SnapshotRecord, list_snapshots
from dotman.ui_context import ui_config_scope


EngineFactory = Callable[[str | None], DotmanEngine]


class InspectionCommandRuntime(Protocol):
    """Interactive resolution needed only by inspection workflows."""

    def resolve_tracked_package_text(
        self,
        engine: DotmanEngine,
        package_text: str,
        *,
        json_output: bool,
    ) -> tuple[Repository, str, str | None]: ...

    def resolve_trackable_selector_text(
        self,
        engine: DotmanEngine,
        query_text: str,
        *,
        json_output: bool,
    ) -> tuple[Repository, str, SelectorKind]: ...

    def resolve_variable_text(
        self,
        engine: DotmanEngine,
        variable_text: str,
        *,
        json_output: bool,
    ) -> str: ...

    def resolve_snapshot_record(
        self,
        snapshot_root: Path,
        snapshot_ref: str | None,
        *,
        json_output: bool,
    ) -> SnapshotRecord: ...


class InspectionCommandRunner:
    command_names = frozenset({"doctor", "search", "list", "info"})

    def __init__(
        self,
        *,
        engine_factory: EngineFactory,
        runtime: InspectionCommandRuntime,
        use_color: bool,
    ) -> None:
        self._engine_factory = engine_factory
        self._runtime = runtime
        self._use_color = use_color

    def run(self, args: Any) -> int:
        if _reads_manager_config_only(args):
            config = load_manager_config(args.config)
            with ui_config_scope(config.ui):
                return self._run_config_only(args=args, config=config)

        if args.command not in self.command_names:
            raise ValueError(f"unsupported inspection command '{args.command}'")

        engine = self._engine_factory(args.config)
        with ui_config_scope(engine.config.ui):
            if args.command == "list" and args.list_command == "sync-bases":
                return cli_emit.emit_sync_bases(
                    entries=engine.list_sync_bases(), json_output=args.json_output,
                    use_color=self._use_color,
                )
            if args.command == "info" and args.info_command == "sync-base":
                return cli_emit.emit_sync_base(
                    detail=engine.info_sync_base(args.sync_unit), operation="info-sync-base",
                    json_output=args.json_output, use_color=self._use_color,
                )
            if args.command == "doctor":
                return cli_emit.emit_doctor_summary(
                    engine=engine,
                    summary=engine.doctor(),
                    json_output=args.json_output,
                    use_color=self._use_color,
                )
            if args.command == "search":
                query = args.query.strip()
                return cli_emit.emit_search_matches(
                    matches=engine.search_selectors(query),
                    query=query,
                    json_output=args.json_output,
                    use_color=self._use_color,
                )
            if args.command == "list":
                return self._run_list(args=args, engine=engine)
            return self._run_info(args=args, engine=engine)

    def _run_list(self, *, args: Any, engine: DotmanEngine) -> int:
        if args.list_command == "tracked":
            tracked_state = engine.list_tracked_state()
            return cli_emit.emit_tracked_packages(
                engine=engine,
                packages=tracked_state.packages,
                invalid_package_entries=tracked_state.invalid_package_entries,
                json_output=args.json_output,
                use_color=self._use_color,
            )
        if args.list_command == "trackables":
            return cli_emit.emit_trackables(
                trackables=engine.list_trackables(),
                json_output=args.json_output,
                use_color=self._use_color,
            )
        if args.list_command == "vars":
            return cli_emit.emit_variables(
                variables=engine.list_variables(),
                json_output=args.json_output,
                use_color=self._use_color,
            )
        raise ValueError(f"unsupported list command '{args.list_command}'")

    def _run_info(self, *, args: Any, engine: DotmanEngine) -> int:
        if args.info_command == "tracked":
            repo, package_id, bound_profile = self._runtime.resolve_tracked_package_text(
                engine,
                args.package,
                json_output=args.json_output,
            )
            package_ref = package_ref_text(package_id=package_id, bound_profile=bound_profile)
            return cli_emit.emit_tracked_package_detail(
                package_detail=engine.describe_tracked_package(f"{repo.config.name}:{package_ref}"),
                json_output=args.json_output,
                use_color=self._use_color,
            )
        if args.info_command == "trackable":
            repo, selector, selector_kind = self._runtime.resolve_trackable_selector_text(
                engine,
                args.query,
                json_output=args.json_output,
            )
            return cli_emit.emit_trackable_detail(
                trackable_detail=engine.describe_trackable(
                    repo_name=repo.config.name,
                    selector=selector,
                    selector_kind=selector_kind,
                ),
                json_output=args.json_output,
                use_color=self._use_color,
            )
        if args.info_command == "var":
            resolved_variable = self._runtime.resolve_variable_text(
                engine,
                args.variable,
                json_output=args.json_output,
            )
            return cli_emit.emit_variable_detail(
                variable_detail=engine.describe_variable(resolved_variable),
                json_output=args.json_output,
                use_color=self._use_color,
            )
        raise ValueError(f"unsupported info command '{args.info_command}'")

    def _run_config_only(self, *, args: Any, config: ManagerConfig) -> int:
        if args.command == "list" and args.list_command == "repo":
            return cli_emit.emit_repos(
                repos=config.ordered_repos,
                json_output=args.json_output,
                use_color=self._use_color,
            )
        if args.command == "list" and args.list_command == "snapshots":
            return cli_emit.emit_snapshot_list(
                snapshots=list_snapshots(config.snapshots.path),
                json_output=args.json_output,
                max_generations=config.snapshots.max_generations,
                use_color=self._use_color,
            )
        if args.command == "info" and args.info_command == "snapshot":
            return cli_emit.emit_snapshot_detail(
                snapshot=self._runtime.resolve_snapshot_record(
                    config.snapshots.path,
                    args.snapshot,
                    json_output=args.json_output,
                ),
                json_output=args.json_output,
                full_paths=args.full_path if args.full_path is not None else config.ui.full_paths,
                use_color=self._use_color,
            )
        raise ValueError(f"unsupported config-only command '{args.command}'")


def _reads_manager_config_only(args: Any) -> bool:
    """Repo and snapshot views must work even when repo manifests do not load."""
    if args.command == "list":
        return args.list_command in {"repo", "snapshots"}
    return args.command == "info" and args.info_command == "snapshot"
