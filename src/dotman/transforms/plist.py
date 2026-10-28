from __future__ import annotations

from pathlib import Path
import plistlib
from typing import Any
from xml.parsers.expat import ExpatError

from dotman.transforms.framework import (
    MappingTransformEngine,
    SelectorSpec,
    TransformOutput,
    TransformRequest,
    STDIN_PATH,
    identity_key_text,
    semantically_unchanged_compare_output,
)


PlistDict = dict[str, Any]


def load_plist(path: Path, *, stdin_bytes: bytes | None = None) -> PlistDict:
    if path == STDIN_PATH:
        assert stdin_bytes is not None
        content = stdin_bytes
    elif not path.exists():
        return {}
    else:
        content = path.read_bytes()

    try:
        loaded = plistlib.loads(content)
    except (AttributeError, ExpatError, ValueError) as error:
        # plistlib reports malformed XML as ExpatError and a malformed value such
        # as `<date>nope</date>` as AttributeError, not only as ValueError.
        raise ValueError(f"invalid plist in {path}: {error}") from error

    if not isinstance(loaded, dict):
        raise ValueError(f"Expected plist dictionary in {path}")

    return loaded


def plist_format_from_name(format_name: str) -> int:
    return plistlib.FMT_XML if format_name == "xml" else plistlib.FMT_BINARY


def plist_bytes(data: PlistDict, fmt: str) -> bytes:
    try:
        return plistlib.dumps(data, fmt=plist_format_from_name(fmt), sort_keys=True)
    except (TypeError, OverflowError) as error:
        # Some values only fit one format: UID needs binary, and both formats
        # cap integers at 64 bits.
        raise ValueError(f"cannot write {fmt} plist: {error}") from error


class PlistTransformEngine(MappingTransformEngine):
    name = "plist"
    KEY_PATH_FORMAT_NAME = "plist"
    KEY_REGEX_DESCRIPTION = "plist key path selector"
    COMPARE_FILE_HELP = "Optional plist to compare against for semantic no-op byte reuse"
    SELECTOR_SPECS = (
        SelectorSpec(
            name="key",
            prefix="exact",
            is_default=True,
            description="exact plist dictionary key path",
        ),
        SelectorSpec(
            name="key_regex",
            prefix="re",
            description="regex matching dotted plist dictionary key paths",
        ),
    )

    load = staticmethod(load_plist)
    key_text = staticmethod(identity_key_text)

    def configure_parser(self, parser) -> None:
        super().configure_parser(parser)
        parser.add_argument(
            "--output-format",
            choices=("xml", "binary"),
            default="xml",
            help="Serialization format for the output plist",
        )

    def build_engine_options(self, parsed_args) -> dict[str, Any]:
        return {**super().build_engine_options(parsed_args), "output_format": parsed_args.output_format}

    def build_output(self, data: PlistDict, request: TransformRequest) -> TransformOutput:
        reused_output = semantically_unchanged_compare_output(
            request.engine_option("compare_path"),
            data,
            plistlib.loads,
            mode_reference_path=request.base_path,
        )
        if reused_output is not None:
            return reused_output
        return TransformOutput(
            content=plist_bytes(data, str(request.engine_option("output_format", "xml"))),
            mode_reference_path=request.base_path,
        )
