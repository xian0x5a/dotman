from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dotman import cli_emit, cli_interaction
from dotman.engine import DotmanEngine
from dotman.interaction_policy import InteractionRequiredError
from dotman.operation_runner import run_restore_operation
from dotman.snapshot import RestoreAction, SnapshotRecord, build_restore_actions
from dotman.terminal import emit_interrupt_notice
from dotman.ui_context import ui_config_scope


EngineFactory = Callable[[str | None], DotmanEngine]
INTERRUPTED_EXIT_CODE = 130


class RestoreCommandRunner:
    """Review and run snapshot restore through typed operation boundaries."""

    command_names = frozenset({"restore"})

    def __init__(self, *, engine_factory: EngineFactory, use_color: bool) -> None:
        self._engine_factory = engine_factory
        self._use_color = use_color

    def run(self, args: Any) -> int:
        engine = self._engine_factory(args.config)
        full_paths = args.full_path if args.full_path is not None else engine.config.ui.full_paths
        with ui_config_scope(engine.config.ui):
            return self._run_restore(args=args, engine=engine, full_paths=full_paths)

    def _run_restore(self, *, args: Any, engine: DotmanEngine, full_paths: bool) -> int:
        snapshot = cli_interaction.resolve_snapshot_record(
            engine.config.snapshots.path,
            args.snapshot,
            json_output=args.json_output,
        )
        actions = build_restore_actions(snapshot)
        if not args.dry_run and not getattr(args, "no_confirm", False) and not cli_interaction.interactive_mode_enabled(json_output=args.json_output):
            raise InteractionRequiredError("restore requires confirmation; use --no-confirm to accept default work")
        if not cli_interaction.review_restore_actions_for_interactive_diffs(
            snapshot=snapshot,
            actions=actions,
            json_output=args.json_output,
            full_paths=full_paths,
            no_confirm=getattr(args, "no_confirm", False),
        ):
            emit_interrupt_notice()
            return INTERRUPTED_EXIT_CODE
        if args.dry_run:
            return cli_emit.emit_restore_payload(
                snapshot=snapshot,
                actions=actions,
                json_output=args.json_output,
                mode=cli_emit.effective_execution_mode(dry_run_requested=True),
                full_paths=full_paths,
                use_color=self._use_color,
            )
        return self._run_restore_execution(
            snapshot=snapshot,
            actions=actions,
            json_output=args.json_output,
            full_paths=full_paths,
        )

    def _run_restore_execution(
        self,
        *,
        snapshot: SnapshotRecord,
        actions: list[RestoreAction],
        json_output: bool,
        full_paths: bool,
    ) -> int:
        renderer = (
            cli_emit.JsonExecutionRenderer()
            if json_output
            else cli_emit.HumanExecutionRenderer(full_paths=full_paths, use_color=self._use_color)
        )
        result = run_restore_operation(
            snapshot=snapshot,
            actions=actions,
            event_sink=renderer.render_restore_event,
        )
        return renderer.render_restore_result(result)
