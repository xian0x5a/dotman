# Dotman Domain

Dotman manages selected package content between canonical repositories and live filesystem locations.

## Language

**Sync Unit**:
The smallest file payload that can be independently included in or omitted from a sync operation. A file target is one sync unit; each child file of a directory target is its own sync unit.
_Avoid_: Package, directory target

**Verification Record**:
A local observation that a target or Sync Unit satisfied one operation's planning contract at a successful verification boundary. It may be reused while the relevant state and inputs remain unchanged.
_Avoid_: Last sync timestamp, sync timestamp

**Signature-Verifiable Sync Unit**:
A sync unit whose relevant repo and live state is completely represented by deterministic filesystem signatures and effective configuration. It does not require a Freshness Window.
_Avoid_: Simple target, raw target

**Path Rule**:
A named policy for matching child Sync Units of a directory target. Matching rules compose by priority.
_Avoid_: Path-rule index, ordered rule

**Sync Policy**:
The effective rule that constrains automatic state flow for a Sync Unit to repository-to-live, live-to-repository, both directions, or repository-directed live deletion. It does not prohibit deliberate repository changes made through the Proposal Editor. Package, target, and Path Rule configuration establish the policy; directional Guards may only narrow it.
_Avoid_: Ignore, Guard, file ownership

**Freshness Window**:
The bounded period during which verification of a target with opaque dependencies may be reused. Once it expires, the target requires full verification.
_Avoid_: Cooldown, probe duration

**Planning Strategy**:
The policy that determines whether sync planning fully verifies selected work or may reuse valid Verification Records. The supported strategies are full and fast.
_Avoid_: Mode, execution mode

**Input Fingerprint**:
The identity of the effective Dotman-controlled inputs that determine how one operation interprets a target or Sync Unit. Opaque external dependencies are excluded and bounded by a Freshness Window.
_Avoid_: Configuration fingerprint, derivation hash

**Guard**:
A directional eligibility rule evaluated before planning work for its repository, package, target, or Path Rule scope. During Sync, `guard_push` and `guard_pull` narrow repository-to-live and live-to-repository capabilities respectively without widening the configured Sync Policy.
_Avoid_: Pre-hook, execution safety check, Sync Policy

**Effective Work**:
Sync actions or noop-eligible pre/post hooks still belonging to a scope after earlier eligibility decisions and exclusions.
_Avoid_: Diff, guard execution

**Potential Work**:
Statically selected operation-eligible targets, probes, or noop-eligible hooks that may produce effective work after planning.
_Avoid_: Planned action, confirmed diff

**Probe Work**:
An active probe target presented by Sync as selectable auxiliary work that may activate applicable hooks. It has no repository/live payload, Resolution Intent, Proposal, Sync Base acknowledgment, or Converged result.
_Avoid_: Sync Unit, Proposal

**On-demand Target**:
A target declared `on_demand = true` that enters Push, Pull or Sync scope only when selected by exact target selector. Package selectors and selector-less runs leave it out silently, because that is its declared intent rather than a skipped outcome. Ownership and collision validation still include it.
_Avoid_: Explicit target, opt-in target, manual target

**Directory Root Work**:
Selectable target-owned auxiliary work that enforces an exact configured mode on an existing directory target root during live publication. It is not a Sync Unit and has no Proposal, Sync Base acknowledgment, or Converged result. It never creates an otherwise-empty directory target.
_Avoid_: Directory Sync Unit, aggregate Proposal

**Structured Transform**:
Format-aware partitioning and recomposition of document content.
_Avoid_: Render, capture, repository transform

**Transform Framework**:
Shared contract that coordinates structured transforms independently of any particular file format or repository policy.
_Avoid_: Structured transform, format transformer, repository transform

**Format Transformer**:
Reusable structured-file transformation behavior for one data format, such as JSON, TOML, plist, or XML.
_Avoid_: Transform framework, repository transform

**Repository Transform**:
Repository-owned transformation policy or behavior tied to one package, application, or repository convention.
_Avoid_: Format transformer

**Rewrite**:
Order-preserving substitution of textual content without interpreting document structure.
_Avoid_: Text transform, structured transform

