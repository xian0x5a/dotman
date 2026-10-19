from __future__ import annotations

from pathlib import Path
from typing import Any
import tomllib

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
