# dotman Code Structure

This document owns contributor module orientation. [Sync lifecycle](sync.md)
owns behavior, [CLI](cli.md) owns the external command contract, and
[repository configuration](repository.md) owns schema and provider rules.

It is guidance, not a promise that every internal module name or boundary is permanent.

## Stable facades

- `src/dotman/cli.py` remains the main CLI entrypoint.
- `src/dotman/engine.py` remains the main engine-facing public facade.
- When practical, new internal work should preserve those public import surfaces instead of pushing callers toward internal modules.

## CLI structure

`src/dotman/cli.py` should stay thin and mostly coordinate:

- parser construction
- command-runner composition and selection
- configuration and engine construction
- top-level error handling

Focused CLI responsibilities live in dedicated modules:

- `cli_parser.py` — argparse construction
- `cli_emit.py` — text/JSON output formatting; text from outside dotman (a live path,
  an expanded repo root, an OS message) goes through `printable.py` where it enters output, so it prints escaped;
  authored identity was validated at load and prints as written
- `standalone_commands.py` — configuration-independent rewrite, transform,
  elevation, capture, editor, and render workflows. Repo manifests call these
  once per render, capture, or probe, so `cli.py` dispatches them before
  importing the engine, and each branch imports only its own command
  (`tests/test_startup_imports.py` guards this)
- `template_commands.py` — Jinja render and patch-capture helpers behind the
  standalone `render` and `capture` commands
- `inspection_commands.py` — configuration-aware list, info, search, and doctor
  workflows
- `state_commands.py` — track, untrack, add, and edit workflows composed with
  command-specific resolution and editor interfaces
- `restore_commands.py` — snapshot restore review, preview, and execution workflow
- `sync_deck_command.py` — Sync, Pull and Push CLI authorization and final output
  over public session views and commands
- `sync_timeline.py` — live human execution timeline rendered from session step events
- `sync_deck.py` — Textual DataTable workset, scrollable frozen review and
  confirmation over public SyncSession commands; ordinary terminal prompts
  continue to use `prompt_toolkit`
- `cli_interaction.py` — info and snapshot target resolution, restore diff
  review, and focused runtime adapters used by command runners; all prompts
  go through `interaction.py`
- `cli_style.py` — labels, colors, and display helpers
- `interaction.py` — typed terminal choices, confirmations, and text input, with
  production and deterministic scripted adapters
- `track_resolution.py`, `untrack_resolution.py`, `add_resolution.py`, and
  `edit_resolution.py` — command-specific matching, ambiguity, profile, label,
  and confirmation policy

Restore execution presentation is event-driven. `operation_runner.py` owns the
restore mutation lifecycle and emits typed events. Human and JSON renderers in
`cli_emit.py` consume those events and final results without performing
privilege, snapshot, or restore work.

The root selects every command through the same declared runner map. Sync, Pull
and Push commands open typed engine sessions directly; there is no global
callback-dispatch record or sync-specific fallback path.

If new CLI behavior grows beyond a small helper, prefer adding or extending a focused module instead of rebuilding a large `cli.py` monolith.

## Engine structure

`src/dotman/engine.py` should stay a facade that wires together narrower modules.

Current responsibility split:

- `repository.py` — repository loading and profile/group/package composition
- `manifest.py` — manifest merge and schema helpers
- `package_resolution.py` — selector parsing, package dependency closure, and resolved package selection construction
- `tracking.py` — persisted tracked-package state flows through `TrackedStateContext`
- `tracked_packages.py` — tracked package lookup and detail helpers
- `planning.py` — shared static planning primitives through `PlanningContext`: tracked selection resolution, target metadata and ownership candidates, and hook plans
- `planning_guards.py` — repo/package-instance/target/path-rule planning eligibility and guard diagnostics
- `collisions.py` — tracked-target winner resolution and conflict checks
- `printable.py` — the `str.isprintable()` rule in one place. `validate_printable` rejects authored identity at load
  (names, target `source` and `path`, path rule `pattern`s, `additional_sources` entries, a repo `path`), and that
  identity prints as written afterwards. It takes an optional `hint` for the refusal (`add` uses it). Four escapes
  show text from outside dotman, where it enters output. `printable_text` is injective (backslash doubled) for Sync
  identity stand-ins; `terminal_safe_line` escapes every unprintable character, tab and newline included, for outside
  text inside a one-line row or field (live paths, OS messages, legacy tracked rows); `terminal_safe_text` keeps
  newline and tab for outside messages that span lines (error details, Sync diagnostics). `terminal_safe_content`
  escapes control characters except tab, plus U+2028/U+2029 and bidi controls, for file content lines in Sync Deck review diffs
  and conflict blocks, where icons and emoji must stay readable. It also holds the
  reserved characters of selector and profile IDs, which package, group, profile and repo names share
