# Sync Base storage

Dotman stores Sync Bases separately from tracked-package state and snapshots.
Each configured repository has private file storage under its manager XDG state
directory. Each Sync Unit owns one self-contained record; payloads are not shared.

The layout under `${XDG_STATE_HOME:-$HOME/.local/state}/dotman/` is:

```text
repos/<state_key>/
  sync-bases.layout
  sync-bases.lock
  bases/<sha256-of-target-identity>/<sha256-of-unit-identity>.json
```

`state_key` is the configured repository storage key, not its filesystem path.
Records are grouped by target: a file target's group holds its one record, and
a directory target's group holds its children's records. An exact read opens
one computed path and a target-scoped read lists one group, so neither costs
more as the store grows. Names are hashes to avoid length, case-folding, and
file/directory clashes; each record also retains and validates its complete
identity and its location. These files share the repository state directory
with tracked-package state; resetting a Base does not change tracked packages
or snapshots. Deleting a group's last record removes the group.

`sync-bases.layout` holds the layout version (`2`), separate from the record
format epoch: a layout change moves records without changing their bytes, so
record epochs cannot detect it. Only this marker establishes a store, and
creation writes it last. Without it, a directory holds no trusted records:
preview and inspection read it as absent, `dotman doctor` warns, and the first
real Push, Pull, or Sync drops any records under `bases/` before writing the
marker. Other files, such as the flat `sync-base-*.json` records of the oldest
layout, are ignored and left in place. A store whose marker names another
layout fails every open, so it never reads as empty.

## Records and atomic acknowledgment

A record contains its canonical Sync Unit identity and typed repository-space payload: `Missing`, `Present(bytes)`, or a directory
child's `Present(bytes, executable)`. Missing is a stored value, not the absence
of a checkpoint. File targets do not store executable state; exact live chmod
is never a Base payload.

Records use canonical compact ASCII JSON, format epoch `2`, with a `record`
object and metadata `digest`. The record stores base64 identity and content,
payload shape, executable state, and content SHA-256
and byte count. The metadata digest covers every record field except content;
content is checked against its separately protected digest and size. Missing has
null content, content digest, size, and executable fields. Exact canonical
encoding, identity binding, record shape, and both integrity checks are validated
before use.

Metadata and payload are replaced together using a private temporary file and
atomic replacement in the same directory. A failure before replacement leaves
the previous authoritative record intact. Rename is the logical commit boundary:
if the subsequent directory flush fails, the new record is already visible.
Dotman reports acknowledgment with a durability-uncertain warning, not a failed
save or an assertion that the old record survived. It does not attempt another
fallible mutation to roll back that committed record. Unit acknowledgments are
independent; an operation is not a multi-unit transaction. Exact deletion affects one record,
without shared-payload garbage collection.

