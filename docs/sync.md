# Sync lifecycle

This document owns the human convergence lifecycle. See [CLI](cli.md#sync) for
commands, controls, output and exits, [repository configuration](repository.md)
for syntax and inheritance, and [contributor architecture](code-structure.md)
for module orientation.

A file target is one **Sync Unit**. Each regular-file child of a directory target
is an independent Sync Unit; a directory root has no aggregate Base.
After tracked ownership selects its winners, their Primary Sources must be
exclusive across all Sync Policies, including push-only targets whose Proposal
Editors can write repository sources. Duplicate or unexcluded nested source
claims are configuration errors before Observation, even for a narrower scope.
Scope resolution plans the tracked graph once per direction; Observation reuses
that planning, so it sees exactly the paths ownership and collisions validated.

## Frozen Observation

A SyncSession observes its resolved file targets, directory children and auxiliary
work once. Guards run in repository → package → target order across both
directional families before endpoint reads, directory census, `ignore.command`,
and comparison. A directory target denied in every direction is not scanned.
After directory census and child policy resolution, active named Path Rule Guards
run by priority then name, once per rule and directional family.
Exit 0 retains capability, 100 removes that direction within the Guard's scope,
and other non-zero exits abort planning. Review and execution never rerun Guards.
Each removed direction appears once per Guard scope as a dimmed, unselectable
**Guard skipped** row, e.g. `main (guard_push)`, while the scope's units stay on
their remaining route.
Configured Sync Policy remains the upper bound; narrowing never changes Base
eligibility (configured `pull-only` or `both`). Observation retains the resolved
scope order, effective projections, typed endpoint bytes and live mode/link
evidence. Each unit also retains applicable
Base evidence independently of Git history.

| Effective policy | Direct comparison |
| --- | --- |
| `push-only` | Repository-derived live bytes and configured file mode against live |
| `pull-only` | Configured repository and live comparison projections |
| `both` | Configured repository and live comparison projections, plus configured exact live file mode |
| `push-only-delete` | Desired `Missing` against live presence |

Exact file mode participates only while push capability survives. Equal content
with a different configured live mode is drift requiring Approval, not direct
agreement; pull-only capability never enforces live chmod.

`Missing` differs from a present empty file. Missing endpoints remain typed
absence rather than passing invented empty bytes through a projection. Under
push-only, a Missing repository produces a Missing live outcome; under
push-only-delete, live deletion retains the repository source and does not
invoke Render, Capture, or comparison commands. Directories at file endpoints,
FIFOs, sockets, and other unsupported nodes are Observation failures, not absence. Each unit is exactly **Directly
InSync**, **Drifted**, or **Observation Failed**. Direct agreement has no drift
row; drift has one stable canonical row, initially unapproved.
Observation failures stay visible and non-approvable. Base read or acknowledgment
failures are warnings, not failed Observations. Endpoint reads and comparison
commands for different units run concurrently, up to eight at a time (fewer on machines with fewer cores). Base reads
and acknowledgments stay serial, and results keep target order. An unavailable Base uses the
normal no-Base resolution rules; explicit Merge remains blocked without a usable Base.
A Guard-removed route is a visible diagnostic, not permission to use the opposite
direction. A unit-local failure does not discard unrelated evidence.

Base availability describes frozen pre-acknowledgment evidence; a separate
acknowledgment flag records successful opening-time maintenance.

External changes never refresh an open session's evidence. Start another session
to see new filesystem, configuration or Git state.

The session freezes what it produces, not what providers read. Render, Capture
and comparison commands read endpoints at their real paths through
`DOTMAN_REPO_PATH` / `DOTMAN_LIVE_PATH`, so a provider may depend on files beside
its endpoint (for example, a lock captured from a whole config directory). Each
projection output is then retained for the rest of the session. A repository
Proposal has no endpoint on disk, so projections of a Proposal read a private
staged file instead. Frozen endpoint bytes still decide raw views and `Missing`,
and Merge uses frozen repository and Base evidence with the Capture output.

## Directory census and child capability

A directory target is a discovery scope, never an aggregate Sync Unit. One
census inspects both trees, combines skip-marker subtrees found on either side,
and applies repository-source Git ignore controls and repository/package/target
exclusions, including a target's computed `ignore.command` lines, symmetrically. Live `.gitignore` content does not define policy.
Control files, directory nodes and empty directories are not payloads. Ignored
paths receive no Observation or Base maintenance. Git ignore matching preserves
[excluded-parent semantics](https://git-scm.com/docs/gitignore): a nested negation
cannot re-include content beneath an excluded directory.

The surviving regular-file union has canonical identities
`repo:package.target/<relative/child>` (including package-instance forms).
Target and child scopes form a de-duplicated union; exact child selection does
not observe unrelated siblings. The census remains control-aware for partial
selection, and an ancestor discovery failure remains visible for affected
selected children rather than implying absence.

Each child resolves its own named Path Rules, configured policy, Render, Capture,
comparison, Editor and exact mode policy, even with a Missing endpoint. Exact
child scopes activate only their resolved configured directions at ancestor and
target Guards; unrelated child rules cannot add a direction. Full-target discovery
retains all potentially configured child directions. Path Rule Guards only narrow
the child's configured capabilities. Executable state matters in child comparison;
other repository permission bits do not. Exact child chmod also participates when
push capability survives (`push-only` or `both`), but is inactive under `pull-only`.
Under `both`, exact chmod does not replace executable-state comparison. Target
chmod belongs to the root.

Repository symlinks and unsupported nodes yield typed path-specific Observation
failures unless excluded. Repository directory links are never descended or used
as ancestors for child endpoint reads. Live directory links fail locally in
`fail` mode; `follow` mode retains lexical child identities and detects loops.
Rejected traversal or unknown controls/discovery at an ancestor block every
affected descendant before either payload endpoint is read, including children
already found on the opposite side and exact child selections. Failures do not
discard unrelated child evidence.

Each child supports the same policy-constrained Resolution Intents, lazy Proposal,
standing opt-in Approval, transactional Editor and independent completion as a
file target. Its repository representation is `Missing` or
`Present(bytes, executable)`. Merge reconciles bytes and executable state
independently: agreement wins; otherwise the side changed from the Base wins.
A conflict remains blocked without changing intent.

Exact Path Rule chmod derives only the approved live outcome with surviving push
capability. Equal bytes and `Use live` can therefore produce chmod-only
Publication Effects. It never changes the repository outcome or the repository
executable state stored in the checkpoint. Other permission bits remain outside Merge.

Each child path is its exclusive Primary Source. Transform and Editor dependencies
use canonical independently approved Additional Sources, including shared
dependencies across children. There is no directory source bundle. A rename is a
deletion and an addition with separate Approval and Base identities.

Children use the same checkpoint lifecycle as file targets. Direct eligible agreement can
acknowledge immediately in a real session; drifted eligible children acknowledge
independently after their own required effects. Ineligible children complete
without a Base. Both stages keep one enclosing target hook scope and report each
child's exact identity. Structural parent directories are created only for selected child writes and
pruned after child deletion, stopping at the target root. Empty targets are never
created merely because they are selected. Existing root mode drift is separate,
selectable **Directory Root Work**: it runs within push hooks before every child
Publication Effect and never produces a Proposal, Converged result, or Base.
A child creating a missing live root applies the configured target mode as its
own prerequisite.

A file/directory transition requires explicit Approval of every managed ancestor
or descendant deletion that blocks a writer, including siblings omitted by an
exact-child selector. Dotman never auto-selects blockers or clears the writer.
Confirmation is unavailable until this structural closure holds; no-confirm
execution rejects it before mutation. An ignored, control, or otherwise unmanaged
blocker produces a typed structural conflict instead. Ready child work uses
normalized relative-path ordering, with prerequisite deletions before dependent
writers. A completed deletion remains Converged if a later writer fails; any
checkpoint already saved for that deletion remains intact.

Obsolete child Bases are considered only from a later complete unrestricted
target census. Reclamation occurs after successful real execution, not while
previewing or aborting review, and does not reclaim newly acknowledged deletions
in that session. Partial selection, ignores, marker restrictions, Guard narrowing,
Path Rule Guards, interactive exclusions, or failed discovery retain records.
A restricted census is deliberately not used as absence proof.

## Auxiliary work

An active Probe becomes **Probe Work** only when at least one configured
capability survives Guards. Its command runs once: exit 0 keeps the row, 100
omits inactive work, and any other non-zero exit aborts planning. Probes run
concurrently, like comparison projections during Observation; when several
fail, the earliest in target order is reported. A Probe cannot
use `push-only-delete`; it has no live endpoint to delete.

Probe Work starts unselected and has no file Observation, payload, comparison,
Resolution Intent, Editor, Proposal, Approval, Base or Converged result. Selection
activates normal hooks only in its surviving families: pull hooks during
Repository Apply, then push hooks during Live Publication.

Independently noop-eligible hooks remain selectable **Hook Work**, identified
only by canonical repository, package-instance or target scope plus
`(pull-hooks)` or `(push-hooks)`. The operation's noop option makes both surviving families noop-eligible,
in addition to manifest-level eligibility. A child Guard does
not remove an ancestor's independently retained noop hooks. Selection coalesces
nested hooks with Probe and file work so each hook runs once. Auxiliary-only
execution writes no payload and creates no snapshot or Base acknowledgment.
Preview freezes Guard and Probe results but executes no hooks.

## Resolution Intents and the Command Deck

Drifted push-only and push-only-delete files offer **Use repository**; pull-only files offer only
**Use live**. Both-policy files offer **Use repository**, **Use live**, and
**Merge** when a usable Sync Base exists. Their default is Merge with a usable
Base; otherwise the default keeps whichever side still has the file when exactly one
side is Missing, else Use repository. Such a guessed default shows as a warning-colored
Resolution in the workset, review, and command output (`resolution_guessed` in JSON);
so it reads without color, the workset marks it with a trailing `?` (`Use repository?`)
and review and command output annotate it `(guessed)`. The
missing Base itself shows on the Sync Base line. Choosing a Resolution or editing the outcome ends the guess for good;
Approval does not. Without a Base, nothing shows
which side changed beyond presence. The repository is the declared configuration, and live writes
are snapshotted while repository writes are not, so the fallback stays Use repository
when both sides are present or both are Missing. No-confirm Sync does not guess and skips
this drift (see [Result log](#result-log)). Proposal Approval starts off;
opening a review does not approve it. Review or Approval materializes the
Proposal from frozen Observation, retaining the repository representation,
policy-derived live outcome, exclusive Primary Source Change, and exact
Publication Effects. Execution consumes that outcome rather than rendering or
observing again. Inclusion is separate from Approval.

The persistent Textual Command Deck shows drift, auxiliary work and diagnostics,
not directly agreeing units. Its aligned table uses canonical target identities
and a **Selection** column. Selection authorizes Proposals and canonical Additional Source Changes through
independent Approval and directly includes auxiliary work. Unsupported resolution capability is labeled separately
from Observation and Proposal failures. A ringed detail panel below the table
describes the focused row: its full canonical identity, diagnostics, and for units
the full live and repository paths, Observation state, Sync Base status and any
differing configured policy. A unit, Probe or hook row that only a package input's
dependency closure brought into scope shows `Included via:` with those package inputs,
and its Target label is dimmed in the table. Wrapped values keep a hanging indent under their
value column, and **Tab** focuses the panel for keyboard scrolling. Directory child rows use the same opt-in Approval as file targets. The deck and
output adapter also recognize semantic **Directory Root Work** as direct Auxiliary
inclusion, without Proposal or Approval. Root work uses the same selection styling
and canonical target identity as other auxiliary work.
Focused review shows
repository-effect and publication previews. When a drifted unit writes nothing,
for example because Capture returns the unchanged repository representation, it
adds a Drift section with the Pull View diff that explains the drifted state. Returning preserves
the workset.
A both-policy outcome whose live bytes already match can still differ from Capture when
Capture reads state that push hooks apply from the live file, such as a settings dump.
When the Pull Views drift and Capture's content differs from the repository outcome, the
Proposal gets a `reapply` Publication Effect: it activates the unit's push hooks without
rewriting or snapshotting the file. Review lists it as `reapply <path> (push-hooks)`, notes the
unchanged live file, and keeps the Drift section's Pull View diff. A unit whose activation runs
no push hook gets no reapply, since nothing could change what Capture reads; it stays a no-write
Proposal.
Changing Resolution Intent preserves Approval, discards the prior Proposal, and
rematerializes approved work. Approval alone, single or batch, never discards a
Proposal; selection materializes only work that lacks one. Push and Pull fix the
Resolution Intent to Use repository and Use live. Review exposes Capture and Reconciliation evidence
separately from repository and live effects. The [CLI reference](cli.md#sync)
defines keyboard and mouse interactions.
Execution confirmation is available only when every approved participating Proposal
has a valid materialization. It summarizes approved units, independently approved
Additional writes, auxiliary inclusion and live effects. Cancelling confirmation
preserves Approval, inclusion, focus and review state. Confirming closes the deck
and executes exactly those already-materialized outcomes; it does not trigger
Capture, Render, re-observation or another planning pass.

Materialization is serialized: review and execution never race with changing
Approval or provider work. Interruption drains owned provider work before the
session discards scratch state and releases its lock. It does not roll back
completed effects or control deliberately detached descendants.

For pull-only files, review or Approval lazily Captures live into the repository
outcome. Capture runs at most once per unit unless retried; retry reads the live
endpoint again, so fixing live state can recover a failed Capture. Review shows the
Primary Source Change (write, deletion, or none) authorized by Proposal Approval;
live remains unchanged. Confirmation counts repository changes separately from
live effects. Structured output keeps repository changes separate from Publication Effects.

Real execution applies approved repository outcomes through pull hooks before
publishing approved live outcomes through push hooks. Pull-only work creates no
live snapshot and cannot mutate live. A pull-only unit becomes **Converged**
after its required repository effect succeeds. A qualifying final candidate may
also establish a Base; acknowledgment is independent of completion. Even a
drifted Capture result needing no repository write requires Approval and a
completion boundary; it is not **Directly InSync**. A drifted push-only unit
becomes **Converged** when its required effects succeed, without creating a Base. An approved drifted Proposal with no writes still needs
this completion boundary; it is not **Directly InSync**. Failed publication
does not claim convergence or roll back earlier successful units.
No-write completion runs no hooks and creates no snapshot. An enclosing
post-hook failure fails the operation without undoing a unit's convergence.

### Transactional Proposal Editor

Open the Proposal Editor from the workset or Proposal Review to deliberately
edit a drifted file unit with a surviving route. One-sided policies permit this explicit
repository edit, including deletion-only units; they still prohibit automatic
flow in the forbidden direction. Observation failures and Guard-blocked units
have no Editor. Dotman never launches an Editor automatically or in no-confirm
Sync.

The Editor starts from the selected Resolution's repository outcome, materializing an
unreviewed Use live or Merge first; a failure there stops that attempt. After a merge
conflict it starts from Git's `zdiff3` merge output, conflict blocks included, so
the user resolves them in place. A saved edit that still contains a complete
conflict block with Dotman's labels (`repository`, `Sync Base`, `Capture`) stays a
blocked conflict without Approval, and the Editor never reopens on its own; the saved
text becomes the evidence the next Editor run starts from. Blocks already present in
the frozen repository source or Capture, and markers with other labels, are content. If no Proposal exists otherwise, including after
a Capture failure, the next attempt starts from frozen repository sources without requiring Capture. The configured/default Editor
receives isolated Primary and permitted Additional Source copies and read-only
review evidence, not writable tracked paths. Cancelling, or any failure before a save such as a failed Editor
command or workspace, discards the attempted
transaction and preserves the previous Proposal and Approval. Saving produces
an **Edited** generation, selects Edited as the Resolution Intent, and derives policy-appropriate effects;
the saved edit stays selectable after switching to another intent, and the next save replaces it; tracked sources
and live endpoints remain unchanged until execution. Deliberate Primary writes
activate Repository Apply pull hooks even when automatic live-to-repository flow
is forbidden by policy or Guards. Guards are not rerun; no-write editing does not
activate these hooks. Quitting without changes, with the Primary identical to its start and no new
Additional Source changes, is not an edit: it keeps the Resolution Intent, Proposal and
Approval and adds no Edited choice. A byte-identical Primary save with Additional changes retains
valid projection results rather than repeating provider work.

### Additional Source Approval

Each changed Additional Source has one canonical repository-relative row, shared
by every referencing Proposal. Source Change Review shows the frozen-preimage
diff and reverse references. Proposal Review lists those references but does not
duplicate their Approval control. Primary Sources remain exclusive to their
own Sync Units and cannot also be Additional Sources.

Additional Approval independently authorizes the repository write, even with no
approved referencing Proposal. New paths start unapproved. Unapproved candidate
bytes remain available to review and later Editor attempts, but dependent
Proposals use the frozen repository preimage instead. Approval and unapproval
rematerialize approved references immediately; unapproved references discard
stale previews and rematerialize only on demand. Editing an unapproved candidate
alone does not invalidate unchanged authorized inputs or repeat their providers.
Batch unselection retains unresolved materialization diagnostics; only successful
rematerialization clears them. It does not turn failed work into healthy work.
Successful rematerialization
preserves Proposal Approval; failure clears only the affected Proposal.

Returning a path to its frozen preimage removes its changed row, not its
session-local Approval. Recreating the change restores that Approval. Batch
selection establishes all final Proposal and Additional states before any
dependent materialization; batch unselection discards invalid previews without
eagerly regenerating them.

Repository Apply writes approved Additional changes once, in stable normalized
path order before Primary changes. Additional-only work activates no directional
hooks. Additional results are reported independently. A failed Additional write
stops execution before Primary writes and publication; no separate per-Proposal
acknowledgment dependency is needed.

Successful rematerialization preserves standing Approval. Failed materialization
clears only the affected Approval and leaves a typed diagnostic with local retry.
Conflict, Capture failure, command failure and Editor cancellation remain
distinct; none silently chooses another Resolution Intent. Materialization and Editor attempts can be retried explicitly. Cancelling an
Editor attempt preserves the session. Terminal Editors temporarily own the terminal and return to the same focused workset or review when finished.

### Frozen execution and partial failure

Execution traverses repositories in configured order, dependency-first package
plans, then stable target-plan order. The complete operation runs Repository
Apply before any Live Publication. Approved Additional Source Changes are written
once in repository order and normalized-path order, before Primary Source work;
Additional-only work owns no package/target hook scope.

Primary changes use nested pull hooks, including deliberate Editor changes under
one-sided policy. Publication uses nested push hooks and consumes the exact
approved effects without reading sources again, Capture, Render, Observation or
replanning. Selected Probe and retained noop-hook work activate only their
surviving directional families.

Every approved no-write Proposal has its normal Repository Apply completion
position. A no-write Proposal whose completion could not record a new Sync Base
(ineligible, unqualified, or Base already current) is a **No-op Proposal**: it
offers no Approval and reports `noop`. Repository-only units complete there;
units requiring live effects wait for their own publication. Qualifying eligible
completion saves the unit's Base before target post-hooks. Post-hook success is not a
condition of convergence.

The first failed required write, safety check, hook or interruption stops
later work. Cancellation is checked at each effect, hook and completion boundary.
Atomic replacement protects each file, not the whole operation: earlier writes,
Converged units and committed Bases remain in place. A write followed by failed
chmod retains the bytes but does not acknowledge or converge that unit.
Checkpoint-save failures before atomic replacement warn, retain the previous
Base, and allow later work. A flush failure after replacement warns that the
new Base is already advanced but its crash durability is uncertain. Neither
warning undoes effects or changes a completed unit into a failed one.

Results retain the exact failed step and ordered successful and unattempted
steps, including required completion boundaries that were never reached. A unit
with no attempted work is `skipped`; one whose repository work succeeded but
whose live work was not attempted is `not-converged`. A failed unit is
`execution-failed` (or `interrupted`), while earlier completed units remain
`converged`. Additional Source results remain independent.

One normal live-only snapshot is created after successful Repository Apply,
after push pre-hooks and safety checks, immediately before the first live
mutation. It covers only the frozen publication set and survives partial live
failure. Preview, repository-only, no-write and hook-only operations create no
snapshot. Restore never restores repository sources.

### Commit Work

Commit Work commits the repository sources a Sync or Pull wrote, so a run can
leave a clean git history without a separate step. Push offers none: it writes
repository sources only through deliberate Editor changes.

- The session offers one option per dotman repo with drift that sits in a git
  work tree; the Deck's confirmation lists those the selection writes. Options
  start on in the Deck, which shows them before execution; a run that does not
  open the Deck commits only with `--commit`. Two dotman repos sharing one git work
  tree make two commits.
- Commits run after all other work, in configured repo order, and only when the
  run completed. A failed or interrupted run leaves every commit `skipped`
  (`skip_reason: earlier-failure`); preview never commits.
- Each commit covers only paths this run wrote: converged Primary Source Changes
  and applied Additional Source Changes. dotman stages them with
  `git add -A -- <paths>` and commits with `git commit -- <paths>`, so unrelated
  staged or unstaged work stays out. Files a pull hook writes are not included,
  and, as with `git add -A`, neither is an untracked source git ignores.
  A repo with nothing left to commit reports `no-changes`.
- The message comes from `[git] commit_message` ([config](config.md#git)). It
  names only the targets whose sources the commit records; the Deck's message
  is a prediction before the run, so it may count a source git later leaves out.
- A failed commit fails the run; an interrupted one aborts it. Earlier commits
  stay. Git hooks and signing run as configured in the repository.
- The timeline shows each commit as a `commit` step under its repo, described
  by the commit subject; the summary line adds `commits: N`.

### Result log

Human execution streams a step timeline while it runs, grouped by package (repo
hooks by repo) and numbered within each group:

```
:: Push
  main:app
    [1/2] pre_push    ./check.sh
      <live hook output>
      ok
    [2/2] write       ~/.config/app/unit
      ok
:: completed — approved: 1 · repos: 0 · live: 1 · in-sync: 0
```

Sessions report `StepStarted`/`StepFinished` through their event sink and stream
pipe-hook output only when opened with `stream_output`; completion checkpoints
stay invisible. A failed hook adds only its exit status, since its output was
already shown; other failures print their error. A failure outside the plan
(snapshot finalize) is reported without a number.

After the timeline, the log recaps only what the timeline could not show:
entries with unshown diagnostics (such as checkpoint warnings), Additional
Source Changes, and failed entries the timeline did not attribute. Work stopped
by an earlier failure is counted as `skipped: N` in the summary line rather than
listed, so the failure stays next to the summary. Stderr diagnostics already
shown are not repeated.

Guard skips are planning results, not execution steps, so they appear where work
is reviewed and are always dimmed: as unselectable rows in the Command Deck, or,
without a Deck (no-confirm), above the timeline. No-confirm Sync prints
no-Base skips in the same place, `[skipped] <identity> (no Base)` with a
first-review hint, undimmed because they need action. They are never folded
into the `skipped: N` count.

`--report` replaces the recap with the full entry log below: every entry with its
Resolution and effects, skipped work listed rather than counted, unselected
entries led by `[unselected]`, and guard and no-Base skips (then omitted above
the timeline).

Preview and pre-execution failures print the entry log instead: each
entry leads with its outcome (`ok`, `failed`, `interrupted`, `skipped`,
`would-apply` in preview, `pending` when execution never started) followed by
its Resolution and effects. Unselected entries are omitted unless they carry
diagnostics. JSON never includes progress.

Aborting the Deck prints only `:: aborted — no changes made`: nothing ran, and
the entries would only echo the dismissed rows as `pending`. `--report` and JSON
still carry every entry.

A failed Guard or Probe is summarized by the first non-empty line of its stderr,
else stdout. Command output is untrusted and may contain managed content, so
this line appears only in human output; JSON diagnostics and `stages` keep typed
evidence (action, scope, exit status). Guard skip reasons (exit 100) are the
exception: they are the Guard's declared explanation and appear in both.

### Both-policy reconciliation

Merge lazily reconciles the usable Base payload, frozen repository representation,
and frozen Capture result. Equal sides agree; when one side still equals the Base,
the other side wins. Otherwise present file contents use Git's three-way
`merge-file --zdiff3` through Command Runtime. When both sides added a file the
Base records as `Missing`, the merge uses an empty ancestor, as Git does for
add/add; a child's executable state then keeps the repository's on disagreement.
A conflict keeps that merge output
as evidence for Proposal Review and the Editor. Conflicts and provider failures remain
distinct typed, blocked, retryable diagnostics. They clear the affected Approval,
not its Resolution Intent; Dotman never selects another intent or opens an Editor
automatically. Successful Capture and Render results are reused for unchanged
frozen inputs.

Use repository never Captures. Use live and Merge derive live publication by
Rendering their repository outcome under policy, so Use live can require live
writes or mode changes as well as a Primary Source Change. Both stages consume
only approved frozen effects. An eligible unit acknowledges its final repository
outcome after its own last required effect, before later units or enclosing
post-hooks. Publication itself supplies the evidence: no intermediate checkpoint
after Repository Apply and no duplicate Render are needed. Acknowledgment warnings
do not undo convergence or earlier checkpoints; failed replacement preserves the
prior Base, while a post-replacement flush warning reports uncertain durability.

## Session lifetime

File-target and directory-child sessions support frozen Observation, push-only, pull-only and both-policy
Proposals, Approval, review, preview, Repository Apply and Live Publication.
Sessions also support auxiliary work and explicit Proposal editing.
Unapproved or excluded healthy work remains untouched. Abort does not undo Base
maintenance already committed while opening.
Deliberately unapproved supported drift remains `pending` without failing the
operation. Included unsupported drift makes the result `incomplete`; diagnostics
and execution failures remain failures. No-confirm Sync rejects a blocked or
unsupported workset before mutation.

### Symlink interpretation

Prompt file-symlink mode observes a regular referent. A write or mode-only
publication replaces the declared leaf link with the frozen regular-file
outcome, but requires explicit live-link replacement authorization first. Authorization and Proposal Approval are
separate; no-confirm execution rejects required authorization. Deletion unlinks
the declared link without replacing or deleting its referent. Pull-only work
never replaces a live link.

Follow file-symlink mode preserves the leaf link. A missing referent is typed
`Missing`; creation writes its then-current referent, and deletion removes
that referent while retaining the link. External referent parents are not pruned.

Directory-symlink fail mode rejects unexcluded links at or below the declared
directory root, including links introduced after Observation. Follow mode
traverses them under lexical child identities. Repository links are unsupported
and never payload inputs; unsafe shapes yield typed diagnostics.

Execution dereferences the current chain for each effect and checks shape,
declared endpoint confinement and selected policy without re-observing payloads,
rerendering, or comparing content. Retargeting alone does not invalidate frozen
work. Enabled snapshots still read live preimages before mutation. Link chains
and resolved referent paths are not identity inputs.

A real session owns the manager's non-blocking operation lock from opening until
execute or abort. Real Push and Pull command workflows take the same lock before
planning and retain it during review and execution. A conflicting operation
fails immediately. Use a session context manager or explicitly abort an
abandoned session; session state is neither persisted nor resumable.

Preview cannot execute and does not take or create the manager lock, write
managed repository/live/Git/state files, acknowledge or clean up Bases, run
hooks, or create snapshots. Cleaned-up private scratch for projections is
permitted. Configured projection providers remain
trusted side-effect-free stdout producers, not sandboxed arbitrary programs.
Comparison-owned Capture views may run during Observation. Pull-only Proposal
materialization reuses that frozen Capture result when the configured live
comparison already required it, rather than running Capture again. Otherwise,
Capture is delayed until review or Approval. Push-only Proposal materialization
needs no Capture or three-way reconciliation.

## Sync Bases

A Sync Base records the repository-space outcome established by a qualifying
synchronization. It is independent of Git commits and may contain working-tree,
Capture, or Editor output; raw live bytes qualify only under the unit's
interpretation. File payloads are `Missing` or `Present(bytes)`; directory
children additionally preserve repository executable state.
Exact live permission policy is not a Base payload.

| Configured Sync Policy | Base-Eligible |
| --- | --- |
| `pull-only`, `both` | Yes |
| `push-only`, `push-only-delete` | No |

Guards narrow the available route for an operation, not Base eligibility.
Changing between eligible policies preserves the checkpoint.

Base inspection reports metadata and applicability without Observation,
projections, or Verification Records. Exact reset discards one Base under the
manager lock; doctor reports aggregate corrupt/orphaned counts without repair.
See [public workflows](sync-base-storage.md#public-inspection-and-reset).

### Applicability

A usable Base has a valid identity, an intact record and payload, and a
currently Base-Eligible unit. `Missing` is a valid usable payload. Git reset,
checkout, branch changes, rewritten or unavailable history, and non-Git
repositories do not by themselves invalidate or prevent checkpoints. A commit
alone never advances one.

Configuration changes never invalidate a Base. Render, Capture, profile
variables, Path Rules, and symlink interpretation modes may change freely: the
Base still records the last agreed repository-side content, which is exactly the
ancestor Merge needs. Discarding it would strip drifted units of that ancestor.
When a stored Base is known to be wrong, discard it with exact
[reset](sync-base-storage.md#public-inspection-and-reset). A stored checkpoint
remains the starting point for independent edits on either side, including
repository changes made by switching or resetting Git.

### Acknowledgment and completion

| Boundary | Base behavior |
| --- | --- |
| Fresh direct agreement in real Push, Pull, or Sync | Eligible participating units may acknowledge immediately, before review or hooks |
| Successful eligible Push or Sync publication | Save the final repository outcome after every required unit effect succeeds; no extra Render |
| Permanent Pull or repository-only Sync | Reuse agreement evidence or freeze forward validation before Apply; save a qualifying candidate after required effects succeed |
| Approved no-write resolution | Still needs Approval and the same agreement evidence; no write alone proves nothing |
| No-op Proposal (no-write, and no new Base to record) | Offers no Approval; reports `noop` without completion |
| Ineligible completion with writes | Complete without a Base or substitute receipt |

Direct agreement is **Directly InSync**, not an approved drift resolution.
A drifted unit is **Converged** after its required effects succeed, independently
of checkpoint qualification or persistence. Completion occurs at the
unit's earliest ordered position, before its enclosing target post-hook.
Later hook failure does not undo convergence. Independently approved Additional
Source Changes run in the earlier prelude; their Approval alone is not checkpoint
evidence.

Repository-only qualification compares Render of the final repository candidate
with frozen live, including executable agreement where required. Effective raw
Render can reuse identity evidence; raw Capture alone does not prove agreement
under a non-raw Render. Patch Capture can reuse its existing successful forward
validation for unchanged final inputs. Arbitrary Capture, lossy transforms, and
Editor outcomes use the same qualification rule, not success of a write alone.

Qualification uses final approved dependency inputs and freezes before Apply.
Changing those inputs invalidates the evidence; execution never rerenders or
recaptures after writes. A successful comparison that differs means the Base is
not advanced, not that the operation failed. A checkpoint-only Render failure
warns and leaves otherwise valid work executable. Render needed for publication
and patch Capture's required validation still fail materialization normally.

The payload remains repository-space state. Exact chmod may intentionally make
live permissions differ; successful required permission effects establish the
policy outcome, not equality of every repository and live permission bit.

Preview, reused Verification Records, excluded or unselected units, and units
removed by Guards never acknowledge. Failed materialization, pending or failed
required effects, and save failures before atomic replacement preserve an eligible
unit's prior authoritative Base. A checkpoint-save failure warns without stopping
remaining units or changing successful completion. If atomic replacement succeeds
but its directory flush fails, acknowledgment is true and a distinct warning
reports uncertain crash durability; the old record is not claimed to survive.
Corrupt or inaccessible Bases warn and use normal no-Base resolution rules,
without bypassing storage safety checks or automatically recreating rejected storage.

### Policy maintenance

After static configuration successfully resolves a selected unit as ineligible,
real Push or Sync deletes its old Base **before Guards and review**, without
waiting for drift, Approval, effects, or convergence. Directory children are
found among the target's stored Bases, not by discovery, so an excluded or absent
child with an ineligible configured policy is maintained too. Later interactive exclusion
does not undo or prevent that maintenance. Pull, preview, read-only inspection,
incomplete resolution, and unrelated partial selection do not perform it.
Returning a deleted identity to an eligible policy requires fresh establishment.

Storage security, atomic replacement, and inspection locking are documented in
[Sync Base storage](sync-base-storage.md).

## Permanent Pull

Pull uses the same Observation, frozen projection cache, Proposal review,
transactional Editor, canonical Additional Source Changes and repository Apply
as Sync. Its direction is fixed live-to-repository: drifted Proposals and new
Additional changes start approved, with no Resolution Intent or Merge.
Active Probe and retained hook work also start selected and may be excluded.
Guard exit 100 omits work from Observation, but each omitted scope stays visible
as unselectable **Guard skipped** auxiliary work so an empty or partial workset
is explained. Sync shows the same rows but keeps the unit, with its narrowed
policy or a no-route diagnostic.
Unapproval changes provider inputs to their frozen originals and rematerializes
approved dependants; unapproved dependants stay lazy.

Pull executes only repository effects and pull hooks, without live snapshots.
Direct agreement may establish an eligible Base during Observation; changed Pull
may establish one after qualifying repository effects. It does not use a Base for
reconciliation. Permanent Pull and repository-only Sync share checkpoint evidence
rules while retaining their distinct direction and Approval behavior.

## Permanent Push

Push uses the same Observation, Command Deck, Proposal review, transactional
Editor and Live Publication as Sync. Its direction is fixed repository-to-live:
drifted Proposals start approved with **Use repository** as the only resolution,
and Guard exit 100 omits work shown as Guard-skipped rows. Real Push performs
policy maintenance before Guards, snapshots before the first live mutation, and
may acknowledge a Base after successful eligible publication. It never reads a
Base for reconciliation.

Interactive Pull permits a healthy approved subset after materialization failure.
No-confirm Pull aborts before Apply on initial failures, including in preview.
Repository Apply is fail-fast, not transactional; completed effects remain
reported even if a later hook or write fails.
