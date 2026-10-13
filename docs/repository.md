# dotman Repository Model

This reference owns repository syntax, defaults, inheritance, precedence, and
validation. See [manager configuration](config.md), [CLI](cli.md), and
[Sync lifecycle](sync.md) for their respective contracts. Runnable layouts live
under `examples/repo/`.

## Core Objects

- A package is the atomic install unit.
- Packages live under `packages/`.
- A package manifest lives at `packages/<package-id>/package.toml`.
- Namespaced package IDs map to nested directories under `packages/`, for example `work/git` -> `packages/work/git/package.toml`.
- Packages may define `binding_mode = "singleton" | "multi_instance"`.
- `binding_mode` defaults to `singleton`.
- `singleton` means the package has one tracked identity regardless of bound profile.
- `multi_instance` means the package definition may produce multiple independent package instances, keyed by bound profile.
- Human-facing target labels use `repo:package.target` and `repo:package<profile>.target`.
- `.` is reserved for the package/target separator and must not appear inside package IDs or target names.
- `/` is reserved for the target/child separator and must not appear inside target names. Package IDs may still use `/` for namespaces, and profile IDs may contain dots inside `<profile>`.
- Packages may declare `depends` for hard requirements.
- `depends` entries may reference either package IDs or group selectors.
- Group dependencies expand to their member packages during dependency resolution.
- Dependency profile context is inherited from the owner selection. A singleton dependency may not be pulled into tracked state under multiple implicit profile contexts.
- If two tracked roots would imply `repo:shared@basic` and `repo:shared@work` for singleton `repo:shared`, dotman fails before target planning. Resolve this by using one profile, making the dependency `multi_instance`, explicitly tracking the intended singleton dependency profile, or moving overlapping config into a shared package design.
- Dependency resolution de-duplicates revisits and truncates cycles, including mixed package/group cycles, so traversal stays finite.
- Packages that exist only for hard dependency aggregation should use a `-meta` suffix by convention.
- A package's target live paths are implicitly reserved.
- Packages may define `reserved_paths = [...]` for additional live paths that must stay exclusive to that package.
- A package directly under `packages/` is in the default namespace.
- Namespaced packages stay explicit, for example `work/git`.
- Groups live under `groups/` and are used for package selection and composition.
- Group IDs may be namespaced, for example `os/arch`.
- Groups are not packages and are not tracked identities.
- Tracking a group should behave like passing its resolved member packages as track arguments with the same bound profile.
- Meta packages are still normal packages; they may be tracked explicitly and may use `depends` for aggregation.
- Terminology should follow ArchWiki's distinction between meta packages and package groups: <https://wiki.archlinux.org/title/Meta_package_and_package_group>
- `/` is reserved for namespacing inside package and group IDs.
- Repo qualification stays outside the selector with `repo:selector`, not `repo/selector`.
- `\` is not a valid selector separator.
- Groups should use a single `members` list for both package selectors and nested group selectors.
- Profiles provide variable values only.
- Profiles may define `includes = [...]` to compose other profiles.
- Included profiles merge in declaration order.
- Later included profiles override earlier included profiles.
- The profile's own vars override all included profiles.
- Repos may define optional repo-wide defaults in `repo.toml`.
- Machine-local or private overrides should not live in the repo.
- Per-repo local overrides should be read from `$XDG_CONFIG_HOME/dotman/repos/<repo-name>/local.toml`, with fallback to `~/.config/dotman/repos/<repo-name>/local.toml`.
- Per-repo local overrides are limited to `[vars]` data.

## Resolution Model

- Package values act as defaults unless overridden.
- Variable resolution order is `package defaults -> composed profile -> local`.
- `local` means the per-repo XDG local override file, not a repo-root file.
- Local override merge rules should match the rest of resolution:
  - keyed maps use deep merge by key
  - scalars use last-wins replacement
  - lists replace the earlier value; they do not merge
- Each persisted explicit package entry stores the selected bound profile.
- Persisted tracked package state stores package selectors only, not group selectors.
- The composed/effective profile is runtime resolution context, not package identity.
- Packages with no file payload may still be useful as meta packages when they only declare `depends`.
- Any string value may contain template expressions and is rendered during resolution.
- Template vars are available both at top level and under `vars`, so `{{ greeting }}` and `{{ vars.greeting }}` resolve to the same value.
- A package may define `extends = [...]` to inherit from one or more parent packages before profile and local values are applied.
- Parent packages resolve in declaration order.
- The child package is applied last.
- The selected package is still resolved into one final package before `push` or `pull`.
- A package may define `sync_policy = "push-only" | "pull-only" | "both" | "push-only-delete"` to gate target participation by operation.
- `sync_policy` defaults to `both` when omitted.
- Target-level `sync_policy` overrides the package default for that target.
- `push-only-delete` participates in Push and Sync, deleting eligible live files while retaining repository sources. Unified exclusions protect directory children.
- Package inheritance should merge `sync_policy` with last-wins behavior, just like other scalar fields.
- Target and reserved-path collision rules apply across all resolved package instances, including instances that come from the same `multi_instance` package definition.
- Keep target reuse explicit by splitting shared logic into smaller packages or using normal `depends`; package dependencies stay package/group-only.

## Package Identity Modes (`binding_mode`)

- `binding_mode` controls package identity semantics, not file rendering semantics.
- File naming conventions such as `.tmpl` should not change package identity behavior.
- A `singleton` package is directly trackable as one package identity.
- A `multi_instance` package definition is not itself a tracked package identity.
- Tracking a `multi_instance` package always produces a package instance bound to one selected profile.
- A `multi_instance` package instance is identified by package ID plus bound profile.
- Effective/composed profile data may be shown for resolution context, but it is not part of package instance identity.
- A dependency on a `multi_instance` package inherits the current bound profile and becomes a distinct tracked identity for each inherited profile.
- `multi_instance` allows multiple instances from the same package definition to coexist as identities.
- Coexisting identities do not bypass normal target ownership or reserved-path conflict checks.

## Package Inheritance

- `extends` is for package reuse, not runtime package selection or profile selection.
- `extends` should accept a list of package IDs.
- Resolution order is:
  - first parent
  - later parents in declaration order
  - child package
- Scalars use last-wins replacement.
- Keyed maps use deep merge by key.
- Lists replace the earlier value; they do not merge.
- Packages may define `remove = [...]` with dotted paths to delete inherited fields or keyed entries explicitly.
- Packages may define `append` to append to inherited list-valued fields without replacing the whole list.
- `append` should mirror the object shape it targets, for example:
  - `[append.hooks]`
  - `pre_push = ["{{ INSTALL }} extra-tool"]`
- `append` should fail if the targeted inherited field is not a list.
- Targets and hooks should stay keyed so merges remain deterministic.
- Conflicting target ownership or incompatible target/path collisions should fail hard.
- Reserved path collisions should also fail hard when one package reserves a live path used or reserved by another package.
- A child package may override inherited targets, hooks, vars, and metadata.
- Platform variants such as `linux/1password` are a primary use case for `extends`.
- `extends` is preferable to cross-package relative `source` references when a variant wants to inherit most of a base package.

Example:

```toml
id = "linux/1password"
extends = ["1password"]

