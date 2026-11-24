---
status: done
---

## Goal
Fix xian0x5a/dotfiles#8 with a small, consumer-declared activation rule instead of
`probe_after` (PR #358, to be closed). In the dotfiles repo, `claude.claude_skills_link`
must relink `~/.claude/skills` in the same push that changes `~/.agents/skills`, and
still repair drift when nothing upstream changed.

## Intention
A probe target may list `additional_triggers`: dependency packages or targets whose work
in the run also activates it. Active = probe exits 0 OR any trigger has work. Its hooks
then run at its own position; dependency-first order puts the triggers before it. Nothing
re-runs at execution, so there is no conditional row, no `probe` step and no own-hooks-only rule.

## Scope & Constraints
- Stacked on `fix/selector-independent-order` (step 1): dependency-first order must hold
  whatever the input order, or a trigger listed after its consumer would run late.
- Config, on a probe target only (v1):
  `additional_triggers = ["package" | "package.target", ...]`.
  - Non-empty list of printable strings; `.` separates package and target.
  - Each package must be in the declaring package's direct `depends` (after `extends`),
    without a cycle back; a target entry must name an existing, enabled target.
  - Requires `probe`. A pure trigger target is `probe = "exit 100"` for now; making
    `probe` optional later only relaxes validation.
- Semantics (decided with the user):
  - Trigger work: a drifted unit, active Probe Work (including another target activated
    by its own triggers), Directory Root Work or Hook Work, matched by repo + package
    (+ target), any package instance.
  - The consumer runs only when it is in scope: `push agents` alone does not run claude's hook.
  - Activation is normal activation: package and repo hooks of the consumer activate as
    for any active probe target.
  - No `run_noop` special case: the field adds activation only.
- Output: the row stays Probe Work. The Deck detail panel shows `Triggered by: <entries
  with work>` when triggers activated it; JSON `probe_work[]` gains `triggered_by` (list
  of `repo:package[.target]` labels, empty when the probe alone activated it).
- Docs: `docs/repository.md` (Targets), `docs/sync.md` (Auxiliary work), `docs/cli.md`
  (detail line, JSON), `GLOSSARY.md` (Trigger), `skills/dotman/SKILL.md`, ADR 0008.

## Work Plan
1. Manifest: `TargetSpec.additional_triggers`, parse/normalize, merge and `extends`
   append, probe-only check; closure validation in `build_target_metadata`.
2. Planning: in `plan_auxiliary`, after probes, root work and hook work, decide triggered
   targets in stage order so an earlier triggered target counts as work for a later one.
3. Rows/output: `AuxiliaryRow.triggered_by`, Deck detail line, JSON field, styles.
4. Docs and skill.
5. Independent tests first (fail before the code), independent review.
6. dotfiles (after dotman merges and the editable install is fast-forwarded): switch
   `claude_skills_link` to `additional_triggers`, fix the package comment, verify in an
   isolated clone (add, remove, reconcile-only, no-change, reversed inputs), close #8.

## Validation
- Scoped pytest for manifest, auxiliary planning, Deck/JSON; full suite once at the end.
- Isolated repro against a dotfiles clone with `HOME` and `XDG_*` in a temp dir.

## Progress
- [x] Step 1 branch `fix/selector-independent-order`: dependency-first packages, declaration
  order, configured repo order; independent tests failed first; reviewed.
- [x] Plan written; independent tests first (29, then 32); implementation; docs; review
  (three low findings fixed: per-stage ordering wording, vacuous shape cases, appendable list).
- [x] Isolated dotfiles repro: add, remove, no-change, reversed inputs, `push agents` alone,
  `push claude` alone.
- Dotfiles switch and closing dotfiles#8 follow the merge, once the editable install has
  the field; tracked in that issue.

## Decisions
- Rejected `probe_after` (#358): ~460 src lines, a conditional row state, an execution
  `probe` step and executor changes for one consumer; council (Fable, DeepSeek) agreed.
- Rejected a writer-side hook in `agents`: couples the writer to its consumer and creates
  `~/.claude/skills` on machines without Claude.
- Rejected cross-package `dependency_hooks`: duplicates the claude repair hook and splits
  hook owner from host position.
- Rejected putting triggers inside `probe` (special command or list): hides dependencies
  from ordering and validation, changes the probe type, and a list reads as AND.
- Name: `additional_triggers` (owner + DeepSeek): `additional_` shows the OR with the probe
  and mirrors editor `additional_sources`; `activated_by`/`triggered_by` read as the only
  condition; `probe_targets` collides with "probe target"; `additional_inputs` suggests
  the probe reads them.

## Surprises & Discoveries
- Repositories also ran in input order, against the documented configured order; fixed in
  step 1 with the package order.
- The "after the entries" guarantee holds per stage: in Sync a both-direction consumer's
  pull hooks run in Repository Apply, before Live Publication writes.

## Outcomes & Retrospective
- `additional_triggers` fixes dotfiles#8 at about a fifth of `probe_after`'s source size,
  with no new row state or execution step; decisions moved to ADR 0008.
- The cheaper shape came from asking whether the probe must re-run at all: an idempotent
  hook makes "an input changed" enough, so planning can decide everything.
