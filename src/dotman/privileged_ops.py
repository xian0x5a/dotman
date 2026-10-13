from __future__ import annotations

import os
import sys
from pathlib import Path

from dotman.atomic_files import write_bytes_atomic, write_symlink_atomic
from dotman.repo_access import restore_repo_path_access_for_invoking_user


class PrivilegedOperationError(RuntimeError):
    pass



def _write_bytes_atomic(path: Path, *, restore_root: Path | None, mode: int | None) -> None:
    write_bytes_atomic(path, sys.stdin.buffer.read(), mode=mode)
    if restore_root is not None:
        restore_repo_path_access_for_invoking_user(path, repo_root=restore_root)



def _write_symlink_atomic(path: Path, target: str) -> None:
    write_symlink_atomic(path, target)



def _delete_path_and_prune_empty_parents(path: Path, *, root: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()
    prune_root = root if root.is_dir() else root.parent
    current = path.parent
    while current.exists() and current != prune_root and current != current.parent:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent



def _chmod(path: Path, mode: int) -> None:
    if not path.exists():
        return
    os.chmod(path, mode)




def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("missing privileged operation", file=sys.stderr)
        return 2

    command = args.pop(0)
    try:
        if command == "write-bytes-atomic":
            if not 1 <= len(args) <= 3:
                raise PrivilegedOperationError("write-bytes-atomic requires: PATH [RESTORE_ROOT] [MODE]")
            restore_root = Path(args[1]) if len(args) >= 2 and args[1] != "-" else None
            mode = int(args[2]) if len(args) == 3 else None
            _write_bytes_atomic(Path(args[0]), restore_root=restore_root, mode=mode)
            return 0
        if command == "write-symlink-atomic":
            if len(args) != 2:
                raise PrivilegedOperationError("write-symlink-atomic requires: PATH TARGET")
            _write_symlink_atomic(Path(args[0]), args[1])
            return 0
        if command == "delete-path-and-prune-empty-parents":
            if len(args) != 2:
                raise PrivilegedOperationError("delete-path-and-prune-empty-parents requires: PATH ROOT")
            _delete_path_and_prune_empty_parents(Path(args[0]), root=Path(args[1]))
            return 0
        if command == "chmod":
            if len(args) != 2:
                raise PrivilegedOperationError("chmod requires: PATH MODE")
            _chmod(Path(args[0]), int(args[1]))
            return 0
    except PrivilegedOperationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"unknown privileged operation: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