[targets.quickaccess_desktop]
source = "files/local/share/applications/1password-quickaccess.desktop"
path = "~/.local/share/applications/1password-quickaccess.desktop"
chmod = "600"
```

## Targets

- Targets may define `path` for the live destination.
- Target keys are arbitrary manifest identifiers.
- Tools may still generate deterministic path-derived target keys for convenience, but that naming is a convention rather than a schema rule.
- `path` may use `~/...` for home-relative destinations or an absolute path otherwise.
- Targets may define `type = "file" | "directory"` when filesystem inference is not enough, such as when both source and live paths are absent. An explicit type establishes the endpoint kind, not permission to Render a missing file source.
- Targets may define `chmod` when the installed root path needs an explicit mode.
- `chmod` is optional and should usually be omitted unless the target needs a non-default live mode.
- For file targets, `chmod` is the source of truth for the installed file mode when present.
- For directory targets, `chmod` applies to the live directory root only; child files mirror Git semantics and carry only the executable bit, not full permission bits such as `600` vs `644`.
- Directory targets may define target-level `render` and `capture`; for directory targets these apply as defaults for every child file.
- Sync discovers directory children through one symmetric control-aware census, then resolves named Path Rules even for repository-only or live-only children. Each child has canonical identity `repo:package.target/<relative/child>`. Controls, ignored paths and directory nodes are not Sync Units; see [Sync lifecycle](sync.md#directory-census-and-child-capability) for the current Observation and convergence boundary.
- Directory targets may define named `[targets.<name>.path_rules.<rule>]` tables for path-scoped child policy. Rule names contain only letters, numbers, `_`, or `-`; `priority` orders matches and rule names provide the lexical tie-break. Path rules support `pattern`, `preset`, `priority`, `chmod`, `render`, `capture`, `compare.repo`, `compare.live`, `editor`, `sync_policy`, and a guard-only `hooks` namespace.
- Path-rule `pattern` values are relative glob-style patterns under the directory target root. They must not be absolute or contain `..` segments.
- Path-rule `preset` reuses built-in target presets as defaults for matching child files. Useful example: `preset = "jinja-patch"` applies Jinja render, patch capture, and the required patch-review views for that path rule.
- Path-rule `render`, `capture`, `compare`, `editor`, and `sync_policy` override target defaults. Priority defaults to `0`; evaluate lower to higher priority, then lexical rule name. Higher-priority non-empty fields win independently. Names are unique in the effective target, contain neither `.` nor `/`, and annotate diagnostics rather than identity. Package inheritance merges rules by name; Guards use the same order.
- Child Pull Views resolve from the effective inherited `compare` pair; `compare.live` defaults to `capture`, whose default provider is `raw`.
- `capture = "patch"` is allowed for directory child files when the effective child settings satisfy the same file-like requirements: effective non-raw `render`, `compare.repo = "render"`, and `compare.live = "raw"`.
- Path-rule `chmod` values apply during Push and Sync live publication only. `pull` still stores only bytes plus the Git executable bit because Git cannot represent full child file modes such as `600`.
- If multiple path rules match the same child file, fields compose independently in ascending priority and lexical rule-name order; an explicitly set field from a later rule wins without resetting other fields. Matching guards run in that same deterministic order and must all pass.
- A Path Rule Guard becomes active when its pattern matches managed repository-side or live-side candidates after unified exclusions and control-file filtering.
- Each active Path Rule Guard runs once per directional family, not once per child. Exit `100` narrows matching children's capability; it never widens configured policy.
- Path-rule guard environments retain target-root `DOTMAN_REPO_PATH` / `DOTMAN_LIVE_PATH` values and add `DOTMAN_PATH_RULE_PATTERN`. They do not expose one child path.

Example:

```toml
[targets.config]
source = "files/config"
path = "~/.config/app"

