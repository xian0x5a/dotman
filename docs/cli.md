# dotman CLI Model

This reference owns command syntax, interaction, output, and exit status.
See [Sync lifecycle](sync.md) for convergence semantics and
[repository configuration](repository.md) for policy and provider syntax.

## Repos

- Dotman should search configured repos from user config.
- Repos should be searched in ascending `order`.
- Lower `order` means earlier search.
- A user may omit the repo name in normal CLI usage and let dotman search across repos.
- An explicit repo-qualified form should still be allowed when needed for disambiguation, for example `test:git@default`.

## Identifier Syntax

- The canonical repo-qualified selector form is `repo:selector`.
- The canonical repo-qualified selector+profile form is `repo:selector@profile`.
- The canonical tracked package-instance form for a `multi_instance` package is `repo:package<profile>`.
- The canonical human-facing target form is `repo:package.target`.
- The canonical human-facing package-instance target form is `repo:package<profile>.target`.
- A directory child uses `repo:package.target/<relative/child>`, with a normalized POSIX relative suffix.
- `/` belongs inside selector IDs for namespacing, for example `work/git` or `os/arch`.
- `<...>` is reserved for resolved tracked package instances, not selector+profile forms or manifest IDs.
- `.` is reserved as the package/target separator in human-facing target labels.
- Authored identity must be printable (`str.isprintable()`) and is printed as written: package, group, profile, target and repo names, a target's `source` and `path` in `package.toml`, a target's path rule `pattern`s and editor `additional_sources` entries, and a repo's `path` in `config.toml`. `docs/repository.md` and `docs/config.md` list the full rules. A value with a control character, bidi override or non-ASCII space such as NBSP fails to load with a usage error (exit 2) that names the file and the field and shows the value escaped. Python also reports private-use glyphs (such as Nerd Font icons) and zero-width joiners as unprintable, so a ZWJ emoji sequence cannot be a name or part of a path. A path is checked as written, before templates render and before `~` and `$VAR` expand, so `{{ vars.where }}/x` and `$HOME/x` pass if the text as written is printable.
- Text from outside dotman prints escaped: file names found on disk (push, pull and restore rows, review and Sync Deck panes, Editor and sudo notices), paths after templates render and `~`, `$VAR` and the config directory expand (a repo's root in `list repo`, `add` results and `info var`, a tracked target's live path), OS and exception messages, error output, a Guard's output, the config file's location in `doctor`, and tracked rows an older dotman stored. Each unprintable character shows as `\xNN` for an undecodable byte, `\uNNNN`, or `\UNNNNNNNN` above U+FFFF; a backslash stays as written. In a row or a one-line field the escape covers tab and newline too, so the text cannot start a new line; an error's detail and a Sync diagnostic keep their line breaks and tabs. A tracked row an older dotman stored with such a character, for example the profile in `work\u00a0laptop`, is listed that way, and `untrack` accepts that escaped text for the row's profile or package. File content in Sync Deck review diffs and merge-conflict blocks is split into lines only at newlines, as Git does. Its escapes cover control characters except tab, the line and paragraph separators U+2028 and U+2029, and bidi controls such as U+202E, so an ESC sequence shows as `\u001b[31m`, a form feed stays inside its line as `\u000c`, and a CRLF line in a diff ends in `\u000d`; tabs expand to spaces; Nerd Font icons, emoji and NBSP in content show as written. Text the author wrote as free text (descriptions, hook commands, variable names and values) prints as written, and the output of hooks, the editor and the pager passes through as those programs write it. Dotman trusts the repos you configure, so read-only commands (`search`, `list trackables`, `info trackable`) print a repo's descriptions as written; inspect an untrusted repo before adding it.
- When a menu, confirmation, diff banner, list output, or info view includes repo context, it should print the canonical colon-qualified form.
- `repo:` is the only repo qualifier. `repo/selector` is not a repo alias: `work/git` always means the package or group `work/git`, and a repo-scoped lookup must be written `repo:work/git`.
- `\` is not a valid selector separator or menu-display form.

## Confirmation and execution flags

- `--json` switches command output to machine-readable JSON. It is a global option and must appear before the subcommand.
- Global options must be spelled in full; dotman rejects abbreviations such as `--conf` for `--config`.
- Global `--no-confirm` suppresses every menu, Editor, pager, and prompt throughout command behavior. It uses configured defaults and fails safely when a required decision cannot be resolved.
- `--no-confirm` does not suppress sudo authentication: when elevation needs a password, sudo authenticates however it is configured: NOPASSWD, a terminal prompt, or the `SUDO_ASKPASS` helper when no terminal is present. Dotman's own sudo login passes `-A` then, so a helper that needs no display also works headless; `sudo` calls inside hooks choose their own flags. A terminal nobody watches waits at the prompt until sudo times out. Dotman never substitutes its own prompt.
- Run dotman as yourself, not under `sudo`. Dotman asks for sudo itself only for protected paths and elevated hooks, so the repo, Sync Base and state stay owned by you. Repo commands exit 2 when they run as root with `SUDO_USER` naming another user, including a `sudo -i` or `sudo -s` shell. A root login without sudo (or `su -`) and root calling sudo are unaffected. Standalone helpers (`transform`, `rewrite`, `render`, `capture`) still run under sudo, so elevated hooks can call them.
- A missing terminal never implies consent. Without `--no-confirm`, an unavailable required interactive decision fails; `--json` selects output only and never authorizes execution.
- Interruption exits 130. When a real SIGINT ended the run, dotman instead ends by SIGINT after cleanup, so the shell script or command line that ran it stops too; shells still report status 130. A real SIGINT is Ctrl-C at a terminal no menu or Deck holds in raw mode, or `kill -INT` while dotman itself holds the terminal. While a hook, Editor, or sudo prompt runs in the terminal, that child owns Ctrl-C, and dotman ends by SIGINT only if the child did. Key-driven aborts (Esc, `q`, or `Ctrl+C` pressed inside the Deck or an fzf menu) stay a plain exit 130, as with fzf itself.
- Execution hooks receive `DOTMAN_NO_CONFIRM=1` under `--no-confirm`, otherwise `0`; planning Guards do not receive it.
- `--run-noop` is meaningful for `push`, `pull`, and `sync`; Sync retains both surviving directional hook families.
- `--run-noop` now feeds normal planning and selection instead of reviving hooks late in execution.
- For the active operation, `--run-noop` temporarily treats pre/post hooks as noop-eligible, even if they do not declare `run_noop = true` in the manifest.
- `--run-noop` still does not fabricate target writes or snapshots.
- In one-sided Push and Pull, exit `100` from the operation's Guard omits
  its scope during planning; a Path Rule Guard omits matching child work.
  Repository exclusions cover lower scopes, while sibling scopes remain eligible.
- One-sided Push and Pull show each omitted scope as unselectable Guard-skipped
  work and list it in JSON `guard_skips` without command text. A wholly
  Guard-skipped one-sided operation with no retained higher-scope hooks succeeds
  without execution or snapshots.
- Sync instead intersects configured policy with surviving directional
  capabilities. For `both`, `guard_push = 100` leaves pull-only capability;
  for `push-only`, the same outcome leaves a visible non-approvable no-route
  diagnostic, not a successful omission. Guards never grant the opposite route.
  Sync also lists each exit-100 scope as the same unselectable Guard-skipped
  row and JSON `guard_skips` entry, so a narrowed policy names its Guard.
  No-confirm Sync rejects a no-route blocker before
  mutation; interactive execution may retain unrelated approved work but still
  reports failure. Configured policy continues to determine Base eligibility.

## Exit status

The same failure cause always gives the same code, whatever `--no-confirm` or `--json` say. Without a terminal, a prompt dotman cannot show is its own cause (exit 1).

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | Runtime failure: a command, hook, or template render failed (wherever the template lives: `[vars]`, manifest fields, template files); `doctor` found failed checks; a Sync Base store, dotman-written state file, or path dotman derives itself (state, live targets, repo files) could not be read; a file the user named exists but cannot be read, decoded, or parsed (for example a malformed document given to `transform` or `rewrite`); or dotman needed a decision it could not ask for (no terminal, or `--no-confirm`), including ambiguous matches a prompt could have resolved. |
| `2` | Usage or configuration error: invalid command-line syntax or option combination, a path named on the command line that does not exist, an unknown, ambiguous or invalid selector or Sync scope, an invalid or conflicting manifest or manager config (including a missing config file), an unusable environment setting such as an invalid `$HOME` for `rewrite home`, or running as root under `sudo`. |
| `130` | Interruption or explicit abort. |

Code `2` means the user's own input is wrong and no prompt would fix it; code `1` covers everything else. With `--json`, a failed Sync still prints its one JSON document under either code.

## Sync scope resolution

The Sync scope resolver accepts exact tracked identities only:
`repo:package`, `repo:package<profile>`, `repo:package.target`,
`repo:package<profile>.target`, and directory-child forms with a normalized
POSIX suffix. Multiple inputs form a stable de-duplicated union. Omitting
inputs expands the current tracked package state across every configured repo,
including dependency closure and ownership winners. Groups are catalog
selectors, not tracked identities, and are never returned as Sync scope
members.

A package input also covers its dependency closure. Rows it reaches only through that
closure, including the dependencies' Probe and hook work, are marked in the Command
Deck: a dimmed Target label and an `Included via:` detail line naming the package inputs. `--no-deps` (on `sync`, `pull` and `push`)
leaves the closure out, so a package input covers only its own targets and hooks.
Target inputs never expand a closure: naming one target brings in no dependency
package, so none of its hooks either. A selector-less run already covers the whole
tracked state, so the flag changes nothing for it.

On-demand targets (`on_demand = true`) are in scope only when an input names
them exactly, e.g. `dotman push niri-custom-git.niri_custom_git_update`.
Package inputs, including their dependency closure, and selector-less runs
leave them out silently: leaving an unnamed on-demand target out is the
declared intent, not a `[skipped]` outcome, so neither human nor JSON output
mentions it. Ownership and collision checks still cover the whole tracked
graph, so naming one never meets a conflict a plain run would not report. Sync
Base inspection (`list sync-bases`, `doctor`) still covers on-demand targets.

Disabled targets (`disabled = true`) are never in scope: naming one exactly
fails as an input that matches no tracked target.

The `sync`, `pull` and `push` commands resolve each input before scope resolution,
using the same package-or-target lookup as `edit query`: `claude`,
`claude.settings` or `dot:claude` resolve to their canonical identity when one
tracked package or target matches exactly. Directory-child suffixes are kept
verbatim after the owning target resolves. Partial or ambiguous input opens a
selection menu in a terminal; JSON, no-confirm, and non-terminal resolution
fails with the candidates rather than guessing.

## Sync

```text
dotman [--config PATH] [--json] [--no-confirm] [--file-symlink-mode MODE] [--dir-symlink-mode MODE] sync [-d | --dry-run] [--full-path] [--report] [--commit] [--no-deps] [--run-noop] [<tracked-scope> ...]
```

File-symlink modes are `prompt` (default) and `follow`; directory-symlink modes
are `fail` (default) and `follow`. Global mode flags override manager configuration.
`--full-path` shows unabridged paths in the execution timeline; deck identities remain canonical.
The Deck detail panel and Proposal Review always show full paths because they wrap long values.
`--report` (also on `push` and `pull`) prints the full entry log after the
execution timeline instead of the short recap, including unselected,
skipped and guard-skipped work.
`--commit` (also on `pull`) selects **Commit Work** for every offered repo in a
run that does not open the Deck; the Deck starts with it on. See below and
[Sync lifecycle](sync.md#commit-work).
Resolution is chosen through policy defaults or the deck, not automation flags.

Sync opens a one-shot session for file targets, independent directory children,
Probe Work, Directory Root Work, and retained hook-only work. It supports
push-only and deletion-only files with **Use repository**, pull-only files with
**Use live**, and both-policy files with **Use repository**, **Use live**, or
Base-backed **Merge**. Both-policy drift defaults to **Merge** with a usable Base;
otherwise **Use repository** is the visible fallback and Merge is unavailable.

- Interactive Sync opens a persistent Command Deck with Approval initially off. When nothing needs review
  (every target in sync, no warnings, hooks or probes; Guard skips alone do not count), it skips the Deck and
  logs directly, printing any Guard skips above the summary.
  Its Textual table aligns Selection (headed `✓`), Target, Policy, and Resolution columns.
  `x` toggles Selection (`Space` also works but goes unhinted), `a` selects eligible rows, and `u` clears Selection.
  Selection means independent Proposal or Additional Source Change Approval,
  or direct auxiliary inclusion. Batch actions establish all final states before
  rematerializing dependent Proposals.
  `/` filters the workset: the search line opens with the current filter, deck keys type
  into it, and the table narrows to Targets containing the text, case-insensitively. `Enter` keeps the
  filter, `Ctrl+U` clears the line, `Esc` restores the previous filter, and `Ctrl+C` still
  aborts. A filter without matches reports
  `No match for …` and is dropped. While filtered, the help text shows the filter and
  its count (`/zsh 2/4`), `a`/`u` act only on visible rows, and the title reports
  selections the filter hides (`(3 selected hidden)`), since confirmation still
  executes them. `q` does not abort a filtered workset; `Esc` clears the filter first.
  The filter survives opening and leaving a review.
  A single click in the Selection column toggles that row; other cells only focus it.
  The bottom help text lists keyboard shortcuts. It adapts to workset, review, and
  confirmation, wrapping to two lines on narrow terminals. Bracketed hints are
  clickable chips that run like their key: `[c confirm]` leads the workset hints, and
  confirmation leads with `[Esc return]`, then `[Enter execute]` (`[Enter preview]` under
  `--dry-run`), so a double-click on `[c confirm]` returns instead of executing. Chips keep the hint style and light up under the mouse (accent
  brackets, undimmed label). A chip runs only when the click is pressed and released on it.
  Other hints are not clickable.
  Long Target identities are shortened in the middle (`…`) so every column fits
  the terminal width; the focused detail line shows the full identity. Arrow keys
  and vim-style `h`/`j`/`k`/`l` navigate; terminals too narrow even for shortened targets scroll horizontally,
  and long worksets scroll vertically without losing column alignment.
- `r`, or a click in the Resolution cell, changes the focused row's Resolution:
  with two allowed intents it toggles between them; with three or more it opens a menu.
  A saved Editor outcome adds **Edited** as an intent for the rest of the session, so
  switching away never loses it: a one-way Policy toggles with Edited, a two-way one opens the menu.
  The `r intent` hint appears only when the focused row has a choice. A Resolution
  fixed by a one-way Policy (push-only Use repository, pull-only Use live) is dimmed
  like auxiliary work, so only choosable Resolutions keep the emphasis.
- `Enter` opens focused Proposal Review or canonical Source Change Review;
  `Esc` returns to the same workset. Source Change Review shows the preimage diff,
  referencing Proposals and independent Approval; Proposal Review lists source
  references without a duplicate Additional Approval control. Both review titles
  annotate the focused row's Approval (`:: Proposal Review (approved)`) so `x`
  shows its effect while the Decision section is scrolled away. Proposal Review
  opens with the target and groups facts under full-width titled rules
  (`── Decision ───`; copied text uses `:: Decision`): Decision (Approval, Resolution, effective
  policy, and configured policy only when it differs), Paths, State (Observation,
  Base availability, Capture, Reconciliation, Primary Source Change authorization),
  Merge conflicts when a merge conflicted (Git `zdiff3` blocks with three context
  lines, repository side green, Capture side blue, Sync Base dimmed), then exact Publication Effects, the repository and live old-vs-new effect previews
  (Git-style `new file` / `deleted file` headers mark existence changes), and last, only for a
  drifted unit that writes nothing, a Drift section diffing the compared repository and live copies. Base and Capture appear only as
  State facts, without diffs of their own. Fact labels are dimmed with values aligned in one column; diffs
  color removed, added and hunk lines. Long lines wrap to the terminal width, with
  continuations hanging under their value or after the diff marker, and rewrap on
  resize. A notice answers the previous key, so the next key press or click clears
  it; clicking the notice dismisses it without other effects. When a key cannot act
  on the focused row, the notice says why in words, for example that an
  Observation failed and sync must run again after the fix. The `y` copy
  confirmation and `No match for …` also clear themselves after a moment.
  `v` in review opens Full View: one diff or merge-conflict section with the whole
  file as context instead of three lines. With several such sections (for example,
  repository and live effect previews, or Merge conflicts and Drift) it first offers a
  menu; with one it opens directly. Full View opens at the top unless the first change
  starts below the screen, in which case it lands on that change; `n` and `N`
  step to the next and previous change block. Full View is read-only: it scrolls and
  copies with `y`, and `Esc` returns to the review at its prior position.
  `/` in review or Full View opens a search line above the help text; while it is open,
  deck keys type into it. `Enter` highlights every case-insensitive match and lands on
  the first one at or below the current position; `Ctrl+U` clears the line, `Esc` cancels,
  and `Ctrl+C` still aborts. While a search is
  active the help text shows it with the match count (`/zsh 3/12`), and `n`/`N`
  step to the next and previous match, wrapping around, instead of change blocks.
  `Esc` clears the search before it leaves the view, and leaving the view clears it too.
  A search without matches reports `No match for …` and is not kept.
  In both search lines `↑`/`↓` recall the last 10 queries, newest first; workset
  filters and reader searches keep separate histories for the session, and `↓` past
  the newest query restores the text typed before recalling.
  `c` opens one compact confirmation with selected units, repository changes,
  and changed live targets, each counted once whether written, deleted,
  mode-changed, or reapplied; deletions and reapplies are also shown in brackets
  (`live: 3 (1 deleted, 1 reapplied)`). Confirming with nothing selected is allowed
  (nothing to do is a valid decision) but adds a warning that the drift stays pending. Cancelling returns without
  changing Approval, inclusion or focus. Real execution is offered only for valid,
  completed approved Proposals; confirming executes the already-reviewed set without
  further materialization.
  Below the summary, a scrollable **Commit to git** list offers **Commit Work**
  for each dotman repo the selection writes that has drift and sits in a git work
  tree: each entry shows `[x] main@main`, the repo and its branch, over its dimmed
  commit subject (`chore(dotman): pull app (2 targets)`); long lines wrap under
  the repo rather than being cut, since the list has no detail panel. `x`, `Space` or a click toggles one repo; `g`
  turns every repo on unless all are on, then turns them all off. Choices start on,
  survive leaving confirmation, and are never
  touched by workset batch selection. Preview offers no list.
  Review scroll position is retained per target; arrow keys, `j`/`k`,
  Page Up/Down, and the mouse wheel scroll frozen evidence.
  `y` copies to the terminal clipboard (OSC 52): the full Target identity from the
  workset; in review, the mouse-dragged selection when there is one, otherwise the
  whole review text. `Ctrl+C` stays Abort; outside review, most terminals bypass
  the deck's mouse capture with Shift+drag for ad-hoc selection.
  `q` from the idle workset or `Ctrl+C` aborts; `Esc` only steps back and does nothing at the workset. During Capture, Merge, and Render,
  the visible deck animates a busy indicator; other keyboard/mouse actions are
  ignored until materialization finishes. `Ctrl+C` or OS SIGINT cancels running
  materialization and the remaining batch, waits for owned process cleanup, and
  exits 130. Completed effects are not rolled back; detached descendants are not
  covered by owned-process cleanup.
- `e` opens the focused drifted file's Proposal Editor from the workset or
  review, including one-sided policies and recoverable Proposal failures.
  The Editor works on isolated repository source copies; quitting without changes keeps the
  Resolution and says so; saving a change shows **Edited**
  and rematerializes policy-appropriate effects without writing tracked sources.
  Successful editing preserves Approval; cancellation preserves the previous
  Proposal. Failed materialization clears only that unit's Approval.
  `t` retries materialization; Editor attempts can be retried with `e`.
  `L` explicitly authorizes prompt-mode live-link replacement for the focused
  Proposal; it does not select or approve it. Review shows authorization in the
  shared warning/success colors. No-confirm Sync cannot supply this decision.
  During editing, `Ctrl+C` or OS SIGINT cancels only the Editor attempt,
  including a sudo password prompt triggered through the intercept shim. Prompt cleanup and terminal
  restoration finish before the deck resumes; `e` can start a fresh attempt.
  Terminal Editors take terminal ownership temporarily and return to the same
  focused deck. No Editor runs automatically or under `--no-confirm`.
- **Tab** focuses the ringed detail panel below the table so arrow, vim and page
  keys scroll it; **Tab** or **Esc** returns to the workset. Row commands keep
  acting on the focused row. The mouse wheel scrolls it without moving focus.
- **Observation failed** and **Proposal failed** retain their diagnostic details
  in the detail panel. Directory scopes discover independent canonical child rows.
  Their **Selection** grants opt-in Approval, just like file targets. Each child
  has its own policy-allowed Resolution, Proposal, effects and completion result.
  Review shows child executable changes with Git mode lines. Exact Path Rule
  chmod affects live publication only; it is not repository or Merge ancestry.
  Controls and exclusions apply symmetrically even for an exact child scope.
- `--no-confirm` uses policy defaults, selects every eligible Proposal, Additional Source Change and auxiliary row, materializes that set, and confirms execution.
  Commit Work stays off unless `--commit` selects it.
  Both-policy drift without a usable Base is the exception: nothing shows which
  side changed, so it stays unselected and prints
  `[skipped] <identity> (no Base)` with a first-review hint. JSON lists it in
  `no_base_skips` with the Base reason (`absent`, `unreadable`, `record_corrupt`,
  or `payload_corrupt`), and the exit status is unaffected. To
  bootstrap a host without the Deck, run `dotman --no-confirm push` (or `pull`) once.
  The next Sync sees both sides agree and records a Base.
  Missing terminals and `--json` do not grant consent. Required interactive
  decisions without a terminal fail rather than selecting work implicitly.
  Ambiguity, unsafe symlink decisions, conflicts, Observation or materialization
  failures, and unsatisfied structural closure fail before mutation.
- `--dry-run` reports frozen approved outcomes without hooks, managed writes,
  snapshots, Base acknowledgment or cleanup. Interactive preview uses the normal Command Deck. No-confirm preview
  selects and materializes all eligible work using policy defaults, reports frozen
  effects without confirmation, and fails if any blocker makes it unexecutable.
- Active Probe Work has no Proposal, Approval, Base or Converged result. Its
  selection activates only Guard-surviving hook families. Hook-only rows show
  canonical scope with `(pull-hooks)` or `(push-hooks)`, never a file identity
  invented for hooks. `--run-noop` retains otherwise inactive pre/post hooks
  without restoring a Guard-removed capability.
- Pull-only review lazily Captures live, shows the repository
  outcome and leaves live unchanged. Confirmation counts repository changes
  separately from live effects. A no-write drift resolution requires Approval
  and completion at its normal ordered position to become Converged when it can
  record a new Sync Base. Checkpoint qualification and persistence are separate
  from successful completion.
- A No-op Proposal (nothing to write and no new Sync Base to record) keeps its
  row as evidence but offers no Approval: the row shows `[-]` with Resolution
  `No-op`, `x` explains why, Review shows `Approval: not needed` plus a
  hint that `compare` does not match Capture, and results report `noop`
  (listed only with `--report`).
- JSON emits one clean final document with `operation`, `mode`, `status`,
  `scope`, `summary`, `sync_units`, `additional_source_changes`, `probe_work`,
  `directory_root_work`, `hook_work`, `commit_work`, and `stages`. Hook output is captured
  so stdout remains valid JSON. It reports
  evidence and effect metadata, never file content bytes, temporary paths, workspaces, handles, or private plans.
  Stage outcomes preserve the failed step and the ordered unattempted tail
  (`status: unattempted`, `skip_reason: earlier-failure`). Units with successful
  repository changes but unattempted live work report `not-converged`, rather
  than `skipped`; previously completed units remain `converged`.
  Directory children expose the same Observation, Approval, Proposal and result
  fields as file targets; Primary Source write summaries also include `executable`.
  `probe_work`, `directory_root_work` and `hook_work` contain canonical `identity`, `selected`,
  `directions` and `diagnostics`, without file/Proposal/Base fields.
  `summary.selected_auxiliary` counts directly selected auxiliary rows.
  `commit_work` lists each offered repo with `repo`, `branch` (null when detached),
  `selected`, `result` (`committed`, `no-changes`, `skipped`, `failed`,
  `interrupted`, or null when not run), short `commit` hash, full `message` and
  `error`; `summary.commits` counts commits made.
  Each unit's `resolution_intent` reports the selected intent, `editor` once Edited is selected;
  `allowed_intents` includes `editor` after a save; `resolution` reports `editor` for an Edited Proposal and `generation` identifies
  the materialized generation. Top-level `additional_source_changes`
  contains canonical `row_id`, `repo`, repository-relative `path`, `approved`,
  reverse `references`, change `kind`, byte count `bytes` (not content), `result` and `diagnostics`.
  Each unit's `additional_source_changes` lists its canonical source row IDs.
  Source execution outcomes are separate from unit convergence. Candidate bytes
  enter referencing previews and the final execution set only while approved.
  `summary.approved_additional_sources` counts independently approved sources.
  Each unit's `primary_source_change` describes its repository write/deletion
  (or is null); `effects` contains only live Publication Effects. Summary
  `repository_changes` counts selected Primary and Additional Source Changes. Base
  `status` remains frozen opening evidence. `base.qualified`
  reports the materialized Proposal's checkpoint evidence (null without a Proposal);
  `base.acknowledged` reports successful real-operation persistence independently
  of the unit's completion result. Diagnostics include `code`, `message`, and
  `severity` (`warning` or `error`). Checkpoint-only warnings do not turn completed
  required effects into execution failures.
  Units directly in sync without diagnostics are omitted from `sync_units` and
  `scope`; `summary.in_sync_units` counts them, and human output shows the count
  as `in-sync: N` on the final summary line.
  Execution outcomes identify their repository, package instance or target scope
  canonically, including failed hooks whose units already converged.
- Human output distinguishes direct agreement, convergence, deliberately pending
  work, and failures; summaries count repository changes, live writes and deletions,
  and selected auxiliary work separately. Checkpoint warnings remain visible even
  when synchronization succeeds and the Base is not advanced. Base saves are
  bookkeeping: the final report never prints a Base line of its own, so Base
  problems appear only as unit diagnostics.
  A `base-durability-uncertain` warning means replacement committed but its final
  flush failed: acknowledgment remains true, without a crash-durability guarantee.
- Exit `0` means healthy completion, including deliberately unselected healthy
  interactive rows and completed work with checkpoint-only warnings. Exit `1`
  covers Observation, materialization, blocking,
  decision-required, execution, or incomplete-convergence failure, and a
  Command Deck that stops on an unexpected error (status `failed`, code `deck-failed`). Partial
  convergence with any failure exits `1`. Exit `2` means a usage or configuration error,
  such as an invalid Sync scope; exit `130` means interruption or explicit abort.

Examples:

```sh
dotman sync main:app.settings
dotman --json --no-confirm sync --dry-run main:app.settings
dotman --no-confirm sync main:app.settings
```

## Selectors

- Normal CLI usage should accept a type-less selector.
- A selector may resolve to either a package or a group.
- Examples:
  - `dotman track git@default`
  - `dotman track os/arch@basic`
- Selectors should be searched across configured repos in repo `order`.
- Exact selector matches should take priority over search behavior.
- If the same exact selector exists in multiple repos, dotman should display an interactive repo selection menu.
- In non-interactive mode, an exact selector collision across repos should fail and print the candidates.
- If a selector matches both a package and a group, that is an ambiguity error.
- Only in collision/debug cases should explicit forms such as `packages/...` or `groups/...` be needed.
- Partial selector input should also be accepted for interactive usage.
- If the input is not an exact selector, dotman should search package and group IDs for matches.
- If partial lookup finds exactly one match, dotman may use it directly.
- If partial lookup finds multiple matches, dotman should display an interactive selection menu.
- In non-interactive mode, partial lookup with multiple matches should fail and print the candidates.
- If no matches are found, dotman should fail fast.
- Interactive ambiguity menus should use the shared CLI selector flow across `track`, `untrack`, and `info tracked`; `push`, `pull` and `sync` use scope resolution instead.
- For shorter candidate lists, dotman should print a numbered menu and let the user pick.
- Selector labels in those menus should use canonical selector text, and include the repo only as disambiguation context.
- For longer candidate lists, dotman should prefer `fzf` when available instead of dumping a tall numbered menu.
- The fzf menus need fzf 0.54 or newer. Esc, Ctrl-C, or Enter with no match cancels like Ctrl-C at a prompt; any other fzf failure (for example an older fzf rejecting an option) is an error that shows fzf's own message.
- The fzf menus keep the look set in `FZF_DEFAULT_OPTS` but reset the options there that change how fzf reads its list or prints the pick (`--multi`, `--print-query`, `--expect`, `--read0`, `--print0`, `--header-lines`). `--accept-nth` in `FZF_DEFAULT_OPTS` is not supported.
- Interactive selector menus should render bottom-up by default.
- Bottom-up selector menus should remain user-toggleable, but the default should favor bottom-up display.
- Manager config may override the default under `[ui.menus]`, and `DOTMAN_MENU_BOTTOM_UP` should still act as a single-run override.

## Search

- `dotman search <query>` is the selector-discovery command.
- It should search package IDs, group IDs, and descriptions across configured repos.
- Search is read-only and does not mutate tracked state.
- For full catalog browsing, use `dotman list trackables`.
- Exact repo-qualified or bare selector matches should rank ahead of prefix, substring, and description matches.
- Human output should render the canonical `repo:selector` form, followed by badge-style labels like `[package]` or `[group]` and a dim description in parentheses.
- JSON output should include the operation name, original query, and the ordered `matches` list.
- An empty query should fail fast.
- Search and `list trackables` skip a package whose `extends` parent the repo does not define, the same dangling reference that makes a tracked row stale. Each skip prints `warning: skipped package <repo>:<package> (package '<package>' extends '<parent>', which repo '<repo>' does not define)` to stderr, once per command (JSON stdout is unaffected). Selecting the skipped package by name (`info trackable`, `track`) fails with the same detail. An `extends` cycle or malformed manifest is a repo error and still fails the command.

## Diagnostics And Catalog Inspection

- `dotman --version` shows the installed build: a release tag such as `0.10.1`, or a development build past it such as `0.10.2.dev3+g1a2b3c4`. The version is fixed at install time, so reinstall to refresh it.
- `dotman doctor` exits 1 when any check fails. It checks manager configuration, configured repo paths, tracked state, required external tools, the configured editor (a warning when none is found; see [Edit](#edit)), and aggregate corrupt/orphaned Sync Base counts. Store failures include repository, path, and cause; no repair is performed.
- `dotman list sync-bases` lists only currently usable Sync Bases.
- `dotman info sync-base <sync-unit>` shows metadata and applicability for one exact repo-qualified file target or directory child; unavailable and ineligible results succeed.
- `dotman reset sync-base <sync-unit>` immediately and idempotently discards one exact Base under the manager operation lock. It has no confirmation, dry-run, fuzzy selection, or scope expansion.
- `dotman reset sync-base --unusable [repo]` replaces `<sync-unit>` to discard every Sync Base that `doctor` counts as corrupt or orphaned, in the named repo or all repos, under the same lock and without confirmation. Give exactly one of `<sync-unit>` or `--unusable`. Output is the dropped count only: `N unusable Sync Bases reset` (JSON `{"status": "reset", "dropped": N}`).
- Sync Base inspection never observes drift, runs projections, or accesses Verification Records. See [Sync Base workflows and output](sync-base-storage.md#public-inspection-and-reset).
- `dotman list repo` lists configured repos in configured order.
- `dotman list trackables` lists every package and group available across configured repos.
- `dotman info trackable <query>` shows one package or group definition with its tracked status.
- Use `--json` with these commands when another tool needs structured output.

Examples:

- `dotman doctor`
- `dotman list repo`
- `dotman info trackable main:git`

## Profiles

- Profiles are separate from selection.
- Profiles provide variable values used during resolution.
- Profiles may include other profiles.
- The selected top-level profile is the bound profile.
- Persisted tracked package entries should store the top-level profile the user selected, not the flattened leaf profile set.
- The composed/effective profile is runtime resolution context, not tracked package identity.
- The same package or group should be applicable with different profiles.

## Track Requests

- The CLI should treat user input as a track request: `repo:selector@profile`.
- A track request combines what to manage with which variable context to resolve it under.
- Examples:
  - `main:git@default`
  - `test:os/arch@basic`
- Repo qualification is the explicit and stable form.
- CLI input may still omit the repo name when dotman can resolve it from configured repo search.
- Profile should not be encoded into repository paths or package/group IDs.
- A tracked `multi_instance` package instance is addressed as `package<bound-profile>` in tracked-package views.
- `track` is command that creates or replaces persisted explicit package entries.
- `push` with no selector and `pull` with no selector should replay persisted explicit package entries.
- `push` and `pull` should resolve profile includes from the current repo state each time, not from stored leaf profiles.
- `selector@profile` is a convenient shorthand when repo resolution is unambiguous.
- A bare selector such as `git` or `os/arch` may still be accepted for interactive commands.
- `track` may resolve a bare or partial selector first, then prompt for a profile interactively.
- If there is exactly one available profile and the user did not type a partial profile query, dotman may use it directly.
- In non-interactive mode, a missing profile should be an error instead of a hidden guess.

## Track

- `track <selector[@profile]>` should resolve the track request, prompt for a profile when needed, and persist the resulting explicit package entries into repo state.
- If the input selector is a group, `track` should expand it exactly as if the user had listed each resolved package selector explicitly.
- Persisted tracked package state should never keep historical group selectors; after tracking, state should contain package selectors only.
- Re-tracking the same effective package entry should update that entry instead of appending a duplicate.
- A tracked `singleton` package should have at most one explicit package entry per repo and package ID.
- A tracked `multi_instance` package may keep one explicit package entry per bound profile.
- If tracking would replace an existing explicit package entry with a different profile, interactive mode should ask for confirmation before writing state.
- Without a terminal or under `--json`, profile-replacing `track` should fail instead of silently overwriting that explicit package entry. With `--no-confirm` it replaces the entry; human output prints an `Applying tracked package entry replacement` notice naming the existing and new entries.
- Tracking a `multi_instance` package with a different bound profile should add a distinct explicit package entry instead of replacing the existing one.
- If `track` would make a new explicit package entry override existing implicit targets, interactive mode should ask for confirmation before writing state.
- Without a terminal or under `--json`, `track` should fail instead of silently overriding implicit tracked targets. With `--no-confirm` it applies the override; human output prints an `Applying explicit override` notice.
- `track` should validate the future expanded tracked graph before writing. A singleton dependency cannot be implicitly required by different tracked roots under different profiles unless one explicit singleton dependency profile owns that package identity.
- To fix an implicit dependency profile ambiguity, use the same profile for the roots, make the dependency `multi_instance`, explicitly track the desired singleton dependency profile, or move the overlapping target/config into the shared dependency package.
- `track` is state-only. It should not run repo-to-live work by itself.
- Tracked target ownership is metadata-first: one live path may have one winning package instance. Same-precedence package instances that declare the same live path conflict even if their current rendered bytes would match.
- If multiple packages need the same live path, move that target into a shared dependency package instead of duplicating the target declaration.
- Examples:
  - `dotman track main:git@default`
  - `dotman track git`

## Operation locking

Real `push` and `pull` acquire the manager-wide non-blocking operation lock before
planning and hold it through review and execution. Another real Push, Pull or
engine SyncSession using the same manager state area fails immediately with the
normal error presentation. Preview does not acquire or create the lock.
Restore and unrelated state commands are outside this lock.

## Push

Push publishes repository outcomes to tracked live paths. It shares the frozen
workset and Command Deck with Sync and Pull, but its only resolution is
**Use repository** and it never writes repository sources on its own.

- `dotman push [repo:package.target ...]` resolves scopes like Pull. Omit
  scopes to use all tracked targets, including dependency closure and ownership
  winners. Untracked input fails; use `track` first.
- Only push-capable targets (`push-only`, `push-only-delete`, `both`) participate.
  Drifted Proposals start approved. `x` opts out; Enter inspects frozen Views
  and the live outcome; `e` edits the repository source transactionally; `t` retries
  a failed Proposal. There is no Resolution Intent or Merge choice.
- Noninteractive use requires global `--no-confirm`, including `--dry-run` and
  `--json`; without a terminal Push otherwise fails with `decision-required`.
  No-confirm Push aborts before publication if an initially approved unit fails.
- `-d` / `--dry-run` runs frozen planning and review without live writes,
  snapshots or Base saves. `--run-noop` retains eligible auxiliary hook work;
  active Probes and retained hook work start selected and can be opted out.
- Only push Guards and hooks run. Guard exit 100 omits its scope before
  Observation and appears as unselectable **Guard skipped** work, e.g.
  `main:app.unit (guard_push)`, in the deck, result output and JSON
  `guard_skips`. Any other Guard failure aborts before review.
- Real Push deletes the Sync Base of statically ineligible selected units before
  Guards and review, as described in [Sync policy maintenance](sync.md#policy-maintenance).
- A file target's missing repository source is a typed Missing outcome: Push
  deletes the selected live file, or plans no content change if live is also
  missing. Render does not run on an absent source, even when configured; Missing
  is not an empty file or input to a generator. Use explicit `type = "file"` when
  both paths are absent and file-kind inference is otherwise impossible.
- Before the first live mutation of a real Push, dotman creates one manager-level
  snapshot of the paths it will change. Hook-only work and direct agreement
  create none. A snapshot survives later failure for inspection or `restore`.
- Publication is fail-fast and nontransactional: completed writes survive later
  failure, and remaining work is reported skipped.
- Fresh direct agreement and successful eligible publication may save the frozen
  repository-space outcome as a Sync Base. Checkpoint-only failures warn without
  stopping later work.
- `file_symlink_mode = prompt` requires explicit per-row link replacement
  authorization (`L`); no-confirm Push cannot grant it. A dangling live link
  is replaceable the same way for push-only work; anything with pull capability
  still rejects it, so it is never read as a deleted live file. `follow` writes through
  to the resolved target. `dir_symlink_mode = fail` rejects symlinked directory
  roots; `follow` manages the resolved tree. CLI flags override either mode for one run.
- Results use the shared Sync document: drifted units report `converged`,
  `would-converge`, `pending` or a failure status.

Examples: `dotman push main:git.config`, `dotman --no-confirm push -d`,
`dotman --json --no-confirm push`.

## Pull

Pull permanently copies live outcomes into tracked repository sources. It shares
the frozen workset and Command Deck with Sync, but has no Resolution Intent or
Merge choice and never publishes to live paths.

- `dotman pull [repo:package.target ...]` resolves exact tracked scopes. Omit
  scopes to use all tracked targets; package instances use `repo:package<instance>.target`.
- Drifted Proposals start approved. `x` opts out; Enter inspects frozen Views
  and the repository outcome; `e` edits transactionally; `t` retries a failed Proposal.
- Raw, verified Patch Capture and Missing outcomes use frozen opening evidence.
  Capture reads the live endpoint; its result is reused, not rerun for review.
- Saved Additional Source Changes start approved independently of their Primary
  Proposal. Unapproving one restores frozen original provider inputs, eagerly
  rebuilding approved dependants and invalidating unapproved ones for lazy reuse.
- Capture failure never launches an Editor automatically. Explicit editing may
  recover the failed unit, which still needs Approval. Interactive execution may
  retain a healthy approved subset; failures remain visible in the result.
- Noninteractive execution requires global `--no-confirm` and aborts before Apply
  if an initially approved unit fails. This also applies to preview.
- `-d` / `--dry-run` runs frozen planning and review without repository Apply.
  `--run-noop` retains eligible auxiliary hook work. Active Probes and retained
  hook work start selected and can be opted out in the deck.
- `--commit` selects Commit Work outside the Deck, as in Sync: after a completed
  run, each selected repo commits the sources Pull wrote ([Sync lifecycle](sync.md#commit-work)).
- Only pull Guards and hooks run. Guard exit 100 omits its scope before Observation.
  The deck keeps one unselectable **Guard skipped** row per omitted scope, e.g.
  `main:app.unit (guard_pull)`, whose detail names the Guard and its first output
  line. Result output prints `[skipped] <scope> (guard_pull)`, and JSON lists
  `guard_skips` with identity, direction, scope kind, Path Rule pattern and
  reason, never command text.
  Apply is fail-fast and nontransactional: completed writes survive later failure,
  and remaining work is reported skipped. Pull creates no live snapshot.
- Directly agreeing eligible observations may establish a Base. Changed Pull does
  not use a Base for reconciliation, but may save its final repository outcome
  after successful Apply when frozen evidence proves agreement with live.
  Qualification reuses existing proof or checks Render before Apply; a mismatch
  does not fail Pull. Checkpoint-only validation and storage failures warn without
  stopping otherwise valid work. Required Capture and patch validation still block
  failed materialization.
- Symlink and directory safety use the same declared identities and frozen
  Observation constraints as Sync.

Examples: `dotman pull main:git.config`,
`dotman --no-confirm pull -d`, `dotman --json --no-confirm pull`,
`dotman --no-confirm pull --commit`.

## Capture

- `capture` is the helper namespace for built-in reverse-capture tools.
- `capture patch` is the built-in automatic patch helper for patchable rendered/template file targets.
- `capture patch` should accept `--repo-path`, `--render`, `--review-repo-path`, `--review-live-path`, and the same template-context flags currently used by `render jinja` (`--profile`, `--os`, `--cpu-arch`, and repeated `--var`).
- `capture patch` should output the patched repo source to stdout.
- `capture patch` reprojects the patched repo file through the forward render path and must match the reviewed live bytes exactly.
- If that verification fails, `capture patch` exits non-zero and Pull leaves the Proposal failed and unapproved; an unverified patch is never applied.
- The built-in target helper should reuse the same implementation as `dotman capture patch`.
- Use `capture = "patch"` for automatic template-style reverse capture when dotman can patch source deterministically and verify the result; use `editor` when a human needs to inspect or edit source reconciliation manually.

## Restore

- `restore` should restore managed live paths from a previously recorded snapshot.
- `restore` should not resolve current repo manifests, current tracked package entries, or profile state.
- `restore` with no snapshot reference should target the latest restorable snapshot.
- `restore <snapshot>` should accept either `latest`, an exact snapshot ID, or a unique leading prefix such as a date or timestamp fragment.
- If a snapshot reference matches multiple snapshots, interactive mode should prompt for one snapshot and non-interactive mode should fail with the candidates.
- `restore` should accept `-d` / `--dry-run` as an explicit preview-only mode selector.
- `restore` should accept `--full-path` to disable human-output path compaction for preview, review menus, and human execution output.
- Plain `restore` should perform real execution after planning and inspection-only diff review.
- Preview and execution headers should show the snapshot ID, creation time, and status in the same form as `info snapshot`.
- Review items should be labeled with the target recorded in the snapshot (`repo:package.target`, or `repo:package<instance>.target` for a `multi_instance` package instance), or with the live path when the snapshot recorded none.
- Diff review should use `git diff --no-index --color=auto`.
- Diff review headers should use explicit `live/...` and `repo/...` paths instead of opaque `before-*` or `after-*` temp names.
- Diff review headers should compact long compared paths for readability, and should additionally collapse the current home directory to `~` instead of a machine-specific absolute prefix.
- Each reviewed diff should print a compact banner before the diff output so sequential reviews do not run together.
- Each reviewed diff should print the destination path under the banner so directory-target child diffs are identifiable even when the target label names the directory root.
- Mode-only diff review should follow Git semantics: show executable-bit changes through `git diff` mode lines, but do not show or plan non-Git permission drift such as `600` vs `644` for directory-target child files.
- In interactive review, diff output should prefer Git's pager and fall back to `less -FRX -R` when the effective pager resolves to `cat`.
- Review commands should support inspecting one item, inspecting all items, listing items, skipping remaining review, or aborting.
- `restore` should compare the current live state against the selected snapshot state without consulting the current repo contents.
- `restore` should restore only the live paths recorded by the selected snapshot.
- `restore` should not run package hooks.
- `restore` should fail fast if the selected snapshot is missing required stored content or has an invalid manifest.
- Examples:
  - `dotman restore`
  - `dotman restore latest`
  - `dotman restore 2026-04-09`
  - `dotman restore 2026-04-09T14-22`
  - `dotman restore --dry-run`

## Snapshot History

- `list snapshots` should list available snapshots in newest-first order.
- `list snapshots` should not require current repo resolution or tracked package entries.
- `list snapshots` should stay overview-oriented by default. It should show summary metadata, not dump every recorded path.
- Each listed snapshot should include a human-readable creation time, a copyable snapshot ref, status, and a compact count of recorded path entries.
- If a snapshot has been restored before, list output should also surface restore metadata such as restore count and most recent restore time.
- Snapshot list output should make it easy to copy a date or timestamp prefix into `restore <snapshot>`.
- `info snapshot <snapshot>` should show detailed information for one snapshot, including recorded path entries.
- `info snapshot` should accept `latest` as a snapshot reference alias for the newest available snapshot.
- `info snapshot` should accept `--full-path` to disable path compaction in human-readable path output.
- Examples:
  - `dotman list snapshots`
  - `dotman info snapshot latest`
  - `dotman info snapshot 2026-04-09`
  - `dotman info snapshot --full-path 2026-04-09`

## Add

- `add` is the unmanaged-to-managed adoption command for creating or updating repository package config from an existing live path.
- `add` should accept `dotman add <live-path> [<repo>:]<package-query>`.
- The live path must be the first positional argument so relative paths such as `.config/nvim/init.lua` do not collide with namespaced package IDs such as `work/git`.
- `live-path` may be relative, `~`-prefixed, or absolute.
- Relative `live-path` input should resolve against the current working directory.
- `add` should fail fast if the resolved live path does not exist.
- `add` should fail fast on symlinks.
- `add` refuses a live path with an unprintable character in its file name or any parent directory (exit 2, nothing written), because the manifest it would write could no longer load.
- `add` should auto-detect whether the live path is a file or directory.
- If the optional package query is provided, dotman should resolve it against package IDs across configured repos using the same repo-aware exact and partial lookup model used elsewhere.
- Package queries may be package-only such as `git`, repo-qualified such as `main:git`, or partial forms such as `ma:gi` for interactive resolution.
- Interactive package resolution for `add` should include a synthetic create option as the first menu entry.
- If the package query is omitted, `add` should be interactive-only and should present a package picker across all repos, with `create a new package` as the first option.
- In non-interactive mode, omitting the package query should be an error instead of a hidden guess.
- Selecting create should always end with an explicit final repo and explicit final package ID before writing any files.
- Partial package text may help find an existing package, but dotman should not silently create a package from an unresolved partial query.
- In non-interactive mode, only exact package matches resolve. A bare query with partial matches fails and names them, as other commands do; a repo-qualified query such as `main:git` creates package `git` even when `main:gitui` exists.
- If the selected package does not exist yet, `add` should create `packages/<package-id>/package.toml`, including namespaced package paths such as `packages/work/git/package.toml`.
- `add` refuses to create a package inside another package's folder, or in a namespace folder that already holds packages, because packages cannot nest.
- If the selected package already exists, `add` should update only that package's `package.toml`.
- `add` should not modify tracked package state.
- `add` should not run `push`, `pull`, hooks, or any repo-to-live or live-to-repo execution.
- `add` should not copy the live file or directory into the repo; this phase is manifest-only.
- `add` should derive a deterministic target key from the live destination path.
- Generated target keys should use `f_` for file targets and `d_` for directory targets.
- The rest of the generated key should be lowercase path-derived snake_case text with punctuation normalized to underscores.
- If the generated target key already exists in the same package, `add` should append a numeric suffix such as `_2`, `_3`, and so on until the key is unique.
- If the same live `path` is already declared by another target in the same package, `add` should fail instead of silently replacing it.
- `add` should store the target `path` as `~/<...>` when the live path is under `$HOME`.
- `add` should store the target `path` as an absolute path when the live path is outside `$HOME`.
- `add` should derive the repo `source` path by mirroring the live path under `files/`.
- For source derivation, dotman should drop the home directory prefix when the live path is under `$HOME`; otherwise it should drop only the leading `/`.
- For every mirrored source path component, dotman should remove any leading `.` character, including for paths outside `$HOME`.
- Examples:
  - `~/.gitconfig` -> `files/gitconfig`
  - `~/.config/nvim/init.lua` -> `files/config/nvim/init.lua`
  - `/etc/.ssh/ssh_config` -> `files/etc/ssh/ssh_config`
- If another target in the same package already uses the derived `source`, `add` should append a numeric suffix such as `_2`, `_3` to the source name, before any extension, until it is unique. For example, adding `~/zshrc` after `~/.zshrc` gives `files/zshrc_2`, and `files/config/app.toml` becomes `files/config/app_2.toml`.
- `add` should fail when the new target would nest with another target of the same package in a way push or pull rejects as `incompatible nested targets`: a live path inside (or containing) another target's live path, or a derived `source` inside (or containing) another target's `source` (dropped leading dots can cause this, e.g. `~/.config/foo` and `~/config/foo/bar`). It applies the engine's own rule, so the other target's ignore patterns, gitignore rules and sync policy decide: a nested path that the parent target's ignore rules cover is added. Only the package's declared targets are compared; paths that need a profile to render are not checked.
- `add` should inspect the actual live file mode and include `chmod` only when the mode is unusual.
- The usual default modes are:
  - regular file: `644`
  - directory: `755`
- If the live mode differs from the usual default for that target kind, `add` should write the actual mode into `chmod`, for example `600` or `700`.
- In interactive mode, `add` should open an editor review when an editor is configured (see [Edit](#edit)).
- That editor review should open a diff view of the old and generated `package.toml` content, and also open an editable temporary `package.toml` copy.
- `add` should not write the edited manifest back to the repo until the editor exits and the user confirms the write.
- The editor review for `add` should not open the live path itself.
- Examples:
  - `dotman add ~/.gitconfig git`
  - `dotman add .config/nvim/init.lua main:nvim`
  - `dotman add ~/.config/gtk-3.0/settings.ini desktop/gtk`
  - `dotman add ~/.config/foo.conf`
## Edit

- `edit`, `add` and `doctor` find the configured editor the way the default Sync Editor does (see [Projection and Editor configuration](repository.md#projection-and-editor-configuration)): the first of `VISUAL`, `EDITOR`, `GIT_EDITOR` and Git's `core.editor` that is set, otherwise the first of `sensible-editor`, `editor`, `nvim`, `vim`, `vi` and `nano` found on `PATH`. `edit`, `add` and Sync Editors run that value the same way: like Git, a value with shell syntax (any of ``|&;<>()$`*?[#~=%``) runs through `/bin/sh -c`, and any other value runs directly with its words split as the shell would. Files follow the editor's arguments, unless the value uses `$` and places them itself through `"$@"` or `$1`. No editor is configured only when every step comes up empty.
- `edit package <package>` should open the tracked package directory in the configured editor.
- `edit target <target>` should open the tracked target repo-side source path in the configured editor.
- `edit repo <repo>` should open the configured repo root in the configured editor; non-exact repo names should use the interactive repo resolver when available.
- `edit local [<repo>]` should open the per-repo local override path in the configured editor; missing, ambiguous, or non-exact `<repo>` should use the interactive repo resolver when available.
- `edit local <repo>` should open directly only for exact repo-name matches; non-interactive mode should reject non-exact matches.
- `edit local` should load the manager config to resolve repo names, but should not load or validate the local override file before opening it.
- `edit local` should create the local override parent directory before launching an editor.
- `edit config` should open the selected dotman manager config path in the configured editor.
- `edit config` should use `--config <config-path>` when provided, otherwise `$XDG_CONFIG_HOME/dotman/config.toml` (or `~/.config/dotman/config.toml`).
- `edit config` should not load or validate the config before opening it, so invalid config can be fixed with the command.
- `edit target` should resolve tracked targets only.
- `edit target` should open the repo-side source file for file targets and the repo-side source directory for directory targets.
- If no editor is configured, `edit package` should print the package directory path and exit successfully.
- If no editor is configured, `edit target` should print the resolved repo-side source path and exit successfully.
- If no editor is configured, `edit repo` should print the resolved repo root path and exit successfully.
- If no editor is configured, `edit local` should print the resolved local override path and exit successfully.
- If no editor is configured, `edit config` should print the selected manager config path and exit successfully.
- `edit package` should use the tracked-package selector flow, so bare and repo-qualified package queries follow the same ambiguity rules as `info tracked`.
- `edit target` should accept explicit target queries in the form `[<repo>:]<package>.<target>`.
- `edit target` may also accept bare target-name queries when they resolve uniquely among tracked targets.
- `edit target` should treat target identity as package-scoped, so ambiguous target-name queries must prompt interactively and fail in non-interactive or JSON mode.
- Bare `dotman edit <query>` is a convenience form that resolves across tracked packages and targets. Ambiguous or partial matches use the same interactive selector flow and fail without guessing in non-interactive or JSON mode.

## Untrack

- `untrack <selector[@profile]>` should remove one persisted explicit package entry from state.
- `untrack` should be state-only.
- `untrack` should not delete live files, run hooks, or build push/pull execution plans; ownership validation should use rendered target metadata only.
- `untrack` should match persisted explicit package entries, not current repo manifests, so stale entries can still be removed after repo changes. `untrack <group>@<profile>` accepts a profile the repo no longer defines when the group's persisted rows still use it.
- `untrack` should accept either `selector` or `selector@profile`.
- If the profile is omitted, dotman should untrack the unique exact explicit package entry that matches the selector.
- Omitting the profile for a tracked `multi_instance` package selector with multiple bound profiles should be an ambiguity error.
- A single fuzzy/partial match must not execute or mutate state silently.
- In interactive mode, a single fuzzy/partial match should require explicit confirmation before dotman continues.
- In non-interactive or JSON mode, a single fuzzy/partial match should fail without guessing.
- Tracked package matches, including implicit packages, should participate in fuzzy-match ambiguity detection so `untrack` does not treat a destructive partial match as uniquely safe.
- Exact invalid or orphan explicit package entries should still be removable by matching the persisted state record.
- If the selector only names a package that is present through another explicit package entry, dotman should explain which tracked package entries currently include it instead of just saying "not tracked".
- `untrack` should validate the resulting tracked package set before writing state.
- If removing one explicit package entry would expose a tracked-target conflict among different remaining package instances, `untrack` should fail and keep state unchanged.
- Singleton package targets reached through multiple tracked roots are the same package instance and should dedupe rather than conflict.
- Repo qualification may still be omitted when the tracked package entry is unique across configured repos.
- Examples:
  - `dotman untrack main:git@default`
  - `dotman untrack git`

## Tracked Package State

- Snapshot history is separate from tracked package state.
- `list repo` should list configured repos in configured order.
- Human `list repo` output should show repo name, order, and repo path; JSON output should also include config/state metadata.
- `list snapshots` and `restore` should operate from snapshot manifests under the snapshot storage root, not from tracked package state.

- `list tracked` should report the packages currently tracked by persisted explicit package entries.
- `list tracked` should resolve the current tracked package state against the current repo manifests.
- `list tracked` should not guess from the live filesystem.
- `list tracked` should not run push/pull planning or execute render/capture commands.
- `list tracked` should include package-level `state` as `explicit` or `implicit`.
- `list tracked` should include invalid explicit package entries for configured repos when persisted state no longer resolves cleanly.
- A persisted explicit package entry is invalid when its profile no longer exists, its selector is not a package (missing, or a group), its package dependencies do not resolve (reason `dependency_resolution_failed`, naming the selector that does not resolve), or a package it reaches `extends` a parent the repo does not define (reason `extends_parent_missing`, naming the package and the parent). `list tracked`, `doctor`, `untrack`, `track`, and push/pull/sync planning all use this one rule: an invalid entry is reported by `list tracked` and `doctor`, kept in state until untracked, and skipped by planning and tracked-target conflict checks instead of failing them.
- A tracked package whose manifest is malformed (for example an `extends` cycle) is a repo error, not a stale entry: `list tracked` and `doctor` report it as invalid with the error, while planning and `track`/`untrack` fail with that error.
- Every repo command except `doctor` and `list tracked` (which list every invalid entry themselves) prints one warning per entry it skipped to stderr, once per command, after it finishes (JSON stdout is unaffected): `warning: skipped stale tracked row <repo>:<package>@<profile> (<reason>); see 'dotman list tracked', or remove it with: dotman untrack <repo>:<package>@<profile>`. The untrack command ends the line and its selector is shell-quoted, so it can be pasted as is; a profile with an unprintable character appears escaped in it. Once planning ran, human push, pull and sync output shows the row as a skipped entry instead, where it shows Guard and no-Base skips (above the timeline, also after a confirmed Deck, or before the summary line in a preview or `--report`): `[skipped] <repo>:<package>@<profile> (<reason>)` followed by an indented `stale tracked row; remove it with: dotman untrack <repo>:<package>@<profile>` line, and does not repeat it as a warning. A row the command itself removed or replaced (untracking a group, or tracking the package under another profile) is not warned about: only rows still in tracked state when the command ends are. A command that rejects an invalid entry because the user named it (`push other:child`, `track other:child@default`, `info tracked other:child`, `edit package other:child`, `edit target other:child.conf`, `info sync-base other:child.conf`) fails with one error that names the row, its reason (for a missing `extends` parent, the parent too) and the untrack command, `<subject> matches stale tracked row <repo>:<package>@<profile>: <reason>; remove it with: dotman untrack <repo>:<package>@<profile>`, and does not also warn about it.
- While an invalid entry is skipped, tracked-target conflicts against it are not checked; the warning makes that visible.
- `track` checks the binding it is given before it reads tracked state: an unknown profile or selector, or a group member, dependency or `extends` parent that does not resolve, fails as a usage error naming it (for a group, also the member package that cannot be tracked). Such a binding is never reported as a skipped stale row, since it was never tracked.
- `list tracked` should include orphan explicit package entries discovered under the manager state root when a persisted `state_key` no longer maps to configured repos.
- Human `list tracked` output should print one flat `repo:package-or-selector state` list.
- Human `list tracked` output should list explicit and implicit packages first, then orphan entries, then invalid entries.
- `dotman list tracked` should list tracked package identities, not collapse `multi_instance` package instances by package definition.
- `singleton` packages should be listed once by package ID.
- `multi_instance` packages should be listed once per bound instance using `package<bound-profile>`.
- `dotman list vars` should list only the winning variable occurrence per repo.
- Human `list vars` output should keep the selector+profile context visible, for example `name (repo:selector@profile)`.
- `dotman info var <var>` should show every resolved occurrence of that variable key and its provenance.
- Variable provenance should identify the winning source layer for the resolved value, such as a package, profile, or repo-local override.
- `dotman info tracked <package>` should show detailed information for one currently tracked package identity.
- `dotman info tracked package<bound-profile>` should address one tracked `multi_instance` package instance.
- Package detail should include the owning repo, description, provenance entries with explicit or implicit reasons, owned targets after tracked-target winner resolution, the bound profile for `multi_instance` instances, and the effective hook commands for that package instance, even when the current push plan would be all-noop.
- Human-readable `info tracked` hook output should stay package-centric. Do not repeat a selector+profile provenance header under `::hooks`; the package instance already implies the single bound profile/effective hook-bearing source entry.
- Package lookup for `info tracked` may use the same repo-qualified and partial-selector rules as other package-oriented commands, but it should search only tracked packages.
- When tracked package lookup is ambiguous in interactive mode, `info tracked` should use the same shared selector menu as the other package-oriented commands.
- When a `multi_instance` package name matches multiple tracked instances, `info tracked` should require or select a specific `package<bound-profile>` instance instead of silently collapsing them.
- When tracked package entries resolve the same target path, explicit provenance should override implicit provenance.
- Conflicting explicit candidates should fail, and conflicting implicit-only candidates should also fail.
- In interactive `track`, dotman may offer a non-conflicting profile switch before failing.
- In interactive `track`, an implicit-only conflict may also offer to promote one of the conflicting packages into an explicit package entry. It lists each candidate as `repo:package@profile`, the repo and profile it is tracked under, and records the chosen package there. The promotion is recorded alongside the requested selector, not instead of it. Every conflicting package is a candidate, whether the request brings it in or not. Only promotions that leave no explicit clash at any target path are offered. When the request itself clashes explicitly, no promotion is offered, and the error names that clash rather than an implicit tie. One confirmation lists every implicit owner the request as a whole overrides. Declining a confirmation after a promotion is held records nothing, unless the request then switches profile, which drops held promotions.

## State

The CLI model implies persistent state.

Dotman should persist explicit package entries only.

- persisted explicit package entries only; group selectors must already be expanded
- no persisted target ownership yet
- no persisted resolved package graph yet

Tracked package state now uses `tracked-packages.toml` and `[[packages]]`, and each row represents one explicit tracked package entry. Repeated identical rows count as one entry, so `untrack` removes them all.

For example:

```toml
schema_version = 1

[[packages]]
repo = "main"
package_id = "desktop/niri"
profile = "basic"

[[packages]]
repo = "test"
package_id = "git"
profile = "personal"
```

- `push` with no selector should recompute the current package set from persisted explicit package entries and the current repo state.
- `pull` with no selector should also recompute from persisted explicit package entries and the current repo state.
- `track` and `untrack` should update only the persisted explicit package-entry set.
- Target ownership and prune-oriented state can be added later when prune behavior is introduced.

## Host Entry Points

- Host-level entrypoints should use host meta packages for convenience.
- Groups still handle reusable composition beneath the host meta layer.

## Home Path Rewrites

`dotman rewrite home expand [--bare-tilde] [INPUT|-]` replaces standalone `~/...` fragments with the active `$HOME`. `dotman rewrite home collapse [--bare-tilde] [INPUT|-]` replaces descendants of the active home path with `~/...`.

By default a bare `~` and the exact home path stay unchanged, because a bare `~` is not a home path in every format: in YAML it means null. `--bare-tilde` also expands a standalone bare `~` and collapses the exact home path to `~`; use it only for files where a bare `~` means home, and pass it to both commands of a `render`/`capture` pair so the round trip stays stable.

The optional input is a UTF-8 file. Omit it or use `-` to read stdin. Rewritten content is written only to stdout; the command has no output operand, in-place mode, or `--stdout` flag. Dotman reads and strictly decodes the complete input before emitting anything, so invalid UTF-8 and input errors cannot produce partial output. Unmatched bytes, including a UTF-8 BOM, line endings, whitespace, final-newline state, ordering, and Unicode normalization, remain unchanged.

`$HOME` is the only home source. It must contain a non-root absolute POSIX path; trailing slashes are ignored. Matching is lexical and does not inspect the filesystem, resolve symlinks, normalize path components, or expand other home shorthands. Attached names, URLs, escaped fragments, `~user`, `~+`, `~-`, `$HOME`, and `${HOME}` remain unchanged.

Home Path Rewrites run without repository discovery, configuration, profile resolution, or sync-engine construction.

```console
dotman rewrite home collapse settings.conf
cat portable.conf | dotman rewrite home expand > settings.conf
dotman rewrite home collapse --bare-tilde kitty.conf > portable-kitty.conf
dotman rewrite home expand --bare-tilde portable-kitty.conf > kitty.conf
```

## Structured JSON transforms

`dotman transform json BASE [OUTPUT] --mode cleanup|merge` runs without a dotman repository or configuration.
`--merge-file` is accepted as an alias for `--overlay-file` for every structured transform.
Every structured transform except XML accepts `not:` in front of a selector, such as `not:settings.cache` or `'not:re:(^|\.)cache$'`. It removes the matched base paths and their subtrees from the selection, whatever the selector order; a selected path that contains an excluded path is split into its children in the base. With only `not:` selectors, the rest of the document is selected.
To sync only part of a settings file with a `render`/`capture` pair, see [partial structured files](repository.md#partial-structured-files).
Selectors always match the base JSON object. Unprefixed selectors default to `exact:`. Exact selectors use dotted nested key paths; quote a segment when its key contains a dot, such as `"key.with.dots".value`, and use `""` for an empty key; an unquoted empty segment such as `a..b` is an error. `re:` selectors use Python regex search against complete dotted key paths. `--selector-type retain|remove` controls selection. Remove also drops an object it leaves empty, in cleanup and merge alike, while an object that was already empty stays. No selectors means identity cleanup, or whole-base overlay merge; selectors that match nothing select nothing. Merge descends into an object present in both the base and the overlay, whether or not selection kept any of it, so base keys left after selection survive and keys keep base order; an object selected as a whole is replaced by the overlay value, and with no selectors so is each top-level key the overlay shares with the base. Merge mode requires `--overlay-file`; cleanup mode rejects it. `--compare-file` reuses exact existing bytes, including line endings, when JSON values are semantically equal with matching types, so `true`, `1`, and `1.0` never reuse each other. A number beyond float range, such as `1e400`, is an error rather than being written as `Infinity`.

Use `-` for base or overlay stdin, and for output stdout. At most one input may be `-`; `--compare-file` must be a file. A missing base or `--overlay-file` counts as empty (for example, a live file before the first push). Inputs must be UTF-8; a leading byte order mark is accepted and not written back. `--stdout` takes precedence over an optional output operand. File output inherits base permissions before it becomes visible, and a symlinked output updates its target; stdin base has no permissions to inherit.

The selector, identity, and type-strict compare rules above apply to the JSON, YAML, and plist transforms alike.

Examples:

```console
dotman transform json settings.json cleaned.json --mode cleanup --selector-type retain --selectors exact:editor re:'^terminal'
dotman transform json live.json --mode merge --overlay-file managed.json --selectors settings --stdout
cat settings.json | dotman transform json - - --mode cleanup --selectors editor
```

### `dotman transform toml`

Transforms TOML documents without repository configuration. At least one selector is required. Unprefixed and `exact:` selectors match exact dotted TOML key paths; a double-quoted segment is a TOML basic string, so it may contain dots, spaces, or escapes (`x."a.b"`, `"a\tb"`), a single-quoted segment is a TOML literal string without escapes (`'C:\dir'`), and `""` names the empty key; an unquoted empty segment such as `a..b` is an error. `re:` selectors use Python regex search against dotted table and key paths; a table match selects its whole subtree, while a key match selects only that key. Selectors reach keys inside inline tables and tables split across the file, including dotted keys inside an inline table (`x = {a.b = 1}`); an inline table that selection or merge rebuilds prints such a key nested (`x = {a = {b = 1}}`). Retain keeps selected content in document order. Remove also drops a table it leaves empty, in cleanup and merge alike, while one that was already empty stays. A comment block directly above a key or table header (no blank line between) belongs to that item and is retained, removed, and merged with it; a block directly after a table's content belongs to that table; any other block stays in place. Runs of blank lines (including lines holding only spaces or tabs) collapse to one, and output neither starts nor ends with blank lines.

Merge recursively overlays tables and inline tables present in both the base and the overlay after partitioning the base, whether or not selection kept any of it, keeping base key order and the overlay's inline or table style; a table selected as a whole is replaced by the overlay's copy; a table split across the file comes out as one table. Scalar values, arrays, and arrays of tables are atomic values and are replaced as units. Each item brings its comments from the side that supplied it, and a table on both sides takes its header comments from the overlay. Ordering, whitespace trivia, and formatting are retained where `tomlkit` can preserve them. Output uses the base file's line ending (the overlay's when the base has none), and non-empty output ends with a line break. Multiline strings are read with LF line breaks, as TOML readers do. `--compare-file PATH` reuses its exact text when parsed TOML values have equal types and values (so `true`, `1`, and `1.0` differ, as do datetimes with different offsets) and comment attachments are equal.

```sh
dotman transform toml live.toml output.toml --mode merge \
  --overlay-file repo.toml --selector-type retain \
  --selectors model 're:^projects\.' --compare-file live.toml
```

Both operands accept `-` for stdin (at most one). Output path `-` and `--stdout` write to stdout. File output inherits base-file permissions.

### `dotman transform plist`

Transforms plist dictionaries without repository configuration. It uses shared transform operands and flags documented above, plus `--output-format xml|binary` (default `xml`) and optional `--compare-file PATH` for semantic no-op raw-byte reuse.

Selectors are exact dotted dictionary paths by default; quote a component containing a literal dot (`"settings.window".width`). `re:` selectors use Python regex search against complete dotted paths. Matching a dictionary selects its subtree; arrays remain atomic. Remove also drops a dictionary it leaves empty, in cleanup and merge alike, while one that was already empty stays. No selectors means identity cleanup, or whole-base overlay merge. Merge descends into a dictionary present in both the base and the overlay, whether or not selection kept any of it, so base keys left after selection survive; a dictionary selected as a whole is replaced by the overlay value, and with no selectors so is each top-level key the overlay shares with the base.

```sh
dotman transform plist live.plist output.plist --mode merge \
  --overlay-file repo.plist --selector-type remove \
  --selectors settings.managed --output-format binary
```

Both operands accept `-` for stdin (at most one). Output path `-` and `--stdout` write XML or binary bytes safely to stdout. File output inherits base-file permissions.

### `dotman transform xml`

Transforms XML trees without repository configuration. At least one selector is required. Unprefixed and `exact:` selectors are `fnmatch`-style, root-inclusive element paths; `re:` selectors use Python regex search against those paths. A path names each element as the document writes it: `config/a` under a default namespace (`<config xmlns="urn:x">`), and `config/x:a` for an element written `<x:a>`. Retain keeps matched subtrees and ancestor chains. Remove deletes matched subtrees. Comments, processing instructions, the doctype, namespace prefixes, and namespace declarations are kept; entity references stay unexpanded, and no external entity or DTD is loaded. Comments and processing instructions never match selectors or pair in merge; those directly before an element (only whitespace between) belong to it and are retained, removed, sorted, and merged with it.

Merge matches repeated siblings by tag and available `id`, `name`, `key`, `uuid`, or non-empty text identity; siblings never match when an `id`, `name`, `key`, or `uuid` both carry differs, or when both carry such attributes but none in common; an attribute only one side carries does not prevent a match. It then applies overlay attributes, text, children, and deletions. Merged children follow overlay order, with the overlay's comments and namespace declarations; a base-only child kept by the selectors stays after the sibling it followed in the base. `--sort-attributes` sorts emitted attributes. Repeated or comma-separated `--sort-children PATH` flags sort immediate children only under matching parents.

`--compare-file PATH` ignores the indentation of element-only content, attribute order, and the order of explicitly selected child lists for semantic comparison; leaf and mixed-content text, including whitespace-only values such as `<sep> </sep>`, must match, as must comments, processing instructions, and the doctype. Equal XML reuses compare file's exact bytes. This compare normalization does not otherwise canonicalize emitted XML. Emitted XML starts with `<?xml version="1.0" ?>`, writes empty elements as `<a/>`, indents element-only content by two spaces, and leaves text and mixed content unchanged.

```sh
dotman transform xml live.xml output.xml --mode merge \
  --overlay-file repo.xml --selector-type retain \
  --selectors 'config/WindowGeometry' \
  --sort-children 'config/mutedDictionaries'
```

Base or overlay accepts `-` for stdin (at most one). Output `-` or `--stdout` writes stdout. File output inherits base-file permissions.

### `dotman transform yaml`

Transforms YAML mappings without repository configuration. Selectors are optional: with no selectors, cleanup is an identity rewrite and merge overlays the whole base. Unprefixed and `exact:` selectors match exact dotted nested YAML mapping key paths; quote a segment containing a literal dot (`"settings.window".width`). `re:` selectors use Python regex search against complete dotted key paths. Matching a mapping selects its subtree; scalars and lists remain atomic. Remove also drops a mapping it leaves empty, in cleanup and merge alike, while one that was already empty stays.

Merge partitions the base by selector, then applies the overlay on top. Merge descends into a mapping present in both the base and the overlay, whether or not selection kept any of it, so base keys left after selection survive and keys keep base order; a mapping selected as a whole is replaced by the overlay value, and with no selectors so is each top-level key the overlay shares with the base. Scalars and lists are atomic: the overlay value replaces the base value whole. `--compare-file PATH` reuses its exact text when parsed YAML values are semantically equal, including line endings. An empty or comment-only document is an empty mapping; a duplicate key or a second document is an error. Otherwise output edits the base document in place (the overlay document when merge has no base) in `ruamel.yaml` round-trip mode, so whatever the transform leaves alone keeps its comments, quoting, flow or block style, custom tags such as `!Ref`, anchors, aliases, and `<<` merge keys. Edits keep the output document's comments: a changed value keeps the comments beside its key, and a key that the overlay adds brings only the comments inside its value. Deleting a key takes its end-of-line comment and the comment lines between it and the previous key, and leaves its neighbours' comments in place; comment lines above a mapping's first key stay, since they may describe the whole mapping or file. An edit to an anchored or `<<`-merged mapping stays in place when every alias and merge of it should change with it; otherwise the edit goes to a copy and the other places keep their value. Deleting a key a `<<` merge also supplies writes the merged keys into that mapping and drops its `<<`; a changed merged key becomes the mapping's own key, which overrides the merge. Values taken from the overlay drop their anchor names, so they cannot redirect the base document's aliases. Before writing, the transform re-reads its output and fails rather than write YAML that does not hold the transformed data. Output takes its indentation from the compare file, else the base, else the overlay: the mapping indent and the sequence indent and dash offset of the first nested block mapping and sequence, defaulting to two spaces with dashes at the key's column.

Values follow YAML 1.2: `yes`, `on`, `y`, and `22:22` are strings, `0777` is 777, `0x1F` is 31, and `1e3` is a float. A document with a `%YAML 1.1` directive is read as YAML 1.1, but output drops the directive and spells values the YAML 1.2 way. A value the transform leaves alone keeps its spelling (`0777`, `0x1F`, `1_000`, `'on'`), and compare reuse compares values, so plain `0777` and quoted `'0777'` differ. Re-emitting normalizes a few spellings: null as `null` (for `~`, `Null`, or an empty value), `TRUE` as `true`, `+12` as `12`, and timestamps in ISO form. Selectors match string keys as spelled in the file and other keys by their written spelling (`true`, `null`, `0777`, `0x1F`).

```sh
dotman transform yaml live.yaml output.yaml --mode merge \
  --overlay-file repo.yaml --selector-type retain \
  --selectors settings 're:^terminal' --compare-file live.yaml
```

Both operands accept `-` for stdin (at most one). Output `-` or `--stdout` writes stdout. File output inherits base-file permissions.


### Sync directory topology

**Directory Root Work** uses the Command Deck's ordinary Selection control and
canonical target identity. It selects mode drift on an existing root, not an
aggregate directory Proposal. Structural approval diagnostics identify missing
deletion approvals; confirmation remains unavailable until every managed blocker
is approved. Unmanaged structural blockers are reported as conflicts.