- `sync_directory.py` — symmetric control-aware census and identity-derived child metadata; no aggregate payload or publication
- `projection.py` — shared Render, Capture and comparison providers over real endpoints or staged Proposals through `ProjectionContext`
- `sync_scope.py` — static tracked scope resolution, ownership/collision/reserved-path validation, and canonical file/child identity keys
- `sync_reconciliation.py` — typed three-way repository reconciliation from frozen Base, repository, and Capture evidence
- `text_merge.py` — shared `git merge-file` three-way text merge used by Reconciliation and patch Capture
- `sync_path_policy.py` — the shape-only `SyncPathError` shared by Observation and execution path checks
- `sync_commit.py` — Commit Work git boundary: work-tree probe, commit message
  rendering and pathspec commits of written sources
- `sync_session.py` — shared Proposal workset, immutable views, semantic commands, transactional Approval and Sync convergence orchestration
- `pull_session.py` — fixed live-to-repository Observation, opt-out Proposal/Additional Approval and repository-only completion over the shared workset
- `push_session.py` — fixed repository-to-live Observation, opt-out Use repository Approval and inherited Live Publication over the shared workset
- `execution.py` — the shared command-hook execution boundary, plus directory file-mode sync
- `sync_editor.py` — isolated configured/default Editor invocation and permitted source staging
- `editor.py` — the one editor lookup chain shared by the default Editor, `edit`, `add`, and `doctor`
- `sync_observation.py` — file endpoint evidence, policy comparisons, frozen Guards/Base facts and opening-time Base lifecycle
- `sync_auxiliary.py` — immutable Probe/hook rows, one-shot Probe activity and Guard-admitted directional hook retention
- `operation_lock.py` — manager-wide non-blocking real-operation ownership shared by sessions and Push/Pull command workflows
- `sync_base_lifecycle.py` — configured-policy Base eligibility, applicability inspection, and per-unit checkpoint acknowledgment decisions
- `sync_base_inspection.py` — metadata-only list/info, exact manager-locked reset, and aggregate doctor diagnostics using shared static resolution and Base applicability
- `sync_base_store.py` — secure per-repository file storage with self-contained Sync Base records and atomic per-unit replacement

Interactive Sync materialization uses one dedicated thread in `sync_deck.py`.
Its awaitable dispatch copies ContextVars, rejects competing deck input while busy,
and drains the actual thread before session abort or operation-lock release.
Observation-time Base stores are already closed; only frozen evidence and static
metadata cross into materialization. Apply and Publication remain synchronous.
`command_runtime.py` owns operation-scoped cancellation and child process handles.
`command_operation()` establishes the CLI operation; nested commands share its
`CommandOperation`. A standalone runtime command establishes its own scope when
none is active. Sync retains the operation identity from opening and reactivates it
for each dispatch, including materialization and final execution/cleanup. Its
thread-safe cancellation latch never resets: copied contexts and late callbacks
still refer to the cancelled operation, while subsequent operations on the same
engine or default/shared runtime get independent latches. ContextVar activation
is lexical, so a session can dispatch from a copied context without resetting a
token created in another context. `check_cancelled()` raises typed
`InterruptedError`. Cancellation never uses Textual thread-worker cancellation as
evidence that provider cleanup finished.