[targets.config.path_rules.secrets]
pattern = "secrets/*.conf"
chmod = "600"

[targets.config.path_rules.secrets.hooks]
guard_push = "test -r /run/credentials/app || exit 100"

[targets.config.path_rules.data]
pattern = "*/data.json"
render = "json-render-command"
capture = "json-capture-command"

[targets.config.path_rules.templates]
pattern = "templates/*.conf"
preset = "jinja-patch"
```

- Targets may define `sync_policy` to narrow or widen the package-level operation gate for that target.
- Use `push-only` for forward-managed targets, `pull-only` for reverse-only targets, `both` for targets that can participate in both operations, and `push-only-delete` for targets whose live file should be removed on Push or Sync while the repo source is retained.
- Targets may define `probe` as a side-effect-free planning command instead of file payload fields.
- A probe target does not define `source`, `path`, `type`, `chmod`, `render`, `capture`, `editor`, pull views, ignore rules, or path rules.
- Probe exit codes are:
  - `0`: active; the target appears in normal selection and makes package/target hooks eligible.
  - `100`: inactive/noop; the target stays out of normal selection and hooks run only if explicitly noop-eligible.
  - any other non-zero status: hard planning failure.
- Planning runs probes concurrently, up to eight at a time (fewer on machines with fewer cores), so a probe must not depend on another probe or share scratch files with it.
- Probe targets do not claim repo/live paths, do not participate in target ownership conflicts, do not create snapshots, and never execute file push/pull steps.
- Use `sync_policy = "push-only"` for install/update probes that should run only before push-style setup.
- Probes reject effective `push-only-delete`, including inherited package policy:
  there is no live endpoint to delete.
- Sync exposes active Probes as directly selectable auxiliary work only when a
  capability survives Guards. They activate only surviving directional hook
  families and never have file Proposals, Base acknowledgment or Converged results.
- Targets may set `on_demand = true` (boolean, default `false`) for expensive or
  update-only work that should run only when asked. An on-demand target enters
  Push, Pull or Sync scope only through an exact target selector
  (`repo:package.target`); package selectors and selector-less runs leave it
  out silently.
  It applies to file, directory and probe targets, and `sync_policy` still gates
  it by operation. An overriding package may set it but cannot
  clear an inherited `true`.
  Ownership and collision checks still cover it. See
  [Sync scope resolution](cli.md#sync-scope-resolution).

```toml
[targets.upstream_update]
probe = 'bash "$DOTMAN_PACKAGE_ROOT/scripts/needs_update.sh" --check-upstream'
sync_policy = "push-only"
on_demand = true
```

- Targets may set `disabled = true` (boolean, default `false`) to switch a
  target off without deleting its declaration. dotman then treats it as
  undeclared: it is never in Push, Pull or Sync scope, an exact target selector
  naming it matches nothing, `info tracked` and `info trackable` omit it, and it
  claims no repo or live path. An overriding package may set it but cannot clear
  an inherited `true`. Use `on_demand` instead when the target should still run
  when named.

## Projection and Editor configuration

Render, Capture, and Editor are flat inherited target and named Path Rule fields.
Comparison uses the paired `compare.repo` and `compare.live` fields. Resolution
Intent is a session choice, not manifest configuration.

Render turns the repository file into the content written live by Push and
Sync. Capture turns the live file into the content written to the repository
by Pull and Sync. Both default to `raw`, a byte copy. By default, comparison
checks the repository file against Capture of the live file.

| Field | Default | Built-in values |
| --- | --- | --- |
| `render` | `raw` | `raw`, `jinja` |
| `capture` | `raw` | `raw`, `patch` |
| `editor` | `default` | `default`, `jinja` |
| `compare.repo` | `raw` | `raw`, `render` |
| `compare.live` | `capture` | `raw`, `capture` |

Other scalar strings denote custom commands. For Render, Capture, or comparison,
a table accepts only `run`; for example `render = { run = "jinja" }` forces
command interpretation. Explicit `raw` cancels inherited Render or Capture.
See [custom Render and Capture commands](#custom-render-and-capture-commands)
for the command contract.

The comparison pair produces repository and live Pull Views during Pull and
live-to-repository-capable Sync. When that capability does not survive Guards,
comparison remains validated but inactive without warning.
Patch Capture requires effective non-raw Render, `compare.repo = "render"`,
and `compare.live = "raw"`. Invalid combinations fail validation; defaults are
not silently rewritten.

An Editor is a scalar provider or a table with exactly one of `type` or `run`.
Built-in `type` values are `default` and `jinja`; other scalar strings are
custom `run` shorthand. Tables may also set `io`, `elevation`, and
`additional_sources`. Custom Editors default to `io = "tty"`, may choose
`pipe`, and inherit default command elevation unless overridden. Built-in
Editors never run elevated.

Editor provider inheritance is atomic; its Additional Source list inherits
independently. Lists replace unless the explicit append mechanism is used;
de-duplication preserves first occurrence. Configured Additional Sources resolve
relative to their declaring package and cannot escape it or replace a Primary
Source. Staging order is Primary Source, configured Additional Sources, then
statically discovered Jinja dependencies. A custom Editor receives all staged
paths as shell positional parameters through `"$@"`, plus scalar environment
values `DOTMAN_EDITOR_PRIMARY_PATH` and `DOTMAN_EDITOR_TRANSACTIONAL_ROOT`
identifying the Primary path and transactional root. Only staged sources
are editable; review evidence is read-only.

The default Editor chooses the first available entry in this order:
`VISUAL`, `EDITOR`, `GIT_EDITOR`, Git's configured editor, `sensible-editor`,
`editor`, `nvim`, `vim`, `vi`, then `nano`. If none exists, the action
fails locally and preserves the prior Proposal. The Jinja Editor discovers static
dependencies before the same external-editor handoff; there is no embedded Editor.
Editor invocation is deliberate, never an automatic Capture-failure fallback.

The `jinja-editor`, `jinja-patch`, and `jinja-patch-editor` presets expand
into these flat fields. Explicit fields override preset values. See
[template targets](templates.md) for complete examples.

### Custom Render and Capture commands

A custom Render, Capture or comparison command is a shell command that prints
the projected file to stdout.

- Jinja in the command string expands with the package context first, so
  `{{ vars.app.key }}` works there. To template the file content itself, use
  `render = "jinja"` ([template targets](templates.md)).
- Commands read files, not stdin. `DOTMAN_REPO_PATH` names the repository file
  and `DOTMAN_LIVE_PATH` the live file. A command runs only when its own input
  exists (repository for Render, live for Capture); the other file may be
  missing, for example the live file before the first Push.
- Those paths may name private staged copies: a Sync Proposal, or a protected
  file Dotman read for the command.
- Commands also receive the target hook variables
  ([Hooks And Commands](#hooks-and-commands)) except `DOTMAN_NO_CONFIRM`, and
  run in the directory of the `package.toml` that declares the target.
- Commands are non-interactive and side-effect-free. Planning runs comparison
  projections concurrently, up to eight at a time (fewer on machines with fewer
  cores), so a projection must not depend on another target's projection or
  share scratch files with it.
- Dotman owns managed-path access, including privileged reads. Projections
  neither inherit default command elevation nor accept elevation configuration.
- Only exit `0` produces a valid result; all non-zero exits are failures.
- Capture of Render's output must reproduce the repository file. Otherwise,
  with the default comparison, the target never shows as in sync after a Push.

```toml
# The repository keeps portable `~` paths; the app needs absolute ones.
[targets.app_conf]
source = "files/app.conf"
path = "~/.config/app/app.conf"
render = 'dotman rewrite home expand "$DOTMAN_REPO_PATH"'
capture = 'dotman rewrite home collapse "$DOTMAN_LIVE_PATH"'
```

To sync only part of a settings file, see
[Partial structured files](#partial-structured-files).

## Partial structured files

A settings file that mixes portable keys with app-written state syncs only the
portable region through a `render`/`capture` pair of
[structured transforms](cli.md#structured-json-transforms). Both commands use
the live file as base, so live-only keys never enter the repository and are
never overwritten on the machine. The selector list names either the synced
keys (allowlist) or the live-only keys (denylist), and `--selector-type` flips
between the two commands:

| Strategy | Selectors name | `render` (`--mode merge`) | `capture` (`--mode cleanup`) |
| --- | --- | --- | --- |
| Allowlist | synced keys | `--selector-type remove` | `--selector-type retain` |
| Denylist | live-only keys | `--selector-type retain` | `--selector-type remove` |

- An allowlist keeps new upstream keys live-only; a denylist syncs them.
- Within the synced region the repository copy wins: a synced key absent from
  the repository is deleted live. For a denylist that includes every key the
  app added since the last Capture.
- A repository key outside the synced region is published once, then dropped
  by Capture, so the target never converges.
- Live-only keys survive Render even when the repository lacks the mapping
  that holds them, and Capture drops a mapping it leaves empty. Deleting an
  entry from the repository therefore sticks, while its live-only keys stay in
  the live file; an entry with no live-only keys leaves no empty mapping.
- To sync a subtree except some keys, add `not:` selectors, such as
  `settings 'not:settings.windowBounds'`, or `settings 'not:re:(^|\.)cache$'`
  for a key at any depth. The excluded live keys survive `render` because merge
  descends into every mapping it does not replace whole.

```toml
[vars.app]
# Everything else in app.json is window and session state.
synced_selectors = ["theme", "editor.fontSize", "keybindings"]

