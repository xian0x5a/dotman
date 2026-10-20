---
name: dotman
description: dotman dotfile manager reference. Use when working in a dotfiles repo managed by dotman (`packages/*/package.toml`, `repo.toml`, profiles, groups), filtering noisy app state out of synced settings files, or running dotman commands.
---

# dotman

This file is an index. Read only the entries your task needs.

## Where the docs live

- `README.md`, `docs/...` and `examples/...` are paths in the dotman repo. A dotman checkout is a repo containing `src/dotman/`; there, read them locally.
- Elsewhere, fetch `https://raw.githubusercontent.com/ewgdg/dotman/<ref>/<path>`. Take `<ref>` from `dotman --version`: the version for a release (`0.9.1`), the hash after `+g` for a dev build (`0.10.5.dev55+ge34356e88` → `e34356e88`), or `main` if neither resolves. Docs at the wrong ref describe flags the installed CLI may lack.
- To list a directory remotely, use `gh api repos/ewgdg/dotman/contents/<dir>?ref=<ref>`.
- `§ Heading` names a section inside that file. `references/...` files ship with this skill.
- For flags, `dotman <command> --help` from the installed CLI is authoritative.

## Operating rules

- Global options go before the command, spelled in full (no abbreviations): `dotman --no-confirm --json <command> ...`. Use `--no-confirm` to avoid prompts and `--json` when you parse output. Elevation can still ask for a sudo password, on the terminal, or through a configured `SUDO_ASKPASS` helper when there is no terminal. Dotman's own login passes `-A` then, so the helper runs without a display. Without a terminal, each `sudo` call inside an `intercept` hook asks on its own, and sudo needs a display for askpass unless the hook calls `sudo -A`.
- Run freely: `list`, `info`, `doctor`, `search`, `transform` and `rewrite` to stdout, `render jinja`, and any command with `--dry-run`.
- Every other command (`push`, `pull`, `sync`, `restore`, `track`, `untrack`, `add`, `capture`, `reset`, ...) writes to the user's machine, repo or dotman state. Run one when the task needs its effect, after a `--dry-run` preview where the command has one. `reset sync-base` has no confirmation and no dry-run. When `doctor` warns about corrupt or orphaned Sync Bases, `reset sync-base --unusable [repo]` discards them all (use it instead of `<sync-unit>`, never together).
- To apply a change, prefer `sync`: it merges both sides against the Sync Base, while `push` and `pull` overwrite the other side. No-confirm Sync leaves both-policy drift without a Base unselected, prints `[skipped] <identity> (no Base)` and still exits 0; resolve that with `push` or `pull` only once you know which side holds the wanted content.
- A stale tracked row (its profile, package or `extends` parent is gone) is skipped, not fatal. Sync, push and pull list it as `[skipped] <repo>:<package>@<profile> (<reason>)` with the `dotman untrack` command that removes it; other commands print a `warning:` line to stderr. Naming the stale package itself (`dotman push <repo>:<package>`, or `dotman track <repo>:<package>@<profile>` when the package no longer resolves) fails with an error that carries the same untrack command instead. Check `dotman list tracked` before untracking.
- Leave elevation (sudo) prompts to the user. Never run `sudo dotman`: dotman elevates protected paths itself, and repo commands refuse to run as root under sudo.
- A package's ID is its folder under `packages/` (`packages/work/git/package.toml` is `work/git`). Manifests have no `id` field; dotman rejects one. Packages cannot nest: below a folder holding `package.toml`, every file (including one named `package.toml`) is package content.
- Package, group, profile, target and repo names must be printable (`str.isprintable()`), and dotman prints them as written: a control character, bidi override, zero-width joiner (so no ZWJ emoji sequence), private-use glyph (such as a Nerd Font icon) or non-ASCII space such as NBSP makes the repo or config fail to load. So must a target's `source` and `path`, its path rule `pattern`s and its editor `additional_sources` entries in `package.toml`, and a repo's `path` in `config.toml`, checked as written before templates render and before `~` and `$VAR` expand; `dotman add` refuses a live path that is unprintable (rename the file, or track a directory above it). Paths after rendering and expansion are outside text and print escaped. Dotman trusts the repos you configure: read-only commands such as `search` print a repo's descriptions as written, so inspect an untrusted repo before adding it. A repo name (`[repos.<name>]` in `config.toml`) also cannot be blank or contain any of `\ : @ < > . /`, because it is the `repo` in `repo:package` and a directory name. Full rules: `docs/repository.md` § Core Objects, `docs/config.md` § Repos.
- Name targets as `repo:package.target` and package instances as `repo:package<profile>.target`.
- Scope a write command to what changed. Name the targets (`repo:package.target`) when only those files changed; their package hooks still run. A package scope (`dotman pull repo:package`) also covers the packages it depends on, so add `--no-deps` when several targets or a shared input (vars, templates, `package.toml`) changed but the dependencies did not. For `pull`, run `--dry-run` on the package first to find the drifted targets, then narrow (`docs/cli.md` § Sync scope resolution).
- `on_demand = true` targets run only when named exactly (`dotman push repo:package.target`); package selectors and plain `push`/`pull`/`sync` leave them out without any output (`docs/cli.md` § Sync scope resolution).
- `disabled = true` switches a target off entirely: no command runs it, even by exact name, and `info` omits it. Use it to park a target without deleting it; use `on_demand` when it should still run when named.