Explicit Proposal editing shares the same admission lane. TTY providers run while
Textual is suspended; pipe providers leave the deck visible. Each Editor attempt
has a separate cancellation scope: cancelling an attempt drains its subprocess
and discards only that transaction, without poisoning the enclosing SyncSession.
Session abort still cancels active Editor work. Canonical Additional Source rows retain staged changes independently from
Proposal generations, with reverse references and path-local standing Approval.
Only approved candidate bytes enter dependent projection inputs; unapproved
paths use frozen preimages. Approval changes eagerly rematerialize approved
references and invalidate unapproved previews for lazy regeneration. Batch
commands assign final selection states before materialization. Repository Apply
consumes each approved Additional change once before exclusive Primary changes;
its result is independent of Proposal completion.

The Base foundation exposes explicit boundaries rather than running a session.
`BaseUnit` carries successfully resolved selected configuration, never a
Guard-narrowed policy. Checkpoint payloads come from frozen repository-space
outcomes and require no Git status, checkout, commit, or ancestry operations.
`BaseInputs` accepts effective projection strings, named Path Rule identities,
profile context (including type-preserving frozen JSON variable inputs), and
symlink modes; it deliberately cannot accept policy, Guards,
Pull Views, chmod, or live referent paths.

Base inspection is metadata-only and needs no live access or projections.
Unavailable results omit stored payload metadata. Operation adapters report
unreadable Bases as warnings and use the existing no-Base resolution rules,
without allowing explicit Merge or weakening storage safety. Inspection commands
continue to report store failures as errors.

The lifecycle distinguishes pre-Guard policy maintenance, direct Observation,
and completion of approved unit effects. Publication supplies checkpoint evidence
without a second Render. Repository-only materialization freezes forward
qualification for the final candidate and approved inputs, reusing existing
proof where available. Qualification never changes the reviewed effect set.
Acknowledgment follows required effects at the earliest ordered completion
boundary; a pre-commit storage failure warns while preserving successful
completion and the previous record. A typed post-rename durability error instead
reports acknowledgment plus a warning, since the new record is already visible.
Storage owns atomic replacement, not operation orchestration. Coherent inventory
separates valid records from aggregate corruption so one damaged record cannot
block healthy inspection.

The Sync Observation adapter expands selected directory scopes into canonical
children after target Guards. It reuses `IgnoreMatcher`, Path Rule composition,
and directional Path Rule Guard evaluation; it does not reuse aggregate
one-sided directory plans. The census retains local diagnostics and combined
controls before narrowing to selected identities. Any ancestor discovery failure
propagates to existing and explicitly selected descendants before payload reads.
Root metadata remains only a hook/discovery scope. Expanded children enter the
shared Proposal and Base adapters as `Missing` or `DirectoryChildPresent` with
bytes and executable state. Capture and Render preserve that state; three-way
Reconciliation merges bytes and executable independently. Exact chmod is applied
only while freezing the live outcome.

Session/operation adapters own the manager lock, exclusions, actual Observation,
final effect execution, and invoking these boundaries in order. Base inspection
CLI behavior is separate from these foundation seams. Aggregate
directory discovery and orphan reclamation are not responsibilities of the
per-unit lifecycle.

The engine composes those immutable contexts once. Internal modules receive configuration, repositories, tracked state, and command execution directly; they do not receive `DotmanEngine` or call back through private facade methods.

Sync, Pull and Push share one planning path: `resolve_sync_scope` resolves the
tracked scope statically, then the session opens Observation over that frozen
workset. Publication builds package and target plans only for approved work,
keeping repo/package/target hook ordering explicit instead of hiding it in ad
hoc sorting.

## Command runtime

`src/dotman/command_runtime.py` is the only process-creation boundary.

- Callers submit `CommandRequest` values containing a shell command or argument vector, environment overlay, working directory, pipe/TTY mode, streaming policy, and elevation mode.
- `ProductionCommandRuntime` constructs the ambient environment, applies elevation, launches the process, owns terminal or pipe behavior, streams and captures output, and normalizes interruption.
- Callers interpret `CommandResult.exit_code` in their own domain. The runtime does not decide whether a status means guard exclusion, probe absence, diff presence, or execution failure.
- `MemoryCommandRuntime` supplies deterministic queued outcomes and records requests for behavior tests.

Planning passes the engine's runtime explicitly while evaluating guards, probes, and projections, and binds that same runtime for shared privileged file helpers. Execution binds one runtime for the complete session. Editor, review, and privileged-helper commands use the same active runtime.

