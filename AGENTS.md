## Project structure

- Core package code lives in `src/dotman/`.
- CLI and engine coverage lives in `tests/`.
- Example repo layouts and sample config live in `examples/repo/`.
- The dotman agent skill lives in `skills/dotman/`. When a change alters user-facing behavior (commands, flags, selector syntax, workflows), update the skill along with `docs/`.

## Probes

- Outside pytest, point `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME` at a temp dir before running dotman; otherwise it reads and writes the user's real config, snapshots, and tracked state. `tests/conftest.py` shows the isolation pytest applies.
- Carry this rule into the brief of any delegated agent that runs dotman.

## UI design

- Do not forget to update style whenever add or modify a user-facing command/UI
- Stay consistent with the existing design.
- Use `repo:package.target` as the main user-facing target identifier form.
- Use `repo:package<instance>.target` for package-instance targets.
- Reserve `.` as package/target separator. Do not allow `.` inside package IDs or target names.
- Use parentheses only for optional annotations such as hook summaries, not target identity.
- Do not reveal the actual tracked root packages except in info command output.
- Reuse the same rendering for keyword terms when possible to keep the color schema consistent.
- When a change alters what the sync Command Deck shows (hints, columns, labels), regenerate the README screenshot with `docs/assets/sync-deck.capture.sh` (Linux, needs `bwrap`).

## Agent skills

### Issue tracker

Issues use GitHub. External PRs are not triage requests. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the standard five-label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Use the single-context layout. See `docs/agents/domain.md`.
