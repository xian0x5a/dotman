"""Stream human Sync execution as a step timeline while it runs."""

from __future__ import annotations

from dotman.diff_review import display_review_path
from dotman.cli_style import MENU_REPO_STYLE, render_execution_action_column, render_execution_status, render_package_label, style_text
from dotman.execution import ExecutionStep, StepFinished, StepStarted, step_group_key
from dotman.ui_context import current_ui_config


class SyncTimelineRenderer:
    """Session event sink printing `[i/n] action target` then each step's status."""

    def __init__(self, *, use_color: bool) -> None:
        self._use_color = use_color
        self._group = None
        # Failure text already printed here; the final recap must not repeat it.
        self.shown_errors: set[str] = set()

    def __call__(self, event) -> None:
        if isinstance(event, StepStarted):
            self._print_group_header(event.step)
            print(f"    [{event.index}/{event.total}] {self._step_line(event.stage, event.step)}", flush=True)
        elif isinstance(event, StepFinished):
            if event.index is None:
                # Steps outside the plan (snapshot finalize) still need attribution.
                print(f"    {self._step_line(event.stage, event.result.step)}")
            self._print_status(event.result)

    def _print_group_header(self, step: ExecutionStep) -> None:
        group = step_group_key(step)
        if group == self._group:
            return
        self._group = group
        repo_name, package_id, bound_profile = group
        if package_id is None:
            label = style_text(repo_name, *MENU_REPO_STYLE) if self._use_color else repo_name
        else:
            label = render_package_label(repo_name=repo_name, package_id=package_id,
                                         bound_profile=bound_profile, use_color=self._use_color)
        print(f"  {label}")

    def _step_line(self, stage: str, step: ExecutionStep) -> str:
        action = render_execution_action_column(step.action, use_color=self._use_color)
        return f"{action} {step_target_display(stage, step)}".rstrip()

    def _print_status(self, result) -> None:
        step = result.step
        if result.status == "interrupted" and step.hook_plan is not None and step.hook_plan.io == "tty":
            # TTY hooks own their interrupt UI; a second line would be noise.
            return
        if result.status == "failed":
            self.shown_errors.add(result.error)
            # Hook output was streamed live, so only the exit status is new.
            detail = f"exit {result.exit_code}" if step.kind == "hook" and result.exit_code is not None else result.error
            print(f"      {detail}")
        print(f"      {render_execution_status(result.status, use_color=self._use_color)}", flush=True)


def step_target_display(stage: str, step: ExecutionStep) -> str:
    if step.hook_plan is not None:
        return step.hook_plan.command
    target = step.target_plan
    if target is None:
        return step.description or step.kind
    ui = current_ui_config()
    compact = not (ui and ui.full_paths)
    # Repository Apply writes sources; Live Publication writes live endpoints.
    path = display_review_path(target.repo_path if stage == "repository-apply" else target.live_path, compact=compact)
    return f"{target.chmod} {path}" if step.action == "chmod" and target.chmod else path