[targets.f_config_app_app_json]
source = "files/config/app/app.json"
path = "~/.config/app/app.json"
render = 'dotman transform json "$DOTMAN_LIVE_PATH" --stdout --mode merge --overlay-file "$DOTMAN_REPO_PATH" --compare-file "$DOTMAN_LIVE_PATH" --selector-type remove --selectors {{ vars.app.synced_selectors|shell_args }}'
capture = 'dotman transform json "$DOTMAN_LIVE_PATH" --stdout --mode cleanup --compare-file "$DOTMAN_REPO_PATH" --selector-type retain --selectors {{ vars.app.synced_selectors|shell_args }}'
```

- `--compare-file` reuses existing bytes for semantically equal content:
  Render compares against live, Capture against the repository.
- `{{ list|shell_args }}` passes each selector as one argument
  ([safe shell argument arrays](templates.md#safe-shell-argument-arrays)).
- For home paths inside values, Render pipes
  `dotman rewrite home expand "$DOTMAN_REPO_PATH"` into `--overlay-file -` and
  Capture pipes `dotman rewrite home collapse "$DOTMAN_LIVE_PATH"` into base
  `-` ([home path rewrites](cli.md#home-path-rewrites)). The rewrite requires
  UTF-8, so binary plists cannot use it.
- The default `compare` pair already fits; leave it unset.
- For child files of a directory target, put the same pair on a `path_rules`
  entry.

## Unified exclusions

| Scope | Accepted `ignore` keys |
| --- | --- |
| Repository | `gitignore`, `patterns`, `skip_markers` |
| Package | `gitignore`, `patterns` |
| Target | `patterns`, `command` |

`gitignore` is a boolean, disabled by default. Package enablement overrides the
repository default; targets cannot override it. When enabled, the normal Git
ignore chain runs from the repository root through nested control files under
target sources. Live control contents do not establish ignore policy.

Repository, package, and target patterns accumulate in that order, relative to
each target root. They use Git ignore syntax, including `**`, leading `/`,
trailing `/`, and `!` negation, with excluded-parent semantics.
Skip markers are repository-level basenames, such as `.dotman-skip`; finding
one on either side excludes that subtree on both sides. Marker contents do not
matter. Control files are never payloads and cannot be re-included by negation.

A target `command` computes extra exclusions from current state, such as a
package manager's lock file:

- It prints Git ignore lines relative to the target root and is appended after
  all static layers. Empty output adds nothing.
- Lines must not start with `!`, so a command can only narrow what syncs.
- It runs once per Push, Pull, or Sync session, when the directory target is
  scanned after target Guards admit it. It uses the same template rendering, working directory, and
  environment as `probe`, and must be side-effect-free and cheap. Other
  commands, such as `info`, and target collision checks never run it.
- A non-zero exit, non-UTF-8 output, or a negation line fails planning. To
  tolerate a missing input, handle it explicitly in the command.

All exclusions apply identically to Push, Pull, and Sync. Excluded paths are
neither observed, changed, nor acknowledged as Sync Bases. Directory writes and
stale-path deletion preserve excluded content.

- For directory-target child files, `push` and `pull` should plan and apply mode changes only when the executable bit differs. Non-executable permission drift such as `600` vs `644` should not trigger an update because Git does not preserve those bits in the repo.
- For directory-target child files matched by `path_rules` with `chmod`, `push` should also plan and apply exact live chmod drift for those matching paths.
- If `type` is omitted, dotman should infer the target kind from either side:
  - if the repo source root exists and is a directory, treat it as a directory target
  - if the repo source root is missing but the live path is a directory, still treat it as a directory target
  - if both repo and live paths are missing, treat the target as `unknown` and plan it as noop rather than guessing file vs directory
- If `type` is set, it overrides inference and plans the target as that kind.
- Explicit `type` still validates existing repo/live path shapes. Live symlink validation respects `file_symlink_mode` and `dir_symlink_mode`; directory symlinks are accepted only when `dir_symlink_mode = "follow"`.
- Empty directory targets may therefore remain absent from the repo on disk; git cannot store empty directories directly, so dotman should not create placeholder repo directories just to represent them.
- Source files can follow a default reverse-sync convention by mirroring the live path under `files/`.
- Template suffixes such as `.tmpl` are optional conventions, not the source of truth.

Example repo defaults:

```toml
[ignore]
patterns = ["*.bak", "*.dotdropbak"]
skip_markers = [".dotman-skip"]
```

With that marker config, `files/config/app/cache/.dotman-skip` makes dotman ignore all of `files/config/app/cache/`, including `state.db` or other siblings, during directory target scans.

Example computed exclusion for skills installed by a package manager:

```toml
[targets.agents]
source = "files/agents"
path = "~/.agents"