Each intercept command owns its elevation broker for the complete runtime
call. The `sudo` shim asks the broker only when it has a controlling terminal;
without one, sudo keys its ticket to the parent process, so the shim runs real
`sudo` without `-n` and lets it authenticate itself (ADR 0007). The broker
copies that command's runtime and cancellation context into its serving threads; a later Editor attempt never reuses a cancelled attempt's broker.
Before returning terminal control, the runtime stops broker admission, unblocks
pending request reads, and drains authentication handlers and their terminal
restoration. Interrupted authentication is reported as interruption (130), not
ordinary authentication failure; requesters that disconnect during cancellation
need no reply. The broker remains threaded: worker-owned commands and TTY Editors
still require context propagation and main-thread-only signal installation.

## Operation runner

`src/dotman/operation_runner.py` is the restore mutation boundary.

- Sync, Pull and Push use their process-local Proposal sessions and shared publication stages rather than this runner.
- Restore executes visible actions in order, stops at the first failure, records successful restore metadata, and emits typed action events/results.
- Human and JSON output policy is selected at CLI composition. JSON consumes no progress events and emits one final result document.

Planning is package-centric:

- selector queries and tracked package entries resolve into `ResolvedPackageSelection`
- Sync/Pull/Push sessions resolve them through `resolve_sync_scope`; publication and snapshot flows consume the resulting package plans
- tracked-package persistence remains a separate storage concern from runtime package planning

If a new engine feature clearly belongs to one of those areas, put it there first and keep `engine.py` as the public facade.

## Contribution rule of thumb

Before adding more logic to `cli.py` or `engine.py`, ask:

- Is this public facade glue?
- Or is it a focused responsibility that belongs in a dedicated module?

Prefer the dedicated module unless there is a strong reason not to.

## Manual Sync demo scene

`uv run python -m tests.demo_scene` rebuilds a throwaway scene under the system temp dir
(or `--dir PATH`) and opens `dotman sync` in it for hands-on review of the Command Deck.
HOME and the XDG directories point into the scene. Each case in `tests/demo_scene.py`
declares the text its Proposal Review must show, and `tests/cli/test_demo_scene.py` keeps
those claims true. Add a case there when a review behavior is worth trying by hand.

## Session orchestration boundary

Static resolution belongs to `sync_scope.py`: tracked selectors, profile and
dependency expansion, ownership, and collisions are settled before a session
opens. `SyncSession` and `PullSession` retain distinct orchestration over shared
frozen observation/projection, transactional sources, review, repository writes,
path safety, and Base storage mechanics. Do not move workflow semantics into a
pervasive `sync | pull` branch in those shared modules.

`sync_deck_command.py` owns CLI consent, no-confirm default selection, output,
and exit mapping. The deck consumes immutable public views and semantic commands;
it owns focus, keybindings, menus and presentation, never private execution plans.
JSON is an output choice, not authorization. Providers and hooks use the shared
Command Runtime so no-confirm execution cannot accidentally fall into terminal
interaction and JSON stdout contains only the final result.

## SyncSession clients

Resolve selectors with `engine.resolve_sync_scope(...)`, then call
`engine.open_sync_session(scope, preview=..., run_noop=..., event_sink=...)`. Opening returns
a `SyncSession` or typed `SessionOpenFailed`. File targets, directory children and
auxiliary scopes are supported. File and child drift materializes policy-allowed
Proposals and supports transactional editing; Probe/hook work is directly included
without an Observation or Proposal.

Read `session.view` rather than private plans. `SetIncluded`, `SetApproval`,
`PrepareProposalReview`, `Preview`, `Execute` and `Abort` carry the view's session
ID and revision. Dispatch returns
`CommandAccepted(view, result)` or mutation-free `CommandRejected(view, reason)`.
Row-local allowed commands distinguish drift, non-approvable diagnostics and
auxiliary rows, which expose only `SetIncluded`.
Convenience `execute()` and `abort()` act on the current view; adapters holding
cached views should dispatch explicit revision-bearing commands.
Accepted commands yield new immutable snapshots; old views remain valid evidence.
Terminal views allow no further commands.

