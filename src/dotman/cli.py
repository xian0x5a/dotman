from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from dotman.cli_parser import build_parser, normalize_edit_query_argv
from dotman.command_runtime import command_operation
from dotman.interaction_policy import InteractionRequiredError, interaction_scope
from dotman.standalone_commands import StandaloneCommandRunner
from dotman.terminal import colors_enabled, emit_interrupt_notice

if TYPE_CHECKING:
    from dotman.interaction import Interaction


INTERRUPTED_EXIT_CODE = 130


class RootUnderSudoError(ValueError):
    def __init__(self, invoking_user: str) -> None:
        self.detail = f"dotman does not run as root under sudo (started by {invoking_user})"
        self.hint = f"Run dotman as {invoking_user}; it asks for sudo itself when a protected path needs it."
        super().__init__(self.detail)


def main(
    argv: Sequence[str] | None = None,
    *,
    interaction: Interaction | None = None,
) -> int:
    no_confirm = False
    try:
        raw_argv = list(argv) if argv is not None else sys.argv[1:]
        args = build_parser().parse_args(normalize_edit_query_argv(raw_argv))
        no_confirm = args.no_confirm
        if args.command in StandaloneCommandRunner.command_names:
            selected_runner = StandaloneCommandRunner()
        else:
            _refuse_root_under_sudo()
            selected_runner = _repo_command_runner(args, None if no_confirm else interaction)
        with command_operation(), interaction_scope(no_confirm=args.no_confirm):
            return selected_runner.run(args)
    except InteractionRequiredError as exc:
        _emit_error(exc, use_color=colors_enabled(sys.stderr))
        return 1
    except KeyboardInterrupt:
        emit_interrupt_notice()
        return INTERRUPTED_EXIT_CODE
    except ValueError as exc:
        _emit_error(exc, use_color=colors_enabled(sys.stderr))
        return 1 if no_confirm else 2
    except RuntimeError as exc:
        # Imported here because only repo commands load the Sync Base store.
        from dotman.sync_base_store import SyncBaseStoreError

        if not isinstance(exc, SyncBaseStoreError):
            raise
        _emit_error(exc, use_color=colors_enabled(sys.stderr))
        return 1 if no_confirm else 2


def _emit_error(exc: Exception, *, use_color: bool) -> None:
    from dotman.cli_emit import emit_error

    emit_error(exc, use_color=use_color)


def _refuse_root_under_sudo() -> None:
    # Repo commands write the repo, Sync Base and state as the running user, so a
    # root run started by sudo would strand root-owned files the user can no
    # longer edit (#171). dotman elevates protected paths itself instead.
    # Standalone helpers skip this check: elevated hooks may pipe through them.
    # Root calling sudo (SUDO_USER=root, e.g. provisioning scripts) owns its files anyway.
    invoking_user = os.environ.get("SUDO_USER")
    if invoking_user and invoking_user != "root" and os.geteuid() == 0:
        raise RootUnderSudoError(invoking_user)


def _repo_command_runner(args: Any, interaction: Interaction | None) -> Any:
    """Build the runner for commands that read a dotman repo.

    The engine and sync stack load here rather than at module level, so the
    standalone helpers that repo manifests call on every sync stay cheap.
    """
    from dotman import cli_interaction
    from dotman.config import load_manager_config
    from dotman.engine import DotmanEngine
    from dotman.inspection_commands import InspectionCommandRunner
    from dotman.interaction import TerminalInteraction
    from dotman.restore_commands import RestoreCommandRunner
    from dotman.state_commands import StateCommandRunner
    from dotman.sync_deck_command import PullDeckCommandRunner, PushDeckCommandRunner, SyncDeckCommandRunner

    stdin_isatty = getattr(sys.stdin, "isatty", None)
    if not args.no_confirm and interaction is None and stdin_isatty is not None and stdin_isatty():
        interaction = TerminalInteraction()
    engine_factory = lambda config_path: DotmanEngine.from_config_path(
        config_path,
        file_symlink_mode=args.file_symlink_mode,
        dir_symlink_mode=args.dir_symlink_mode,
    )
    use_color = colors_enabled(sys.stdout)
    command_runners = (
        InspectionCommandRunner(
            engine_factory=engine_factory,
            runtime=cli_interaction.InspectionRuntime(),
            use_color=use_color,
        ),
        StateCommandRunner(
            engine_factory=engine_factory,
            runtime=cli_interaction.StateRuntime(interaction),
            use_color=use_color,
        ),
        SyncDeckCommandRunner(
            engine_factory=engine_factory,
            use_color=use_color,
            interaction=interaction,
        ),
        PullDeckCommandRunner(
            engine_factory=engine_factory,
            use_color=use_color,
            interaction=interaction,
        ),
        PushDeckCommandRunner(
            engine_factory=engine_factory,
            use_color=use_color,
            interaction=interaction,
        ),
        RestoreCommandRunner(
            config_loader=load_manager_config,
            use_color=use_color,
        ),
    )
    runner_by_command = {
        command_name: runner
        for runner in command_runners
        for command_name in runner.command_names
    }
    selected_runner = runner_by_command.get(args.command)
    if selected_runner is None:
        raise ValueError(f"unsupported command '{args.command}'")
    return selected_runner

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