[targets.agents.ignore]
patterns = ["tools/"]
# Prints one "/skills/<name>/" line per lock entry.
command = "sh hooks/managed-skill-ignores.sh"
```

Example sync policy split:

```toml
id = "shell"
sync_policy = "push-only"

[targets.profile]
source = "files/profile"
path = "~/.profile"

[targets.history]
source = "files/history"
path = "~/.local/share/history.txt"
sync_policy = "pull-only"
```

Example probe target for install/update hooks:

```toml
[targets.nvim_version]
sync_policy = "push-only"
probe = "sh hooks/needs-nvim-update.sh"

[targets.nvim_version.hooks]
pre_push = "sh hooks/install-or-update-nvim.sh"
```

```sh
# hooks/needs-nvim-update.sh
# exit 0 means install/update is needed; exit 100 means already current.
installed="$(nvim --version 2>/dev/null | head -n 1 || true)"
wanted="NVIM vX.Y.Z"
[ "$installed" = "$wanted" ] && exit 100
exit 0
```

## Hooks And Commands

- Render, Capture, comparison, Probe and Editor command environments use
  `DOTMAN_OPERATION` for the invoking workflow: `push`, `pull` or `sync`.
  Guard and pre/post hook environments instead use their directional family,
  `push` or `pull`, including during Sync.

- Supported hook names are `guard_push`, `pre_push`, `post_push`, `guard_pull`, `pre_pull`, and `post_pull`.
- Guards decide eligibility once, during planning, and are never rerun after review. Put checks that must hold at execution time (network, mounts, credentials) in `pre_push`/`pre_pull`, which run immediately before the work and stop it on failure.
- Directory path-rule `[...path_rules.hooks]` tables support only `guard_push` and `guard_pull`; path-rule pre/post hooks are invalid.
- Hook entries may be a single item, an ordered list, or a table with `commands` and optional metadata.
- Repo, package, and target pre/post hook tables support `run_noop = true | false`.
- Pre/post hook command objects support `run_noop = true | false` to make only that command noop-eligible.
- Repo, package, and target guard hooks reject hook-level and command-level `run_noop`.
- `run_noop` defaults to `false`.
- Repo, package, and target hook shorthand still work and normalize to `run_noop = false`.
- Empty hook command lists are allowed and mean the hook is effectively disabled at that package layer.
- Hook command lists may mix plain strings and command objects in either shorthand or table form.
- Pre/post command objects use `{ run = "...", io = "pipe" | "tty", elevation = "none" | "root" | "intercept", run_noop = true | false }`.
- Repo, package, target, and path-rule guard command objects use `{ run = "...", io = "pipe", elevation = "none" | "root" | "intercept" }`.
- `run` is required for command objects and must not be empty after trimming.
- `io` defaults to `pipe`.
- `elevation` defaults to the repo-level `default_command_elevation`, or `none` when the repo default is omitted.
- `repo.toml` may set `default_command_elevation = "none" | "intercept"`. This applies to repo hooks, package hooks, target hooks, and custom editor commands that omit explicit `elevation`.
- `default_command_elevation` does not support `root`; set it on specific command objects instead.
- Explicit command metadata wins over the repo default. Use `elevation = "none"` to opt a command out of a repo default.
- `default_command_elevation = "intercept"` gives every command without explicit `elevation` the temporary `sudo` shim in `PATH`.
- Every mode authenticates the same way under `--no-confirm`: when the sudo ticket is missing, sudo authenticates however it is configured (NOPASSWD, a terminal prompt, or the `SUDO_ASKPASS` helper when no terminal is present).

Elevation modes:

| Mode | Behavior | Use for |
| --- | --- | --- |
| `none` | No dotman-managed elevation. | Normal commands. |
| `root` | Dotman requests sudo and runs the whole command through non-interactive sudo. | Root-only actions such as `systemctl restart ...`. Do not use for AUR helpers such as `yay`. |
| `intercept` | The command runs as the user with a temporary `sudo` shim first in `PATH`. When the command calls `sudo`, the shim asks dotman for the sudo lease, then runs real `sudo -n`. Dotman asks for a password only when a `sudo` call happens. | Commands that call `sudo` themselves, such as install scripts and AUR helpers. |

- `intercept` only sees `sudo` resolved through `PATH`. Call `sudo` by name, not `/usr/bin/sudo`.
- The shim always passes `-n`, so scripts that pipe a password into `sudo -S` do not work under `intercept`.
- To authenticate early, for example before a long build, call `sudo -v`; it goes through the shim like any other `sudo` call.

Example conditional package installer:

```toml
default_command_elevation = "intercept"