The shared [Base lifecycle](sync.md#sync-bases) owns eligibility, applicability,
checkpoint evidence, and acknowledgment timing. Storage does not infer whether
successful repository writes constituted synchronization.

## Read-only inspection and locking

Read-only opening never creates directories, records, or missing lock artifacts,
and cannot replace or delete records. Handles do not retain an exclusive lock
throughout their lifetime.

- Each read sees current committed records, not an earlier handle-level cache.
- Grouped reads hold a shared repository storage lock for a coherent view.
- Replacement and deletion use the exclusive repository storage lock.
- Contention fails immediately rather than waiting.
- These locks are separate from the manager operation lock. Real operations
  acquire the manager lock first; list, info, and doctor never acquire it.

Collect inspection data under a short read transaction, then release it before
rendering output or waiting for review. Holding a read lock during review would
unnecessarily block checkpoint writes.

## Filesystem trust and failures

The manager state directory and managed storage directories are current-user-owned
with mode `0700`. Records, temporary files, and locks are current-user-owned
regular files with mode `0600`. Storage rejects symlinks, hard-linked or nonregular
files, unsafe ownership, and invalid directory/file bindings. Writable opens
tighten the modes of current-user-owned managed directories and store files
through their pinned descriptors, then re-validate. Read-only opens reject
nonprivate modes without changing them. Storage never requests root access.

Descriptor-relative operations, no-follow opens, and binding validation protect
the managed private tree. The caller trusts the XDG parent outside that tree.
Advisory locks coordinate cooperating Dotman processes, not hostile same-user or
root processes bypassing locks or modifying open inodes.

Record/envelope corruption is distinct from payload-integrity corruption.
One corrupt record does not hide healthy records: coherent inventory returns
validated records and an aggregate corruption count, including damaged records
whose identity cannot be recovered. Exact info reads only the requested record.
Read-only inspection performs no cleanup. Real operations may perform normal
record-level policy and obsolete-child maintenance only inside safely accessible storage;
unsafe or unreadable storage is never automatically recreated or repaired beyond
the mode tightening above.

During Push, Pull, or Sync, unavailable/corrupt Base reads and checkpoint-save
failures are warnings. They do not turn successful required effects into failure
or stop later units. A failed read uses normal no-Base resolution and reports
Base reason `unreadable`, distinct from `absent` (no Base recorded yet); explicit Merge
remains blocked without a usable Base. A save rejected before atomic replacement
leaves the previous authoritative checkpoint rather than rolling back repository
or live changes. A post-replacement flush warning instead reports the new Base
as advanced, with crash durability unconfirmed.
Required operation safety and effect failures still stop execution.

Inspection and explicit reset retain their own error reporting: unsafe or
unreadable storage is not reported as a successful empty inspection. No repair,
quarantine, or bulk-reset command is provided.

Atomic replacement depends on the host filesystem's rename and durability
semantics. Storage uses POSIX descriptor-relative filesystem operations and
`flock`; native platform support must include those facilities.
See Python's [filesystem operations](https://docs.python.org/3/library/os.html)
and [advisory locking](https://docs.python.org/3/library/fcntl.html#fcntl.flock).

## Obsolete directory-child identities

A child deletion may save its `Missing` checkpoint independently. Only a later
successful real Sync with complete unrestricted target census proof may reclaim
the absent identity. Candidate keys are frozen before review, so the operation
cannot reclaim a deletion it just acknowledged. Partial selectors, exclusions,
ignores, markers, Guard restrictions, and discovery failures cannot provide that
proof; preview and aborted sessions do no reclamation.
Reclamation does not create missing storage. If a directory's stored Bases
cannot be read, only that directory's units warn `base-maintenance-failed`, and
its obsolete Bases wait for a later run.

## Public inspection and reset

- `dotman list sync-bases` returns only currently usable entries, including usable
  Missing payloads. Empty output succeeds; unusable metadata is not disclosed.
- `dotman info sync-base main:app.settings` inspects exactly one current file Sync
  Unit. A directory child uses `main:app.tree/nested/file`; package instances use
  `main:app<work>.tree/nested/file`.
- `dotman reset sync-base main:app.settings` immediately discards exactly that
  record. Already absent succeeds. There is no confirmation, preview, wildcard,
  fuzzy selector, package scope, directory-target scope, or all-units form.
  Reset takes the manager non-blocking operation lock before resolution and fails
  while a real Push, Pull, or Sync owns it.
- `dotman doctor` warns with aggregate corrupt and proven orphaned record counts
  per repository, without identities or repair plans. A complete unrestricted
  directory census can prove absent children; excluded, guarded, or failed
  discovery cannot. Unsafe/unreadable stores report repository, path, and cause;
  unversioned stores warn that the next real Push, Pull, or Sync replaces them.

Info reports `usable`, `unavailable`, or human `not applicable` (structured
`not-applicable`). Unavailable/ineligible inspection succeeds; invalid identities
and store failures are errors. Unavailability reasons are `absent`, `ineligible`,
or `corrupt`. Structured reason codes distinguish `record_corrupt` and
`payload_corrupt`.

List and info expose canonical identity, policy, eligibility, status/reason,
payload kind, size, digest, and child executable state, with the integrity check. Unknown/not-performed checks are null. Unavailable output
does not disclose stale payload metadata; payload bytes are never output.
Reset reports identity and `reset` or `already_absent`.

List, info, and doctor never inspect Verification Records or run Guards, Render,
Capture, Pull View, live comparison, or drift Observation. Static resolution and
doctor directory-control census do not establish a checkpoint. A stored Missing
directory-child Base remains inspectable until a real operation safely reclaims it.

### Real Push maintenance boundary

Real Push performs configured-ineligibility cleanup while holding the manager
operation lock, after successful static ownership/conflict resolution and before
Guards and review. Only selected configured `push-only` or `push-only-delete`
file units and directory children lose existing Bases. Children come from the
target's stored records and their configured Path Rules, not from discovery, so
excluded and absent children are maintained alike. Eligible and unrelated units
remain untouched. Cleanup does not create missing storage.

Dry-run and ordinary engine planning remain read-only. Cleanup is separate from
successful eligible publication and direct-agreement acknowledgment, which use
the shared [Base lifecycle](sync.md#acknowledgment-and-completion).