## Index

### Setup and concepts

| Need | Read |
| --- | --- |
| Install dotman, register a repo, first push | `README.md` § Install, § Quick start; `docs/config.md` § Repos |
| Packages, targets, groups, profiles, vars, resolution | `docs/repository.md` § Core Objects, § Resolution Model |
| Example repo and what each package demonstrates | `examples/repo/README.md` |
| Sync vocabulary: Proposal, Sync Base, Capture, Guard | `GLOSSARY.md` |

### Writing manifests

| Need | Read |
| --- | --- |
| Adopt an existing live file into a package | `docs/cli.md` § Add |
| Target fields: `path`, `type`, `chmod`, `sync_policy`, `on_demand`, `disabled`, `path_rules`; install/update `probe` targets | `docs/repository.md` § Targets |
| Package instances and inheritance | `docs/repository.md` § Package Identity Modes, § Package Inheritance |
| Skip files or subtrees in directory targets | `docs/repository.md` § Unified exclusions |
| Sync only part of a JSON/YAML/TOML/plist/XML settings file | `references/noise-filtering.md` |
| `render`, `capture`, `editor`, `compare` fields; writing a custom Render/Capture command | `docs/repository.md` § Projection and Editor configuration, § Custom Render and Capture commands |
| Per-machine values with Jinja templates | `docs/templates.md` |
| Hooks, elevation, hook env vars | `docs/repository.md` § Hooks And Commands |
| `~` vs absolute home paths inside file content | `docs/cli.md` § Home Path Rewrites |
| Local per-machine overrides | `docs/config.md` § Local Overrides |
| Live files that are symlinks (e.g. migrating from stow) | `docs/config.md` § Symlink Handling |

### Running dotman

| Need | Read |
| --- | --- |
| Identifiers and selectors | `docs/cli.md` § Identifier Syntax, § Selectors |
| Track and untrack packages, choose profiles | `docs/cli.md` § Track, § Untrack, § Profiles |
| Push, pull and their flags | `docs/cli.md` § Confirmation and execution flags, § Push, § Pull |
| Commit what sync/pull wrote (`--commit`, `[git] commit_message`) | `docs/sync.md` § Commit Work; `docs/config.md` § Git |
| Sync, Proposals and Sync Bases | `docs/cli.md` § Sync, then `docs/sync.md` |
| Inspect state, diagnose problems | `docs/cli.md` § Diagnostics And Catalog Inspection, § Tracked Package State |
| Snapshots and restore | `docs/snapshot.md`, `docs/cli.md` § Restore |
| User config (`~/.config/dotman/config.toml`) | `docs/config.md` |