[hooks.pre_push]
commands = ["sh hooks/install-arch-packages.sh"]
```

```sh
[ -n "$missing_packages" ] || exit 0
# yay calls sudo itself; the shim asks for the password only now.
printf '%s\n' "$missing_packages" |
  xargs -r yay -S --needed --answerdiff=None --answeredit=None
```

Repo hooks live in `repo.toml` under top-level `[hooks]` and run once per repo per operation:

```toml
[hooks]
pre_push = "echo repo pre"

[hooks.guard_pull]
commands = ["echo repo guard pull"]
```

Target hooks live under `[targets.<name>.hooks]` inside a package manifest:

```toml
[targets.config]
source = "files/config.txt"
path = "~/.config/app/config.txt"

[targets.config.hooks]
pre_push = "echo target pre"

[targets.config.hooks.post_pull]
commands = ["echo target post pull"]
run_noop = true
```

Example:

```toml
[hooks]
pre_push = "echo hi"
post_pull = ["echo one", "echo two"]

[hooks.pre_pull]
commands = [
  "echo prep",
  { run = "nvim files/config.txt", io = "tty" },
  { run = "sh hooks/refresh-cache.sh", run_noop = true },
  "echo done",
]
```

- Hook and guard command lists run in declaration order and stop on first non-zero exit.
- Repo, package, and target `guard_*` hooks are non-interactive planning eligibility rules. They run after static ownership resolution and before host-state work for their scopes.
- Guard order is repository, package, then target, followed by active named Path Rule Guards. Each outcome applies within its declared scope; sibling scopes remain independently eligible.
- Target guards run before file projection, directory scanning, `ignore.command`, and probe commands. A directory target denied in every direction is not scanned.
- Exit `0` retains the Guard's directional capability; `100` removes it within
  that scope; any other non-zero exit aborts planning. One-sided Push and Pull
  omit work denied by their operation's Guard.
- Sync intersects surviving capabilities with configured `sync_policy`.
  A `both` unit with `guard_push` exiting `100` retains pull-only capability.
  A `push-only` unit with the same outcome has no route and remains a visible,
  non-approvable diagnostic. It is not a successful planning omission. Such
  blockers fail no-confirm Sync before mutation; unrelated interactive work may
  execute, but the diagnostic still makes the result a failure.
- Neither directional narrowing nor no-route outcomes change Base eligibility,
  which follows configured policy. Guards cannot enable policy-forbidden flow.
- Guards use captured pipe I/O, never receive `DOTMAN_NO_CONFIRM`, and may use configured elevation.
- Each repo, resolved package instance, and target guard runs once per plan build. Guards are not emitted as execution steps or rerun after review or selection.
- Guard outcomes never change static ownership or hide malformed configuration and ownership conflicts.
- `pre_*` runs immediately before the package's selected target steps.
- `pre_*` stays hard-fail only: any non-zero exit, including `100`, fails the run.
- `post_*` runs only when earlier steps for that package succeed.
- `post_*` stays hard-fail only: any non-zero exit, including `100`, fails the run if it executes.
- Execution contains pre/post hooks only. Repo pre/post hooks wrap retained package/target work; target pre/post hooks wrap their target actions.
- Package hooks normally run when the package still owns at least one non-noop effective target after tracked-target winner resolution and any interactive target exclusion.
- Target hooks normally run when that target still owns at least one non-noop effective target action after winner resolution and any interactive target exclusion.
- Repo hooks normally run when the finalized repo still has any retained package or target work.
- If a package hook declares `run_noop = true`, dotman may retain that hook as standalone hook-only package work when the package has no executable target steps for the active operation.
- If a target hook declares `run_noop = true`, dotman may retain that hook as standalone hook-only target work when that target action is noop.
- If a repo hook declares `run_noop = true`, dotman may retain that hook as standalone hook-only repo work when the finalized repo has no lower-scope work.
- If only specific command objects declare `run_noop = true`, standalone noop hook work retains those commands only.
- Standalone hook-only package execution must not fabricate target writes or snapshots.
- Standalone hook-only target or repo execution must not fabricate target writes or snapshots.
- Provenance alone should not cause hooks to execute.
- Repo hook template expansion and env stay repo-scoped only. Dotman intentionally does not inject ambiguous single-package-entry values like `DOTMAN_PROFILE` or `DOTMAN_PACKAGE_ID` there.
- Repo hook env includes `DOTMAN_REPO_NAME`, `DOTMAN_REPO_ROOT`, `DOTMAN_STATE_PATH`, `DOTMAN_OPERATION`, `DOTMAN_NO_CONFIRM`, and flattened repo vars as `DOTMAN_VAR_*`.
- Package hook env includes repo hook vars plus `DOTMAN_PACKAGE_ID`, `DOTMAN_PACKAGE_ROOT`, `DOTMAN_PROFILE`, `DOTMAN_OS`, `DOTMAN_CPU_ARCH`, and flattened package vars as `DOTMAN_VAR_*`.
- `DOTMAN_PACKAGE_ROOT` is the package directory containing that package's `package.toml`.
- Target hook env includes package hook vars plus `DOTMAN_TARGET_NAME`, `DOTMAN_TARGET_REPO_PATH`, `DOTMAN_TARGET_LIVE_PATH`, `DOTMAN_REPO_PATH`, `DOTMAN_SOURCE`, and `DOTMAN_LIVE_PATH`.
- Execution-time pre/post hooks receive `DOTMAN_NO_CONFIRM=1` when global CLI `--no-confirm` is active and `0` otherwise. Planning guards do not receive it.
- Hooks never auto-escalate through target path permissions, even when adjacent target work touches protected paths. Elevation only comes from explicit command metadata or repo-level `default_command_elevation`.
- If a hook really must run as root, use explicit command metadata such as `{ run = "systemctl restart sddm", elevation = "root" }`.
- If a hook only sometimes needs elevation, use `elevation = "intercept"` and call `sudo` only after the script proves privileged work is required.
- Repo-wide helper scripts live under `scripts/`.
- Package-specific scripts live inside the package, for example `hooks/`.
- Prefer explicit runner commands such as `sh hooks/push.sh`, `python3 hooks/render.py`, or `uv run hooks/render.py` instead of relying on executable bits.
- When a Python helper depends on repo-managed dependencies, prefer `uv run --project "$DOTMAN_REPO_ROOT" ...` so it uses the repo `pyproject.toml` and lockfile.
- Bare script paths should be reserved for cases where the repository intentionally manages executability.
- Reusable root-level action definitions are not needed. Shared behavior should live in `scripts/` and be invoked from hooks or transform strings with template-expanded args.
- Target-level command strings may be repo scripts, package-local scripts, or inline command strings.
- Command strings may use the same template expansion rules as other string values.
- Dotman may pass standard path and context values to target commands through both env vars and command args.
- Projection and Editor syntax and inheritance are defined in [Projection and Editor configuration](#projection-and-editor-configuration).
- Directory targets inherit providers per child, including Editor; Path Rules refine each child independently.
- Render derives live representation; Capture derives repository representation from the live endpoint. Providers read endpoints at their real paths, so they may use neighbouring files; projections of an unapplied repository Proposal read a private staged file. Editor explicitly modifies staged repository sources.
- Hook `io` defaults to `pipe`; `tty` requires an interactive terminal. No-confirm execution cannot invoke an interactive provider or run a `tty` hook; sudo authentication is unaffected.
- Capture failure stays a typed failure. It never launches Editor or chooses another Resolution Intent.
- When `pull` writes repo-side files while dotman is running under `sudo`, dotman should restore ownership of the written repo path back to the invoking user so the repo does not get stranded as root-owned.
- Live file mode checks should compare against explicit target `chmod` where applicable. Directory-target child-file checks should compare only the Git-tracked executable bit.

## Current Boundaries

- Install behavior is copy-only.
- `var_schema` is currently not supported.
- Prefer complete packages over hidden merging or cross-package coupling.

## Reference Paths

- `examples/repo/packages/`
- `examples/repo/groups/`
- `examples/repo/profiles/`
- `examples/repo/local.example.toml`
- `$XDG_CONFIG_HOME/dotman/repos/<repo-name>/local.toml`
