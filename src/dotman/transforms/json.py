from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any

from dotman.transforms.framework import (
    MappingTransformEngine,
    SelectorSpec,
    TransformOutput,
    TransformRequest,
    identity_key_text,
    read_input_text,
    read_reference_text,
    semantically_unchanged_compare_output,
)


JsonDict = dict[str, Any]
DEFAULT_JSON_INDENT = "  "
_JSON_INDENT_RE = re.compile(r"^([ \t]+)\S")
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def parse_finite_json_float(number_text: str) -> float:
    # json.loads turns an out-of-range number such as 1e400 into inf, which
    # would be written back as the invalid JSON token Infinity.
    value = float(number_text)
    if math.isinf(value):
        raise ValueError(f"JSON number {number_text} is out of float range")
    return value


def reject_non_json_constant(token: str) -> float:
    # json.loads accepts NaN and Infinity as an extension, but they would be
    # written back as the same invalid JSON tokens.
    raise ValueError(f"{token} is not a valid JSON number")


def load_json(path: Path, *, stdin_bytes: bytes | None = None) -> JsonDict:
    source_text = read_input_text(path, stdin_bytes=stdin_bytes)
    if source_text is None:
        return {}

    loaded = json.loads(
        source_text,
        parse_float=parse_finite_json_float,
        parse_constant=reject_non_json_constant,
    )
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected top-level JSON object in {path}")
    return loaded



def detect_json_indent(text: str) -> str | None:
    for line in text.splitlines():
        match = _JSON_INDENT_RE.match(line)
        if match:
            return match.group(1)
    return None



def detect_json_indent_from_text(text: str | None) -> str | None:
    if text is None:
        return None
    try:
        json.loads(text)
    except ValueError:
        return None

    return detect_json_indent(text)



def select_json_indent(*reference_paths: Path | None, stdin_bytes: bytes | None = None) -> str:
    for reference_path in reference_paths:
        indent = detect_json_indent_from_text(
            read_reference_text(reference_path, stdin_bytes=stdin_bytes)
        )
        if indent is not None:
            return indent
    return DEFAULT_JSON_INDENT



def json_text(data: JsonDict, indent: str = DEFAULT_JSON_INDENT) -> str:
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    # JSON permits lone surrogate escapes such as "\ud800", but UTF-8 cannot
    # encode them raw, so keep those (and only those) escaped.
    return _LONE_SURROGATE_RE.sub(lambda match: f"\\u{ord(match.group()):04x}", text) + "\n"



class JsonTransformEngine(MappingTransformEngine):
    name = "json"
    KEY_PATH_FORMAT_NAME = "JSON"
    KEY_REGEX_DESCRIPTION = "JSON key selector"
    COMPARE_FILE_HELP = "Optional JSON file to compare against for semantic no-op text reuse"
    SELECTOR_SPECS = (
        SelectorSpec(
            name="key",
            prefix="exact",
            is_default=True,
            description="exact dotted or quoted nested JSON object key path",
        ),
        SelectorSpec(
            name="key_regex",
            prefix="re",
            description="regex matching full JSON object key paths",
        ),
    )

    load = staticmethod(load_json)
    key_text = staticmethod(identity_key_text)

    def build_output(self, data: JsonDict, request: TransformRequest) -> TransformOutput:
        compare_path = request.engine_option("compare_path")
        reused_output = semantically_unchanged_compare_output(
            compare_path,
            data,
            lambda content: json.loads(content.decode("utf-8-sig")),
            mode_reference_path=request.base_path,
        )
        if reused_output is not None:
            return reused_output

        indent = select_json_indent(
            compare_path,
            request.base_path,
            request.overlay_path,
            stdin_bytes=request.engine_option("stdin_bytes"),
        )
        return TransformOutput(
            content=json_text(data, indent=indent),
            mode_reference_path=request.base_path,
        )
