from __future__ import annotations

import os
import sys
from pathlib import Path

from dotman.errors import UsageError
from dotman.rewrites.home import active_home_path, collapse_home_paths, expand_home_paths


class HomeRewriteError(ValueError):
    def __init__(self, detail: str, *, path: Path | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.path = path


class HomeRewriteUsageError(HomeRewriteError, UsageError):
    """The caller named an input that cannot be used, as opposed to a failure while reading it."""


def _read_input(input_path: str | None) -> tuple[bytes, Path | None]:
    if input_path is None or input_path == "-":
        try:
            return sys.stdin.buffer.read(), None
        except OSError as error:
            raise HomeRewriteError(f"could not read stdin: {error.strerror or error}") from error

    path = Path(input_path)
    try:
        return path.read_bytes(), path
    except FileNotFoundError as error:
        raise HomeRewriteUsageError("input file does not exist", path=path) from error
    except PermissionError as error:
        raise HomeRewriteError("input file is not readable", path=path) from error
    except OSError as error:
        raise HomeRewriteError(f"could not read input: {error.strerror or error}", path=path) from error


def run_home_rewrite(*, action: str, input_path: str | None, bare_tilde: bool) -> int:
    home = active_home_path(os.environ)

    source_bytes, source_path = _read_input(input_path)
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HomeRewriteError("input is not valid UTF-8", path=source_path) from error

    rewrite = expand_home_paths if action == "expand" else collapse_home_paths
    rewritten_text = rewrite(source_text, home=home, bare_tilde=bare_tilde)
    # Preserve the original bytes on a no-op so byte identity is explicit and
    # cannot be weakened by a future change to text encoding behavior.
    output_bytes = source_bytes if rewritten_text == source_text else rewritten_text.encode("utf-8")
    sys.stdout.buffer.write(output_bytes)
    return 0