**Home Path Rewrite**:
Reversible rewriting between paths under the active home directory and `~/...` when either appears as a complete path fragment in text; the bare `~` and the exact home path are rewritten only on opt-in.
_Avoid_: Home normalization, home path transform

**Render**:
Forward projection from repository representation toward live representation.
_Avoid_: Push, transform

**Base-Eligible Sync Unit**:
A Sync Unit whose configured Sync Policy permits live-to-repository flow: `pull-only` or `both`. Directional Guards do not change Base eligibility.
_Avoid_: Effective-policy unit, mergeable unit

**Sync Base**:
A repository-space checkpoint established for a Base-Eligible Sync Unit by qualifying synchronization or directly observed agreement during a real Push, Pull, or Sync. It records the shared starting point for later three-way Reconciliation independently of Git commits.
_Avoid_: Current Git HEAD, last deployed file, snapshot

**Observation**:
The frozen classification of a selected, policy-eligible Sync Unit as Directly InSync, Drifted, or Observation Failed from its policy-defined repository and live comparison states. Typed missing endpoints are evidence within the classification, not separate workflow states.
_Avoid_: Proposal, Reconciliation

**Directly InSync**:
An Observation result for a Sync Unit whose policy-defined repository and live comparison states agree before Dotman materializes a drift resolution. A drifted unit with a no-write resolution is not Directly InSync.
_Avoid_: Noop, unchanged

**Converged**:
A completion result for a drifted Sync Unit after its required final-preview Primary Source Change and Publication Effects succeeded. It describes policy-authorized completion, not fresh Directly InSync agreement or successful Sync Base acknowledgment.
_Avoid_: Applied, Directly InSync

**Capture**:
Reverse projection of live file state into repository representation before Reconciliation.
_Avoid_: Pull, transform

**Pull View**:
A side-specific projection configured under `compare` and used to compare repository and live representations during Pull or live-to-repository-capable Sync observation and review.
_Avoid_: Write preview, Pull Candidate

**Reconciliation**:
Resolution of current repository state and a Capture result by replacement or, using a Sync Base, three-way merge. It produces a Proposal or an explicit conflict.
_Avoid_: Capture, raw overwrite

**Primary Source**:
The exclusive repository source whose representation one Sync Unit reconciles. A repository path may be the Primary Source of at most one Pull Sync Unit.
_Avoid_: Additional Source

**Additional Source**:
A declared or dependency-discovered repository component staged with a Proposal because its Primary Source depends on it. It may be shared by multiple Sync Units but cannot also be a Primary Source.
_Avoid_: Primary Source, extra file

**Source Change**:
A session-local staged mutation of one normalized repository-relative source path that exists only while staged state differs from its frozen preimage. It may be shared by referencing Editors; an Additional Source Change has one canonical Command Deck row and is written at most once by Apply. Approval behavior belongs to the active command workflow rather than Source Change identity.
_Avoid_: Proposal, Sync Unit, editable copy

**Resolution Intent**:
The session-local choice of `Use repository`, `Use live`, `Merge`, or, once the Proposal Editor saved for the unit, `Edited`, for one drifted Sync Unit before Dotman materializes a Proposal. Changing it discards the prior Proposal for rematerialization but preserves Approval; returning to an earlier intent does not revive its prior frozen outcome. The saved edit stays selectable for the session: returning to `Edited` rematerializes its saved sources as a new generation.
_Avoid_: Proposal, applied resolution

**Proposal**:
The session-local candidate for resolving one drifted Sync Unit. Pull materializes its fixed live-to-repository outcome directly. Sync begins with a Resolution Intent and lazily freezes the repository outcome, policy-derived live outcome, Primary Source Change, and Publication Effects. A Proposal's Source Change and Publication Effect sets may be empty.
_Avoid_: Pull View, Source Change

**No-op Proposal**:
A materialized Proposal whose Approval would change nothing: no Primary Source Change, no Publication Effects, and no new Sync Base to record because the unit is ineligible, does not qualify, or its Base already records the outcome. Its drifted row stays as evidence but offers no Approval and reports `noop`. It arises when `compare` sees drift that Capture does not.
_Avoid_: Directly InSync, skipped

