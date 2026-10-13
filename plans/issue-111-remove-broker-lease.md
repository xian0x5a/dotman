---
status: done
---

# Remove broker and lease elevation modes (#111)

## Goal
Command elevation supports only `none`, `root`, and `intercept`. Remove the
`lease` and `broker` modes and the hidden `dotman elevation request` command.
`default_command_elevation` accepts `none` or `intercept`.

## Intention
`intercept` already provides everything `broker` does: it starts the same broker,
exports `DOTMAN_ELEVATION_BROKER`, and a script can authenticate early with
`sudo -v` through the shim. `lease` only adds eager auth for absolute-path sudo.
Without a terminal, dotman cannot fix `lease` or `broker` (#108, reproduced),
while it can fix `intercept`. Fewer modes also shrink the #108 fix.

## Scope & Constraints
- Remove: mode values, `SystemCommandElevation` branches, `request_elevation_from_env`,
  the `elevation` CLI subcommand, its `--no-confirm` rejection, related tests.
- Keep the broker internals (`ElevationBroker`, shim) as the engine behind intercept.
- No compatibility path: old values fail existing config validation.
- Document `sudo -v` for early auth; record the reversal of `plans/elevation-broker.md`
  as an ADR; update `docs/repository.md`, `docs/code-structure.md`, `skills/dotman/`.
- Out of scope: the #108 shim forwarding fix; default stays `none`.

## Work Plan
1. Code removal in `models.py`, `manifest.py`, `command_runtime.py`, `elevation.py`,
   `cli_parser.py`, `standalone_commands.py`.
2. Update existing tests that use removed modes (retarget to intercept or delete).
3. Docs, ADR, skill.
4. Independent agent writes tests for the new contract (intercept `sudo -v`
   early auth). Removal is migration: no tests for rejected old values.
5. Independent review, then PR.

## Validation
- Targeted pytest for elevation, runtime, manifest, CLI parser; full suite once at the end.

## Progress
- [x] Worktree `.worktrees/remove-broker-lease`, branch `refactor/remove-broker-lease`.
- [x] Code removal, existing tests retargeted, docs, ADR 0006.
- [x] Independent tests for intercept `sudo -v`; removal-only tests dropped (removal is migration, not worth tests).
- [x] Independent review: nothing blocking; low findings addressed.

## Surprises & Discoveries
- `skills/dotman/` never listed the modes, so it needed no change.
- `tests/cli/test_elevation_interrupt.py[ctrl_c]` flaked once during independent testing (SIGINT hit the test's helper script); not related to this change.

## Decisions
- Keep default `none`: intercept as default would let any hook in an opted-out repo
  ride dotman's cached ticket silently, and loses process-group isolation.
- Rejected: keep `lease` for absolute-path sudo. No real repo needs it, and
  `sudo -v && ...` covers eager auth.

## Outcomes & Retrospective
- Elevation is `none` / `root` / `intercept`; old values fail with `unsupported elevation '<mode>'; expected one of: ...`.
- Decision recorded in `docs/adr/0006-intercept-is-the-only-user-scoped-elevation.md`; `docs/repository.md` documents `sudo -v` and the shim limits.
- Next: #108 shim forwarding, now only for `intercept`.
