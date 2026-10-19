from __future__ import annotations

import copy
import functools
import platform
import shlex
from collections.abc import Callable, Iterator
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template, TemplateNotFound, TemplateSyntaxError, Undefined, UndefinedError, meta, nodes


class DotmanUndefined(Undefined):
    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        self._fail_with_undefined_error()
        return ""


class JinjaRenderError(ValueError):
    def __init__(self, path: Path | None, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(detail)

    def __str__(self) -> str:
        return format_jinja_render_error(self)


def _shell_args(value: Any) -> str:
    if not isinstance(value, list) or any(not isinstance(element, str) for element in value):
        raise ValueError("shell_args requires a flat list of strings")
    return " ".join(shlex.quote(element) for element in value)


def _register_filters(environment: Environment) -> Environment:
    environment.filters["shell_args"] = _shell_args
    return environment


@functools.cache
def _string_environment(base_dir: Path) -> Environment:
    return _register_filters(
        Environment(
            autoescape=False,
            loader=FileSystemLoader(str(base_dir)),
            undefined=DotmanUndefined,
            keep_trailing_newline=True,
            # This environment lives for the whole process; its include cache
            # would only trust mtimes, so re-read included files on every render.
            cache_size=0,
        )
    )


@functools.cache
def _compiled_string_template(base_dir: Path, value: str) -> Template:
    # Planning re-resolves the same vars and target strings for every package,
    # so compiling each distinct string once dominates sync startup.
    return _string_environment(base_dir).from_string(value)


def _file_environment(base_dir: Path) -> Environment:
    return _register_filters(
        Environment(
            autoescape=False,
            loader=FileSystemLoader(str(base_dir)),
            undefined=DotmanUndefined,
            keep_trailing_newline=True,
            # File templates are line-oriented config text, so standalone control
            # lines should not leave extra blank lines in rendered output.
            trim_blocks=True,
            lstrip_blocks=True,
        )
    )


def template_syntax_tokens(source: str) -> tuple[tuple[str, str], ...]:
    """Return the Jinja tokens of `source` other than literal text and tag whitespace."""
    return tuple(
        (token_type, value)
        for _line, token_type, value in _file_environment(Path(".")).lex(source)
        if token_type not in {"data", "whitespace"}
    )


VarPath = tuple[str | int, ...]


def _is_template_string(value: Any) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _template_strings(value: Any, path: VarPath = ()) -> Iterator[tuple[VarPath, str]]:
    """Yield the path and source of every template string nested in dicts and lists."""
    if _is_template_string(value):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _template_strings(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _template_strings(child, (*path, index))


def _static_reference(node: nodes.Node) -> VarPath | None:
    """Return the constant lookup chain of `name.attr['key'][0]`, or None when any step is dynamic."""
    if isinstance(node, nodes.Name):
        return (node.name,)
    if isinstance(node, nodes.Getattr):
        base = _static_reference(node.node)
        return None if base is None else (*base, node.attr)
    if isinstance(node, nodes.Getitem) and isinstance(node.arg, nodes.Const) and isinstance(node.arg.value, str | int):
        base = _static_reference(node.node)
        return None if base is None else (*base, node.arg.value)
    return None


def _lookup_chains(node: nodes.Node) -> Iterator[VarPath]:
    reference = _static_reference(node)
    if reference is not None:
        yield reference
        return
    # A dynamic step still exposes its static prefix, e.g. `vars[name]` yields `vars`.
    for child in node.iter_child_nodes():
        yield from _lookup_chains(child)


@functools.cache
def _template_references(source: str) -> tuple[VarPath, ...]:
    try:
        parsed = _string_environment(Path(".")).parse(source)
    except TemplateSyntaxError as exc:
        raise JinjaRenderError(path=None, detail=str(exc)) from exc
    context_names = meta.find_undeclared_variables(parsed)
    return tuple(chain for chain in _lookup_chains(parsed) if chain[0] in context_names)


def _referenced_var_path(reference: VarPath, variables: dict[str, Any]) -> VarPath | None:
    """Map a lookup chain to the deepest var node it can reach, or None when it names no var.

    Steps that are not keys (such as `.items()`) stop the walk, so the reference
    depends on the whole node reached so far. A computed key (`vars[name]`) is
    not known before rendering, so it depends on every var; when that includes
    the referencing var itself, it is reported as a cycle rather than guessed.
    """
    head, *steps = reference
    if head == "vars":
        node: Any = variables
        path: VarPath = ()
    elif head in variables:
        node, path = variables[head], (head,)
    else:
        return None
    for step in steps:
        if isinstance(node, dict) and step in node:
            node = node[step]
        elif isinstance(node, list) and isinstance(step, int) and -len(node) <= step < len(node):
            # Template string paths use non-negative indexes, so `items[-1]` must match them.
            step %= len(node)
            node = node[step]
        else:
            break
        path = (*path, step)
    return path


def _paths_overlap(first: VarPath, second: VarPath) -> bool:
    shorter = min(len(first), len(second))
    return first[:shorter] == second[:shorter]


def _format_var_path(path: VarPath) -> str:
    return "".join(f"[{step}]" if isinstance(step, int) else f".{step}" for step in path).lstrip(".")


def _assign(container: Any, path: VarPath, value: Any) -> None:
    for step in path[:-1]:
        container = container[step]
    container[path[-1]] = value


def _resolve_vars_templates(variables: dict[str, Any]) -> dict[str, Any]:
    """Render every var template string once, after the var nodes it references.

    Rendering once keeps `{% raw %}` output literal; a reference cycle has no
    valid order and is reported instead of leaving template text in values.
    """
    template_sources = dict(_template_strings(variables))
    dependencies = {
        path: {
            other_path
            for reference in _template_references(source)
            if (target := _referenced_var_path(reference, variables)) is not None
            for other_path in template_sources
            if _paths_overlap(other_path, target)
        }
        for path, source in template_sources.items()
    }
    try:
        render_order = tuple(TopologicalSorter(dependencies).static_order())
    except CycleError as exc:
        cycle = " -> ".join(_format_var_path(path) for path in exc.args[1])
        raise JinjaRenderError(path=None, detail=f"vars reference each other in a cycle: {cycle}") from exc

    resolved = copy.deepcopy(variables)
    for path in render_order:
        source = template_sources[path]
        rendered = _render_template(lambda: _compiled_string_template(Path("."), source), {**resolved, "vars": resolved})
        _assign(resolved, path, rendered)
    return resolved


# `uname -m` names one CPU differently per OS (Linux `aarch64` vs macOS `arm64`,
# FreeBSD `amd64` vs `x86_64`). Fold them so templates compare against a single
# value; the canonical pair matches macOS `uname -m` and electron-builder assets.
CPU_ARCH_ALIASES = {"aarch64": "arm64", "amd64": "x86_64"}


def resolve_cpu_arch(variables: dict[str, Any], override: str | None = None) -> str:
    # Like `os`, an explicit `cpu_arch` var wins so a repo can render for another machine.
    explicit = variables.get("cpu_arch")
    if override:
        cpu_arch = override
    elif isinstance(explicit, str):
        cpu_arch = explicit
    else:
        cpu_arch = platform.machine()
    return CPU_ARCH_ALIASES.get(cpu_arch, cpu_arch)


def build_template_context(
    variables: dict[str, Any],
    *,
    profile: str,
    inferred_os: str,
    cpu_arch: str | None = None,
) -> dict[str, Any]:
    resolved = _resolve_vars_templates(variables)
    context = dict(resolved)
    context["vars"] = resolved
    context["profile"] = profile
    context["os"] = inferred_os
    context["cpu_arch"] = resolve_cpu_arch(resolved, cpu_arch)
    return context


def format_jinja_render_error(error: JinjaRenderError) -> str:
    location = f" for {error.path}" if error.path is not None else ""
    return f"jinja render failed{location}: {error.detail}"


def _render_template(
    compile_template: Callable[[], Template],
    context: dict[str, Any],
    *,
    source_path: Path | None = None,
) -> str:
    try:
        return compile_template().render(context)
    except (TemplateNotFound, TemplateSyntaxError, UndefinedError, ValueError) as exc:
        raise JinjaRenderError(path=source_path, detail=str(exc)) from exc


def render_template_string(
    value: str,
    context: dict[str, Any],
    *,
    base_dir: Path,
    source_path: Path | None = None,
) -> str:
    return _render_template(lambda: _compiled_string_template(base_dir, value), context, source_path=source_path)


def discover_template_file_dependencies(path: Path) -> tuple[Path, ...]:
    env = _file_environment(path.parent)
    loader = env.loader
    if not isinstance(loader, FileSystemLoader):  # pragma: no cover - guarded by _file_environment.
        raise ValueError("jinja dependency discovery requires a filesystem loader")

    source_name = path.name
    visited_names: set[str] = set()
    discovered_paths: list[Path] = []

    def visit(template_name: str) -> None:
        if template_name in visited_names:
            return
        visited_names.add(template_name)
        try:
            source_text, filename, _uptodate = loader.get_source(env, template_name)
        except TemplateNotFound as exc:
            raise ValueError(f"jinja template dependency not found from {path}: {template_name}") from exc

        resolved_path = Path(filename).resolve()
        if resolved_path != path.resolve():
            discovered_paths.append(resolved_path)

        try:
            parsed = env.parse(source_text)
        except TemplateSyntaxError as exc:
            raise ValueError(f"jinja template dependency parse failed for {filename}: {exc}") from exc

        for reference in meta.find_referenced_templates(parsed):
            if reference is None:
                # Built-in Jinja reconcile must know the full editable source set
                # before launching the editor, so dynamic template references are
                # intentionally rejected instead of guessed.
                raise ValueError(
                    f"jinja reconcile requires static template references: {filename} contains a dynamic reference"
                )
            visit(reference)

    visit(source_name)
    return tuple(discovered_paths)



def render_template_file(
    path: Path, context: dict[str, Any], *, source_bytes: bytes | None = None,
) -> tuple[bytes, str]:
    try:
        source_text = (
            path.read_text(encoding="utf-8")
            if source_bytes is None
            else source_bytes.decode("utf-8")
        )
    except IsADirectoryError as exc:
        raise JinjaRenderError(path=path, detail="source path must be a file") from exc
    except UnicodeDecodeError as exc:
        raise JinjaRenderError(path=path, detail="source file must be UTF-8 text") from exc

    rendered = _render_template(lambda: _file_environment(path.parent).from_string(source_text), context, source_path=path)
    return rendered.encode("utf-8"), "template"
