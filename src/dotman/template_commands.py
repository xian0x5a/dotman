from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from dotman.errors import UsageError
from dotman.templates import (
    JinjaRenderError,
    build_template_context,
    render_template_file,
    resolve_cpu_arch,
)


def _assign_nested_value(target: dict[str, object], key_parts: Sequence[str], value: str) -> None:
    current = target
    for key in key_parts[:-1]:
        nested = current.get(key)
        if not isinstance(nested, dict):
            nested = {}
            current[key] = nested
        current = nested
    current[key_parts[-1]] = value


def _template_vars_from_dotman_env(environ: dict[str, str]) -> dict[str, object]:
    variables: dict[str, object] = {}
    for key, value in environ.items():
        if not key.startswith("DOTMAN_VAR_"):
            continue
        path_parts = [part for part in key.removeprefix("DOTMAN_VAR_").split("__") if part]
        if path_parts:
            _assign_nested_value(variables, path_parts, value)
    return variables


def _apply_template_var_assignments(
    variables: dict[str, object],
    assignments: Sequence[str],
) -> dict[str, object]:
    for assignment in assignments:
        if "=" not in assignment:
            raise UsageError(f"invalid --var assignment '{assignment}'; expected <key=value>")
        dotted_key, value = assignment.split("=", 1)
        key_parts = [part for part in dotted_key.split(".") if part]
        if not key_parts:
            raise UsageError(f"invalid --var assignment '{assignment}'; expected <key=value>")
        _assign_nested_value(variables, key_parts, value)
    return variables


def run_jinja_render(
    *,
    source_path: str,
    profile: str | None,
    inferred_os: str | None,
    cpu_arch: str | None,
    var_assignments: Sequence[str],
) -> int:
    path = Path(source_path)
    variables = _template_vars_from_dotman_env(dict(os.environ))
    _apply_template_var_assignments(variables, var_assignments)
    if not path.exists():
        raise UsageError(f"source path does not exist: {path}")
    context = build_template_context(
        variables,
        profile=profile or os.environ.get("DOTMAN_PROFILE") or "default",
        inferred_os=inferred_os or os.environ.get("DOTMAN_OS") or sys.platform,
        cpu_arch=cpu_arch or os.environ.get("DOTMAN_CPU_ARCH"),
    )
    rendered, _projection_kind = render_template_file(path, context)
    sys.stdout.write(rendered.decode("utf-8"))
    return 0


def _build_patch_capture_cli_env(
    *,
    repo_path: Path,
    variables: dict[str, object],
    profile: str,
    inferred_os: str,
    cpu_arch: str,
) -> dict[str, str]:
    # Local import: `render jinja` loads this module and must not pay for the
    # manifest/models import chain that only patch capture needs.
    from dotman.manifest import flatten_vars

    env = {
        "DOTMAN_REPO_PATH": str(repo_path),
        "DOTMAN_SOURCE": str(repo_path),
        "DOTMAN_PROFILE": profile,
        "DOTMAN_OS": inferred_os,
        "DOTMAN_CPU_ARCH": cpu_arch,
    }
    for flat_key, value in flatten_vars(variables).items():
        env[f"DOTMAN_VAR_{flat_key}"] = value
    return env


def _build_cli_patch_capture_projector(
    *,
    repo_path: Path,
    render_command: str,
    variables: dict[str, object],
    profile: str,
    inferred_os: str,
    cpu_arch: str,
):
    if render_command == "jinja":
        context = build_template_context(
            variables,
            profile=profile,
            inferred_os=inferred_os,
            cpu_arch=cpu_arch,
        )

        def project(candidate_bytes: bytes) -> bytes:
            rendered, _projection_kind = render_template_file(repo_path, context, source_bytes=candidate_bytes)
            return rendered

        return project

    # Local import keeps the command runtime off the `render jinja` startup path.
    from dotman.command_runtime import (
        CommandRequest,
        ShellCommand,
        current_command_runtime,
        raise_for_command_interruption,
    )

    base_env = _build_patch_capture_cli_env(
        repo_path=repo_path,
        variables=variables,
        profile=profile,
        inferred_os=inferred_os,
        cpu_arch=cpu_arch,
    )

    def project(candidate_bytes: bytes) -> bytes:
        # The renderer may resolve sibling files relative to $DOTMAN_SOURCE, so
        # the transient candidate must stay beside the real repository source.
        with tempfile.NamedTemporaryFile(
            prefix=f".dotman-patch-{repo_path.stem}-",
            suffix=repo_path.suffix,
            dir=repo_path.parent,
            delete=False,
        ) as temp_source:
            temp_source.write(candidate_bytes)
            temp_source_path = Path(temp_source.name)
        try:
            temp_source_text = str(temp_source_path)
            result = current_command_runtime().run(
                CommandRequest(
                    command=ShellCommand(render_command),
                    cwd=repo_path.parent,
                    env={
                        **base_env,
                        "DOTMAN_REPO_PATH": temp_source_text,
                        "DOTMAN_SOURCE": temp_source_text,
                    },
                )
            )
            raise_for_command_interruption(result)
            if result.exit_code != 0:
                stderr = result.stderr.decode("utf-8", errors="replace")
                raise ValueError(stderr.strip() or f"render command exited with status {result.exit_code}")
            return result.stdout
        finally:
            temp_source_path.unlink(missing_ok=True)

    return project


def run_patch_capture(
    *,
    repo_path: str,
    render_command: str,
    review_repo_path: str | None,
    review_live_path: str | None,
    profile: str | None,
    inferred_os: str | None,
    cpu_arch: str | None,
    var_assignments: Sequence[str],
) -> int:
    # Local imports keep capture and the command runtime off the `render jinja`
    # startup path; manifests call that helper on every render.
    from dotman.capture import capture_patch
    from dotman.command_runtime import current_command_runtime

    for label, path_text in (
        ("repo path", repo_path),
        ("review repo path", review_repo_path),
        ("review live path", review_live_path),
    ):
        if path_text is not None and not Path(path_text).expanduser().exists():
            raise UsageError(f"{label} does not exist: {path_text}")
    resolved_repo_path = Path(repo_path).expanduser().resolve()
    variables = _template_vars_from_dotman_env(dict(os.environ))
    _apply_template_var_assignments(variables, var_assignments)
    resolved_profile = profile or os.environ.get("DOTMAN_PROFILE") or "default"
    resolved_os = inferred_os or os.environ.get("DOTMAN_OS") or sys.platform
    resolved_cpu_arch = resolve_cpu_arch(variables, cpu_arch or os.environ.get("DOTMAN_CPU_ARCH"))
    captured = capture_patch(
        repo_path=resolved_repo_path,
        review_repo_path=review_repo_path,
        review_live_path=review_live_path,
        project_repo_bytes=_build_cli_patch_capture_projector(
            repo_path=resolved_repo_path,
            render_command=render_command,
            variables=variables,
            profile=resolved_profile,
            inferred_os=resolved_os,
            cpu_arch=resolved_cpu_arch,
        ),
        command_runtime=current_command_runtime(),
        protect_template_syntax=render_command == "jinja",
    )
    sys.stdout.buffer.write(captured)
    return 0
