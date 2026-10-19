from __future__ import annotations

import re
from pathlib import Path
from typing import Any
import tomllib

import tomlkit

from dotman.errors import UsageError


class TomlLoadError(UsageError):
    def __init__(
        self,
        *,
        context: str,
        path: Path | None,
        detail: str,
        package_repo: str | None = None,
        package_id: str | None = None,
    ) -> None:
        self.context = context
        self.path = path
        self.detail = detail
        self.package_repo = package_repo
        self.package_id = package_id
        super().__init__(detail)

    def __str__(self) -> str:
        return format_toml_load_error(self)


def load_toml_file(
    path: Path,
    *,
    context: str,
    package_repo: str | None = None,
    package_id: str | None = None,
) -> Any:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise TomlLoadError(
            context=context,
            path=path,
            detail=str(exc),
            package_repo=package_repo,
            package_id=package_id,
        ) from exc


def load_toml_text(
    text: str,
    *,
    context: str,
    path: Path | None = None,
    package_repo: str | None = None,
    package_id: str | None = None,
) -> Any:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlLoadError(
            context=context,
            path=path,
            detail=str(exc),
            package_repo=package_repo,
            package_id=package_id,
        ) from exc


def format_toml_load_error(error: TomlLoadError) -> str:
    location = error.context
    if error.package_repo is not None and error.package_id is not None:
        location = f"{location} for '{error.package_repo}:{error.package_id}'"
    if error.path is not None:
        location = f"{location} {error.path}"
    return f"invalid TOML in {location}: {error.detail}"


# Every backslash in tomlkit's output starts an escape (it writes basic strings
# only), so matching `\\.` pairs left to right keeps `\\e`, an escaped backslash
# then a literal `e`, intact.
_TOML_ESCAPE = re.compile(r"\\(.)", re.DOTALL)


def dump_toml_text(data: dict[str, Any]) -> str:
    """TOML text for dotman state and manifests, which `load_toml_file` must read back.

    tomlkit writes ESC as `\\e`, a TOML 1.1 escape that `tomllib` (TOML 1.0)
    rejects (#304), so ESC is written as `\\u001b` instead.
    """
    return _TOML_ESCAPE.sub(
        lambda match: "\\u001b" if match.group(1) == "e" else match.group(0),
        tomlkit.dumps(data),
    )