`SessionOpened`, `SessionChanged` and `SessionFinished` are immutable lifecycle
events for recording or presentation sinks. Callback programming failures
escape; failed opening still releases owned resources. Context-manager exit
aborts an unfinished session. Review and Approval materialize immutable Proposals
from frozen inputs. Preview reports approved effects without mutation; Execute
applies those same repository and live effects without re-observation or projection. Results keep
direct agreement, approved convergence, pending/excluded drift and failures
distinct. File inclusion never substitutes for Proposal Approval; auxiliary
inclusion authorizes only retained directional hooks.

`sync_capture.py` materializes Use live from the live endpoint and frozen comparison
evidence through shared projection and patch mechanics. A Capture-backed comparison
is reused rather than run again. `sync_repository_apply.py` applies independently approved Additional Source
Changes before exclusive Proposal-owned Primary Source outcomes through pull hooks without accessing live state or
snapshots. Units without required live effects complete at their ordered position
before the enclosing post-hook; the session uses
the shared Base lifecycle with frozen qualification evidence to acknowledge
eligible repository outcomes. A failed required repository stage prevents all
Live Publication; a checkpoint-only failure does not.

`sync_publication.py` freezes hook and target execution metadata at opening and
publishes approved file effects through the existing file-access, snapshot and
hook mechanics. Both stages use its shared ordered stage-step construction:
repository order, package plan order, target plan order and nested directional
hooks. Child execution paths are frozen separately from their enclosing target,
so hooks run once per target while each child retains its own effects and result.
That frozen sequence drives execution and explicit unattempted-tail
reporting; it is not reconstructed from filesystem outcomes. Cancellation checks
precede hooks, effects and completion. Publication acknowledgment warnings remain
distinct from successful content, chmod, and unit completion.
The session preserves repository partial success as not-converged when later
publication is skipped. Public results retain immutable semantic step outcomes and
operation-level diagnostics separately from unit completion, so an enclosing
post-hook failure does not erase convergence. Each scoped outcome carries a
canonical `scope_identity`, preserving package instances and target names
without exposing execution plans.

The operation lock is a POSIX advisory `flock(LOCK_EX | LOCK_NB)` on the
owner-only `$XDG_STATE_HOME/dotman/operation.lock` file. Never unlink this file on
release: another process may already hold its inode. The manager directory must
be current-user-owned and not writable by other users; Base storage additionally
requires its documented exact private-directory modes. Lock acquisition rejects
symlink, nonregular, hard-linked, and wrong-owner lock files. For a verified
current-user-owned regular single-link file, acquisition automatically sets
its mode to `0600` through the open descriptor and revalidates it. This needs
no root access; failed permission repair aborts acquisition without replacing
or unlinking the lock.
Low-level planning and execution helpers do not acquire a second nested lock;
their direct callers must own an operation lifetime explicitly.
Restore and unrelated state commands do not participate in this Push/Pull/Sync
operation lock. It coordinates cooperating operations, not external filesystem
writers or hostile same-user code.

Projection providers read the Primary and live endpoints at their real paths,
so commands can depend on neighbouring files; the session freezes their output. Only a repository Proposal, which
has no file on disk, and a protected input are staged as private copies; custom
commands retain configured dependency access and cwd.
Session input metadata carries the invoking workflow into every provider,
including Probe and transactional Editor. Directional Guard/hook metadata stays
separate so provider identity cannot change which directional family executes.
Jinja renders frozen Primary bytes through the shared template renderer.
The public view exposes no staging paths, mutable configuration dictionaries,
store handles or execution steps.

References: [Python file locking](https://docs.python.org/3/library/fcntl.html#fcntl.flock)
and [descriptor-relative file access](https://docs.python.org/3/library/os.html#os.open).

`sync_deck_command.py` adapts Sync, Pull and Push to the shared Command Deck.
The engine opens Pull through `open_pull_session` and Push through
`open_push_session`.

## Versioning and releases

Git tags are the only version source. `hatch-vcs` derives the package version
from the nearest tag at build time, and `dotman --version` reports it. A release is
a pushed tag such as `0.10.1`; no file records the version. `uv sync` rebuilds the
editable install when the commit or tags change. A build without `.git` or tags
reports `0.0.0`.
