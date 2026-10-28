from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from dotman import cli_emit
from dotman.interaction_policy import InteractionRequiredError
from dotman.add import AddOperationResult, AddReviewResult, prepare_add_to_package, write_add_result
from dotman.add_resolution import AddResolver
from dotman.config import default_config_path, load_manager_config
from dotman.edit_resolution import EditResolver
from dotman.engine import DotmanEngine
from dotman.interaction import Interaction
from dotman.track_resolution import TrackResolver
from dotman.ui_context import ui_config_scope
from dotman.untrack_resolution import (
    UntrackEntryRequest,
    UntrackGroupRequest,
    UntrackResolver,
)


EngineFactory = Callable[[str | None], DotmanEngine]


class StateCommandRuntime(Protocol):
    """Terminal and editor operations used by state-changing commands."""

    @property
    def interaction(self) -> Interaction | None: ...

    def add_editor_available(self) -> bool: ...

    def review_add_manifest(self, result: AddOperationResult) -> AddReviewResult | None: ...

    def open_editor_path(self, *, path: Path, missing_editor_label: str) -> int: ...

    def emit_resolution_error(self, error: ValueError) -> None: ...

    def emit_resolution_message(self, message: str) -> None: ...


class StateCommandRunner:
    command_names = frozenset({"track", "untrack", "add", "edit", "reset"})

    def __init__(
        self,
        *,
        engine_factory: EngineFactory,
        runtime: StateCommandRuntime,
        use_color: bool,
    ) -> None:
        self._engine_factory = engine_factory
        self._runtime = runtime
        self._use_color = use_color

    def run(self, args: Any) -> int:
        if args.command == "edit" and args.no_confirm:
            raise InteractionRequiredError("edit requires an editor and is unavailable with --no-confirm")
        if args.command == "edit" and args.edit_command == "config":
            return self._runtime.open_editor_path(
                path=_resolve_edit_config_path(args.config),
                missing_editor_label="Config path",
            )
        if args.command == "edit" and args.edit_command in {"local", "repo"}:
            return self._run_config_edit(args)
        if args.command not in self.command_names:
            raise ValueError(f"unsupported state-changing command '{args.command}'")

        engine = self._engine_factory(args.config)
        with ui_config_scope(engine.config.ui):
            if args.command == "reset" and args.unusable_repo is not None:
                return cli_emit.emit_unusable_sync_bases_reset(
                    result=engine.reset_unusable_sync_bases(args.unusable_repo or None),
                    json_output=args.json_output, use_color=self._use_color,
                )
            if args.command == "reset":
                return cli_emit.emit_sync_base(
                    detail=engine.reset_sync_base(args.sync_unit), operation="reset-sync-base",
                    json_output=args.json_output, use_color=self._use_color,
                )
            if args.command == "track":
                return self._run_track(args=args, engine=engine)
            if args.command == "untrack":
                return self._run_untrack(args=args, engine=engine)
            if args.command == "add":
                return self._run_add(args=args, engine=engine)
            return self._run_engine_edit(args=args, engine=engine)

    def _run_config_edit(self, args: Any) -> int:
        config = load_manager_config(args.config)
        with ui_config_scope(config.ui):
            resolver = EditResolver(
                config,
                interaction=None if args.json_output else self._runtime.interaction,
                use_color=self._use_color,
            )
            if args.edit_command == "local":
                path = resolver.resolve_local_path(args.repo)
                if self._runtime.add_editor_available():
                    path.parent.mkdir(parents=True, exist_ok=True)
                return self._runtime.open_editor_path(
                    path=path,
                    missing_editor_label="Local override path",
                )
            return self._runtime.open_editor_path(
                path=resolver.resolve_repo_path(args.repo),
                missing_editor_label="Repo path",
            )

    def _run_track(self, *, args: Any, engine: DotmanEngine) -> int:
        resolution = TrackResolver(
            engine,
            interaction=None if args.json_output else self._runtime.interaction,
            message_sink=None if args.json_output else self._runtime.emit_resolution_message,
            use_color=self._use_color,
        ).resolve(
            args.binding,
            no_confirm=args.no_confirm,
        )
        if resolution.disposition == "kept":
            return cli_emit.emit_kept_package_entry(binding=resolution.binding, use_color=self._use_color)
        if resolution.disposition == "skipped":
            return cli_emit.emit_skipped_tracking(binding=resolution.binding, use_color=self._use_color)
        # Promotions need an interaction, which JSON output disables, so JSON
        # output always reports exactly one entry.
        recorded_bindings = [*resolution.promotions, resolution.binding]
        for binding in recorded_bindings:
            engine.record_tracked_package_entry(binding, validate=False)
        for binding in recorded_bindings:
            cli_emit.emit_tracked_package_entry(
                binding=binding,
                json_output=args.json_output,
                use_color=self._use_color,
            )
        return 0

    def _run_untrack(self, *, args: Any, engine: DotmanEngine) -> int:
        resolver = UntrackResolver(
            engine,
            interaction=None if args.json_output else self._runtime.interaction,
            use_color=self._use_color,
        )
        request = resolver.resolve(args.binding)
        if isinstance(request, UntrackGroupRequest):
            removed_bindings = engine.remove_tracked_package_entries(
                list(request.removal_bindings),
                operation="untrack",
                operation_label=request.label,
            )
            return cli_emit.emit_untracked_package_entries(
                request_binding=request,
                bindings=removed_bindings,
                still_tracked_packages=[
                    resolver.remaining_tracked_package(removed_binding)
                    for removed_binding in removed_bindings
                ],
                json_output=args.json_output,
                use_color=self._use_color,
            )
        if not isinstance(request, UntrackEntryRequest):
            raise TypeError(f"unsupported untrack request: {type(request).__name__}")
        removed_binding = engine.remove_persisted_tracked_package_entry(
            request.record,
            operation="untrack",
        )
        return cli_emit.emit_untracked_package_entry(
            binding=removed_binding,
            still_tracked_package=resolver.remaining_tracked_package(removed_binding),
            json_output=args.json_output,
            use_color=self._use_color,
        )

    def _run_add(self, *, args: Any, engine: DotmanEngine) -> int:
        resolver = AddResolver(
            engine,
            interaction=None if args.json_output else self._runtime.interaction,
            error_sink=self._runtime.emit_resolution_error,
            use_color=self._use_color,
        )
        destination = resolver.resolve(args.package_query)
        repo_name = destination.repo_name
        package_id = destination.package_id
        result = prepare_add_to_package(
            repo=engine.get_repo(repo_name),
            package_id=package_id,
            live_path_text=args.live_path,
        )
        if not args.no_confirm and (args.json_output or self._runtime.interaction is None):
            raise InteractionRequiredError("add requires review; use --no-confirm to accept the generated manifest")
        if args.no_confirm:
            return cli_emit.emit_add_result(
                result=write_add_result(result),
                json_output=args.json_output,
                use_color=self._use_color,
            )
        if self._runtime.add_editor_available():
            review_result = self._runtime.review_add_manifest(result)
            if review_result is None:
                raise ValueError("add review expected an editor, but none is configured")
            if review_result.exit_code != 0:
                return review_result.exit_code
            if review_result.manifest_text == result.before_text:
                return cli_emit.emit_noop_add_result()
            if not resolver.confirm_manifest_write(repo_name=repo_name, package_id=package_id):
                return cli_emit.emit_kept_add_result(
                    repo_name=repo_name,
                    package_id=package_id,
                    use_color=self._use_color,
                )
            result = write_add_result(result, manifest_text=review_result.manifest_text)
        else:
            result = write_add_result(result)
        return cli_emit.emit_add_result(
            result=result,
            json_output=args.json_output,
            use_color=self._use_color,
        )

    def _run_engine_edit(self, *, args: Any, engine: DotmanEngine) -> int:
        resolver = EditResolver(
            engine.config,
            engine=engine,
            interaction=None if args.json_output else self._runtime.interaction,
            use_color=self._use_color,
        )
        if args.edit_command == "package":
            path = resolver.resolve_package_path(args.package)
        elif args.edit_command == "query":
            path = resolver.resolve_query_path(args.query)
        else:
            path = resolver.resolve_target_path(args.target)
        return self._runtime.open_editor_path(
            path=path,
            missing_editor_label="Source path",
        )


def _resolve_edit_config_path(config_path: str | None) -> Path:
    selected_path = Path(config_path).expanduser() if config_path is not None else default_config_path()
    return selected_path.resolve()
