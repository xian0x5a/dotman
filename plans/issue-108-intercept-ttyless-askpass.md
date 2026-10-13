---
status: open
---

# Intercept sudo authenticates itself without a terminal (#108)

## Goal
Without a controlling terminal, a `sudo` call inside an `intercept` command works
when sudo can authenticate on its own (`SUDO_ASKPASS` or NOPASSWD). Today it fails
with `sudo: a password is required` right after dotman authenticated.

## Intention
Without a terminal, sudo's default `timestamp_type=tty` falls back to per-parent-process
tickets, so dotman's ticket can never reach the shim's `sudo` (reproduced, see #108).
Instead of making dotman's ticket reach it, let that call authenticate itself: the
shim skips the broker and runs real `sudo` without `-n`, so sudo uses askpass.
The terminal path stays exactly as it is.

## Scope & Constraints
- Only the shim body in `src/dotman/elevation.py` changes.
- Detect "no terminal" the way sudo does: `/dev/tty` cannot be opened.
- With a terminal: unchanged (broker lease, then `sudo -n`).
- Without a terminal: no broker request, real `sudo <args>`; keep running it through
  the Command Runtime so interruption still exits 130.
- Cost accepted by the user: one askpass prompt per `sudo` call in tty-less runs.
  Without askpass, sudo's own error names the fix ("configure an askpass helper").
- Out of scope: the forwarding design (rejected, see Decisions), default mode.

## Work Plan
1. Independent agent writes a regression test first (tty-less intercept hook with a
   fake ppid-ticket sudo + askpass) and sees it fail for the expected reason.
2. Shim change.
3. Existing shim unit tests that assumed the broker path run without a terminal;
   give them a controlling terminal where they test the terminal path.
4. Docs: `docs/repository.md`, `skills/dotman/SKILL.md`, ADR 0007, ADR 0006 bullet.
5. Independent review, full suite, PR.

## Validation
- Regression test fails before, passes after.
- Elevation unit and PTY tests.
- Real tty-less check with sudo + askpass.

## Progress
- [x] Branch `fix/intercept-ttyless-askpass` in `.worktrees/intercept-ttyless-sudo`.

## Surprises & Discoveries

## Decisions
- Rejected: shim forwards argv/cwd/env/stdio fds to the broker so dotman runs `sudo -n`
  itself. It changed the terminal path for every intercept call (Ctrl-C leaked because
  the forwarded sudo inherited dotman's ignored SIGINT; fixable) and let any same-uid
  process that can reach the socket run root commands without a password; closing that
  needs per-platform caller ancestry checks. Archived evidence lives outside the repo.
- Rejected: won't fix + docs. D fixes the common askpass setup with ~10 lines.
- Weighted scores: D 4.3, won't fix 4.2, forwarding only when tty-less 3.9, forwarding always 3.1.

## Outcomes & Retrospective
