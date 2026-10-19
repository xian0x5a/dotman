"""Three-way reconciliation of frozen repository representations."""

from dotman.command_runtime import CommandRuntime
from dotman.sync_base_store import FilePresent, DirectoryChildPresent, Missing, SyncBasePayload
from dotman.text_merge import TextMergeFailed, merge_text


MERGE_LABELS = ("repository", "Sync Base", "Capture")
# Git's default 7-character markers carrying the labels above, in zdiff3 order.
CONFLICT_MARKER_LINES = tuple(
    f"{marker} {label}".encode() for marker, label in zip(("<<<<<<<", "|||||||", ">>>>>>>"), MERGE_LABELS)
)


def unresolved_conflict_blocks(content: bytes, *, known_sources: tuple[bytes, ...]) -> tuple[bytes, ...]:
    """Complete conflict blocks this merge would write that none of `known_sources` already contains.

    Blocks already present in a frozen source, or using other labels, are intentional content.
    """
    start, base, end = CONFLICT_MARKER_LINES
    expected = (start, base, b"=======", end)
    blocks, current = [], None
    for line in content.splitlines(keepends=True):
        stripped = line.rstrip(b"\r\n")
        if stripped == start:
            current, stage = [], 0
        if current is not None:
            current.append(line)
            if stage < len(expected) and stripped == expected[stage]:
                stage += 1
                if stage == len(expected):
                    blocks.append(b"".join(current))
                    current = None
    return tuple(block for block in blocks if not any(block in source for source in known_sources))


class ReconciliationConflict(ValueError):
    """The frozen sides require an explicit resolution decision.

    `conflict` holds the zdiff3 merge output to resolve in the Editor; it is None
    when the sides disagree on presence and no text merge was possible.
    """

    def __init__(self, message: str, conflict: SyncBasePayload | None = None) -> None:
        super().__init__(message)
        self.conflict = conflict


class ReconciliationFailed(ValueError):
    """The reconciliation provider failed; its frozen operands remain retryable."""


def _empty_ancestor(captured: FilePresent | DirectoryChildPresent) -> FilePresent | DirectoryChildPresent:
    """Ancestor for add/add: both sides created the file since the Base recorded it Missing.

    Like Git, merge against empty content so differing additions yield conflict
    blocks for the Editor. With no recorded executable state, the ancestor takes
    Capture's, so a disagreement keeps the repository's declared state.
    """
    if isinstance(captured, DirectoryChildPresent):
        return DirectoryChildPresent(b"", captured.executable)
    return FilePresent(b"")


def reconcile(
    base: SyncBasePayload,
    repository: SyncBasePayload,
    captured: SyncBasePayload,
    *,
    command_runtime: CommandRuntime,
) -> SyncBasePayload:
    if repository == captured:
        return repository
    if repository == base:
        return captured
    if captured == base:
        return repository
    if isinstance(base, Missing) and type(repository) is type(captured) is not Missing:
        base = _empty_ancestor(captured)
    if not all(isinstance(value, (FilePresent, DirectoryChildPresent)) for value in (base, repository, captured)):
        raise ReconciliationConflict("Repository and Capture disagree on file presence")
    if isinstance(repository, DirectoryChildPresent):
        if not isinstance(base, DirectoryChildPresent) or not isinstance(captured, DirectoryChildPresent):
            raise ValueError("Child reconciliation requires child ancestry and Capture")
        # Bytes and Git executable state are independent merge operands. A mode
        # change must not turn otherwise non-overlapping byte changes into conflict.
        executable = (repository.executable if repository.executable == captured.executable
                      else captured.executable if repository.executable == base.executable
                      else repository.executable)
        try:
            content = reconcile(
                FilePresent(base.content), FilePresent(repository.content), FilePresent(captured.content),
                command_runtime=command_runtime,
            ).content
        except ReconciliationConflict as exc:
            raise ReconciliationConflict(str(exc), DirectoryChildPresent(exc.conflict.content, executable)) from exc
        return DirectoryChildPresent(content, executable)
    try:
        merged = merge_text(
            repository.content, base.content, captured.content,
            labels=MERGE_LABELS, command_runtime=command_runtime,
        )
    except TextMergeFailed as exc:
        raise ReconciliationFailed(f"Reconciliation failed with {exc}") from exc
    except OSError as exc:
        raise ReconciliationFailed(str(exc)) from exc
    if merged.conflicted:
        raise ReconciliationConflict("Repository and Capture contain conflicting changes", FilePresent(merged.content))
    return FilePresent(merged.content)
