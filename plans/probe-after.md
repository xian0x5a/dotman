---
status: done
---

# Probe after upstream writes (`probe_after`)

## Goal
Fix xian0x5a/dotfiles#8: a probe target whose probe reads state that the same
run writes (`claude.claude_skills_link` reads `~/.agents/skills`, which
`agents.d_agents` and the `agents.managed_skills_reconciled` hook write) sees the
pre-write state, because every probe runs during planning. A skill added in a push
is not linked; a removed one keeps its stale link.

## Intention
The probe result that matters is the one taken after the writes it reads. Check it
then, but only when those writes actually happen in this run, so unrelated pushes
stay quiet and nothing runs "always on" (`run_noop` was rejected for that reason).

## Accepted design (confirmed with the user)
- Config: `probe_after = ["package", "package.target", ...]` on a probe target.
  Entries name the probe's inputs. Each must be the target's own package (or one
  of its targets) or a package (or target) in its `depends` closure, so it already
  runs earlier. Same repo only, packages only (no groups). An entry must not name
  the target itself or a `probe_after` target declared later in its own package
  (amended: chaining, below). `probe_after` requires `probe`. Appendable via
  `append`, like `depends`.
- Order: within its package a `probe_after` target runs after the normal targets;
  packages already run dependency-first.
- Chaining (added after the user's "go"): planning decides `probe_after` targets
  after the other probes, one at a time in execution order, so an earlier
  conditional or active one counts as work for a later one that names it.
- Planning: normal probes run first. A `probe_after` target is **conditional**
  when any listed entry has work in this run (a drifted unit row, Probe Work,
  Directory Root Work or Hook Work), matched by repo + package (+ target),
  ignoring package instance. Conditional rows skip the planning probe. Otherwise
  the probe runs once at planning: exit 0 -> plain Probe Work, 100 -> no row.
  The probe runs once per stage at most.
- Execution: a selected conditional row adds a `probe` step right before its own
  hooks. 0 -> hooks run; 100 -> step `skipped` (`probe-inactive`) and its hook
  steps `skipped` (`probe-inactive`); other exit -> failed step, stage stops.
- Own hooks only: a `probe_after` target's activation never activates its
  package or repo hooks (their pre hooks already ran by its turn).
- Output: Deck Resolution `Probe Work?`; detail panel `Checked after: <entries
  with work>`; dry-run outcome `would-check`; JSON `probe_work[].conditional`;
  stage step kind `probe`.

## Rejected
- `run_noop = true` / `--run-noop`: runs every push; papers over ordering.
- Boolean `probe_after_writes = true` over the whole dependency closure: in the
  dotfiles history most `?` rows would come from unrelated `claude` / `agents`
  changes (noise); a list is the long-term shape, so no boolean-then-list migration.
- Post target at the end of the whole run: breaks dependency-first order for
  packages that depend on it, and flags on any change.
- Hook-level `reprobe = true` on `post_push`: no row exists when planning says
  100, so the hook is never scheduled; cannot cover pre hooks.
- Watched paths: blind to hook-produced state (npx-installed skills).
- Re-probing every probe after writes: always-on noise and doubled probe cost.
- Field names `post` (reads as hook phase / always on), `probe_inputs` (hides the
  timing change).

## Work plan
1. Manifest: `TargetSpec.probe_after`, parsing, merge, inheritance table, probe-only check.
2. Planning: closure validation in `build_target_metadata`.
3. Execution metadata: `TargetPlan.probe_after`, package-last order, own-hooks-only
   activation, `probe` step, executor gating (publication + repository apply).
4. Auxiliary planning: two-phase probes, `AuxiliaryRow.checked_after`.
5. Session wiring (Sync, Push, Pull), Deck, command output, JSON, timeline.
6. Docs (`repository.md`, `sync.md`, `cli.md`, GLOSSARY), skill `skills/dotman`.
7. Independent tests, independent review.
8. dotfiles: switch `claude_skills_link` to `probe_after`; verify add/remove repro;
   close dotfiles#8.

## Validation
- Scoped pytest for manifest, auxiliary planning, publication ordering, deck/JSON.
- Issue repro against the dotfiles repo, with `HOME` and `XDG_*` pointed at a
  temp dir: add a throwaway skill, push `agents.d_agents claude.claude_skills_link`,
  the link exists; remove it, push again, the link is gone. Also a plain push with
  no upstream change shows no `?` row.

## Progress
- [x] Design confirmed with the user.
- [x] Steps 1-6 implemented; isolated smoke run covers add, remove, no-change,
  unrelated change, inactive re-check, dry-run `would-check`, JSON `conditional`.
  Full suite green after updating two exact `probe_work` JSON assertions.
- [x] Independent tests (step 7): 28 tests, then 8 regression tests for review findings.
- [x] Independent review (step 7): findings fixed in 8471ac2; re-review confirmed, one
  new minor finding (preview promised a check when every hook is noop-eligible).
- [x] dotfiles repro in an isolated clone (step 8): repair, add, remove, no-change pass.
- [x] Preview honesty: a row is conditional only if a non-noop-eligible hook waits.
- [x] Chaining: ordered planning loop, same-package declaration-order check,
  regression tests written to fail first.
- Landing (outside this repo): after merge, fast-forward the editable install, commit
  the dotfiles switch, close dotfiles#8.

## Surprises & Discoveries
- Target execution order within a package is declaration order (`build_target_metadata`
  iterates `package.targets`), not name order; `tracking.py` sorts only for display.
- Review found that gating every hook skipped noop-eligible ones, that literal package
  IDs broke `extends` children, and that cyclic `depends` voided the ordering promise.
- A probe step that gates no hook (noop-eligible only, or none in that stage) must not
  run, and preview must not promise it.

## Outcomes & Retrospective
- Shipped `probe_after` with conditional Probe Work; the design and rejected options
  moved to `docs/adr/0008-probe-after-names-a-probes-inputs.md`, behavior to
  `docs/repository.md`, `docs/sync.md`, `docs/cli.md` and `GLOSSARY.md`.
- The issue repro passes in an isolated clone of the dotfiles repo.
- Chaining was first rejected as needing sequential planning; the loop turned out
  small because only `probe_after` targets need ordering, and the other probes
  still run concurrently.
- The first design gated every hook and promised checks that never ran; asking "what
  does this step gate?" for each stage would have caught both earlier.
