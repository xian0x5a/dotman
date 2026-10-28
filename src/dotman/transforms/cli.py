from __future__ import annotations

import argparse
from pathlib import Path
import sys

from dotman.errors import UsageError
from dotman.transforms.framework import (
    EXCLUSION_PREFIX,
    STDIN_PATH,
    BaseTransformEngine,
    SelectorAction,
    TransformMode,
    TransformRequest,
    emit_transform_output,
)


def parse_selectors(
    engine: BaseTransformEngine, selectors: list[str]
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """Split selectors into included and excluded values, each keyed by selector type."""
    included_by_type: dict[str, list[str]] = {spec.name: [] for spec in engine.selector_specs()}
    excluded_by_type: dict[str, list[str]] = {spec.name: [] for spec in engine.selector_specs()}
    prefix_to_spec = {f"{spec.prefix}:": spec for spec in engine.selector_specs()}
    default_spec = next((spec for spec in engine.selector_specs() if spec.is_default), None)

    for selector in selectors:
        target_by_type = included_by_type
        if selector.startswith(EXCLUSION_PREFIX):
            selector = selector[len(EXCLUSION_PREFIX):]
            target_by_type = excluded_by_type

        matched = False
        for prefix, spec in prefix_to_spec.items():
            if selector.startswith(prefix):
                target_by_type[spec.name].append(selector[len(prefix):])
                matched = True
                break
        if not matched:
            if default_spec is None:
                raise UsageError(
                    f"Engine {engine.name} has no default selector spec, but no prefix was matched for: {selector}"
                )
            target_by_type[default_spec.name].append(selector)

    return (
        {name: tuple(values) for name, values in included_by_type.items()},
        {name: tuple(values) for name, values in excluded_by_type.items()},
    )



def selector_help(engine: BaseTransformEngine) -> str:
    specs = engine.selector_specs()
    default_spec = next((spec for spec in specs if spec.is_default), None)
    prefix_descriptions = "; ".join(
        f"{spec.prefix}: {spec.description}" for spec in specs
    )
    default_description = (
        f"Unprefixed selectors use {default_spec.prefix}: ({default_spec.description}). "
        if default_spec is not None
        else ""
    )
    exclusion_description = (
        f" Prefix any selector with {EXCLUSION_PREFIX} to remove its paths and their"
        " subtrees from the selection; with only such selectors the rest of the"
        " document is selected."
        if engine.SUPPORTS_EXCLUDED_SELECTORS
        else ""
    )
    return (
        f"Base-file selectors. {default_description}Prefixes: {prefix_descriptions}."
        f"{exclusion_description}"
    )


def configure_parser(parser: argparse.ArgumentParser, engine: BaseTransformEngine) -> None:
    parser.add_argument(
        "base_path",
        type=Path,
        help="Base file; selectors always apply to this file",
    )
    parser.add_argument(
        "output_path",
        nargs="?",
        type=Path,
        help="Transformed output path; optional when --stdout is used",
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in TransformMode],
        required=True,
        help="Transform mode",
    )
    parser.add_argument(
        "--overlay-file",
        "--merge-file",
        dest="overlay_path",
        type=Path,
        help="Overlay file applied on top of the filtered base; required when --mode=merge",
    )
    parser.add_argument(
        "--selector-type",
        choices=[action.value for action in SelectorAction],
        default=SelectorAction.RETAIN.value,
        help="Preserve or remove the selected region from the base file",
    )
    parser.add_argument(
        "--selectors",
        nargs="*",
        default=[],
        help=selector_help(engine),
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write the transformed output to stdout instead of a file",
    )

    engine.configure_parser(parser)


def build_request(
    parser: argparse.ArgumentParser,
    engine: BaseTransformEngine,
    parsed_args: argparse.Namespace,
) -> TransformRequest:
    try:
        selectors_by_type, excluded_selectors_by_type = parse_selectors(
            engine, getattr(parsed_args, "selectors", [])
        )
    except ValueError as e:
        parser.error(str(e))

    request = TransformRequest(
        base_path=parsed_args.base_path,
        output_path=parsed_args.output_path,
        mode=TransformMode(parsed_args.mode),
        selector_action=SelectorAction(
            getattr(parsed_args, "selector_type", SelectorAction.RETAIN.value)
        ),
        selectors_by_type=selectors_by_type,
        excluded_selectors_by_type=excluded_selectors_by_type,
        overlay_path=parsed_args.overlay_path,
        engine_options=engine.build_engine_options(parsed_args),
    )

    try:
        engine.validate_request(request)
    except ValueError as error:
        parser.error(str(error))

    return request



def run_parsed_engine(engine: BaseTransformEngine, parser: argparse.ArgumentParser, parsed_args: argparse.Namespace) -> int:
    stdin_inputs = [
        path for path in (parsed_args.base_path, parsed_args.overlay_path) if path == STDIN_PATH
    ]
    if len(stdin_inputs) > 1:
        raise UsageError("at most one of BASE and --overlay-file may read from stdin ('-')")

    if getattr(parsed_args, "compare_file", None) == STDIN_PATH:
        parser.error("--compare-file must be a file path; stdin ('-') is not supported")

    parsed_args.stdin_bytes = read_stdin_bytes() if stdin_inputs else None
    if parsed_args.output_path == STDIN_PATH:
        parsed_args.stdout = True
        parsed_args.output_path = None
    request = build_request(parser, engine, parsed_args)
    try:
        output = engine.transform(request)
        emit_transform_output(
            request.output_path,
            output,
            stdout=bool(request.engine_option("stdout", False)),
        )
    except OSError as error:
        # Unreadable inputs and unwritable outputs are user errors; report
        # them like other transform errors instead of as a traceback.
        raise ValueError(str(error)) from error
    return 0


def read_stdin_bytes() -> bytes:
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is not None:
        return stdin_buffer.read()
    return sys.stdin.read().encode("utf-8")