**Approval**:
The Command Deck state that authorizes a Proposal or Additional Source Change for execution. For Sync, Proposal Approval is standing authorization to execute the current successfully materialized unit outcome, including its exclusive Primary Source Change, policy-derived Publication Effects, and, for a qualifying Base-Eligible Sync Unit, Sync Base acknowledgment. Additional Source Change Approval independently authorizes its repository write, even when no approved Proposal references it. Additional Approval does not itself prove that a referencing Proposal qualifies for Sync Base acknowledgment. An unapproved Additional Source Change remains staged for review, but its candidate bytes are excluded from referencing Proposal inputs; approved referencing Proposals rematerialize immediately from the frozen repository preimage, while unapproved ones discard stale previews and rematerialize lazily. Successful rematerialization preserves Proposal Approval; failure clears only the affected Proposal Approval. Approval is independent of Proposal generation: changing Resolution Intent or editing rematerializes effects without changing the existing state; an explicit toggle or materialization failure changes it. Approval for an Additional Source path survives while no Source Change exists and returns if the row reappears. Batch actions establish every row's final Approval state before Proposals materialize against that state. Sync is opt-in, including for one-sided and no-write Proposals; Pull is opt-out. A No-op Proposal offers no Approval.
_Avoid_: Inspection status, execution result

**Selection**:
The Command Deck's per-row checkbox state and the single user-facing name for toggling it. Selecting a Proposal or Additional Source Change sets its Approval; selecting auxiliary work (probes, hooks, directory-root modes) includes it. Row states and the review title name the effect (`approved`, `selected`), while the toggle itself is always "select". Menus pick an option with "choose", never "select".
_Avoid_: Mark, approve (as the toggle's name)

**Command Deck**:
An operation's persistent workset view. It combines row-level actions, selection, and focused review while preserving the user's place in the workset. Pull and Sync may use different row states and actions; the name does not imply Proposals or Approval.
_Avoid_: Standalone selection menu, Proposal list

**Proposal Review**:
The focused full-screen view for one Proposal that shows its repository and live effect previews, Pull Views only when a drifted unit's outcome previews show no content change, Sync Base and Capture facts, reconciliation evidence, Primary Source Change approval, and referenced Additional Source Changes without mutating repository sources.
_Avoid_: Proposal window, Apply screen

**Source Change Review**:
The focused full-screen view for one Additional Source Change that shows its frozen-preimage diff, referencing Proposals, and approval action.
_Avoid_: Proposal Review, repository editor

**Proposal Editor**:
A configured or default action that edits the current Proposal through its transactional repository sources without directly mutating the repository working tree. It opens the selected Resolution Intent's outcome, materializing it first when unreviewed. If materialization failed, it starts from the current repository sources as `Use repository` would, or from the saved edit when `Edited` is selected; saving a change produces an `Edited` Proposal and replaces any earlier saved edit; quitting without changes keeps the current Resolution Intent.
_Avoid_: Outcome handler, repository editor

**Apply**:
Mutation of the repository working tree from independently approved Source Changes. Each file is replaced atomically, but a multi-file Apply may partially complete.
_Avoid_: Commit, Capture

**Repository Apply**:
Sync's operation-wide repository-destination stage. It applies approved Additional Source Changes before Proposal-owned Primary Source Changes and uses pull hooks for retained repository work.
_Avoid_: Pull, applying a Proposal

**Live Publication**:
Sync's operation-wide live-destination stage. It follows successful Repository Apply, executes frozen Publication Effects, and uses push hooks for retained publication work.
_Avoid_: Push, Render

**Publication Effect**:
A frozen write, deletion, mode change, or hook-only reapply to one Sync Unit's live target, executed during Live Publication. A reapply leaves the file untouched and only activates the unit's push hooks.
_Avoid_: Live effect, Push

**Command Runtime**:
The internal typed boundary that launches commands and owns environment construction, pipe or terminal I/O, elevation, streaming, and interruption normalization. Callers retain the meaning of command exit statuses.
_Avoid_: Hook runner, subprocess helper
