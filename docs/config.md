# dotman Config

This reference owns user-level manager configuration. See
[repository configuration](repository.md) for package policy and providers,
[CLI](cli.md) for per-run overrides, and [Sync lifecycle](sync.md) for behavior.

## Config Path

- Dotman should read user config from `$XDG_CONFIG_HOME/dotman/config.toml`.
- If `XDG_CONFIG_HOME` is unset, dotman should fall back to `~/.config/dotman/config.toml`.

## Repos

- User config defines the available dotman repos, not the package/group/profile schema inside a repo.
- Repos should be declared under `[repos.<name>]`.
- Each repo entry must define `path`.
- Repos may define `state_key` to control where tracked package state is stored under the manager state root.
- Repos should define `order`, where lower values are searched first.
- Repo `order` values should be unique; ties should fail config validation.
- Repo `state_key` values should be unique.
- If `state_key` is omitted, dotman should default it to the repo name.
- Dotman should derive the repo state dir as `$XDG_STATE_HOME/dotman/repos/<state_key>/`.
- `state_key` must be a non-empty simple key and must not contain path separators, use `.` / `..`, or contain a character that fails `str.isprintable()`. A bad key is a usage error that names the config file.
- Repo names should be treated as stable identifiers, because dotman also uses the repo name to locate per-repo local overrides under XDG config.
- A repo name is the `repo` in `repo:package` and a directory under `repos/`, so it follows the package ID rules without namespaces: it must not be blank, contain `\ : @ < > .` or `/`, or contain a character that fails `str.isprintable()`. A bad name is a usage error that names the config file and shows the name with each unprintable character as `\uNNNN`, and it is checked before `state_key`.

## Local Overrides

- Machine-local or private overrides should not live in the repo.
- Dotman should read optional per-repo local overrides from `$XDG_CONFIG_HOME/dotman/repos/<repo-name>/local.toml`.
- If `XDG_CONFIG_HOME` is unset, dotman should fall back to `~/.config/dotman/repos/<repo-name>/local.toml`.
- Local override files are scoped to one configured repo.
- Per-repo local overrides are limited to `[vars]` data.
- Local overrides should not redefine package structure or behavior such as targets, hooks, dependencies, groups, or profiles.
- Unknown top-level keys in a local override file should fail validation.
- A missing local override file means that repo has no machine-local overrides.
- If a repo is renamed in manager config, its local override path should move with the new repo name.

## Local Override Loading

- Dotman should derive the local override path from the configured repo name, not from the repo filesystem path.
- Dotman should load at most one local override file for a repo.
- Dotman should attempt local override loading only after the manager config resolves the repo set.
- A missing local override file is normal and should not produce a warning.
- A present but unreadable or malformed local override file should fail fast.
- Local override loading should be independent per repo; one repo's local file should not affect another repo.
- Local override data should participate only in variable resolution.
- Local override data should not change tracked package identity.

## Symlink Handling

- Dotman should declare symlink handling under `[symlinks]`.
- `file_symlink_mode` defaults to `prompt` and accepts `prompt` or `follow`.
- `dir_symlink_mode` defaults to `fail` and accepts `fail` or `follow`.
- CLI flags `--file-symlink-mode` and `--dir-symlink-mode` should override the config file for a single run.
- `prompt` requires explicit authorization when publication must replace a file symlink. No-confirm runs cannot grant it. Deletion unlinks the leaf without deleting its referent; repository-only work does not replace it.
- `follow` means dotman manages the resolved target instead of the symlink itself.

## Snapshots

- Snapshot config is manager-level and applies to real Push and Sync live publication across the whole dotman run, even when that run spans multiple repos.
- Snapshot settings should be declared under `[snapshots]`.
- `enabled` is optional and defaults to `true`.
- `path` is optional and overrides the snapshot storage root.
- If `path` is omitted, dotman should default snapshot storage to `$XDG_DATA_HOME/dotman/snapshots/`.
- If `XDG_DATA_HOME` is unset, dotman should fall back to `~/.local/share/dotman/snapshots/`.
- `max_generations` is optional, must be a positive integer, and defaults to `10`.
- `max_generations` is count-based retention. Dotman should prune the oldest snapshots when the retained snapshot count exceeds that limit.
- Snapshot storage is distinct from repo tracked package state. Snapshots belong under data home, while tracked package state stays under state home.

## Git

- `[git]` holds settings for Sync and Pull **Commit Work**
  ([Sync lifecycle](sync.md#commit-work)).
- `git.commit_message` is optional and defaults to
  `"chore(dotman): {summary}\n\n{packages}"`.
- Placeholders:
  - `{operation}`: `sync` or `pull`.
  - `{count}`: number of targets committed.
  - `{summary}`: operation and the narrowest scope: `pull zsh.zshrc` for one
    target, `pull zsh (3 targets)` for one package, otherwise
    `pull 5 targets in 3 packages`.
  - `{packages}`: one package per line, sorted, instances kept (`git<work>`).
  - `{repo}`: the dotman repo name.
- An unknown placeholder, or a format spec or conversion that cannot render,
  such as `{count:s}` or `{repo!x}`, fails config loading.

## UI

- UI behavior is manager-level and applies across interactive selector pickers, exclusion menus, diff review screens, and shared human-readable path output.
- UI settings should be declared under `[ui]` and `[ui.menus]`.
- `ui.full_paths` is optional and defaults to `false`.
- When `ui.full_paths` is `true`, dotman should show unabridged absolute paths in human-readable output that uses the shared path renderer.
- `ui.compact_path_tail_segments` is optional, must be an integer greater than or equal to `1`, and defaults to `2`.
- When paths are compacted, `ui.compact_path_tail_segments` controls how many ending path segments are kept, including the final file or directory name. For example, `3` renders `~/.local/share/nvim/init.lua` as `~/.../share/nvim/init.lua`.
- `ui.menus.bottom_up` is optional and defaults to `true`.
- When `ui.menus.bottom_up` is `true`, dotman should render interactive selector menus from bottom to top.
- The `DOTMAN_MENU_BOTTOM_UP` environment variable should continue to override `ui.menus.bottom_up` for a single run.

Example:

```toml
[repos.main]
path = "~/projects/dotfiles"
order = 10
state_key = "main"

[repos.test]
path = "~/sandbox/dotfiles"
order = 20
state_key = "test"

[snapshots]
enabled = true
max_generations = 10

[git]
commit_message = "chore(dotman): {summary}\n\n{packages}"

[ui]
full_paths = false
compact_path_tail_segments = 2

[ui.menus]
bottom_up = true

[symlinks]
file_symlink_mode = "prompt"
dir_symlink_mode = "fail"
```

Example per-repo local override:

```toml
[vars]
INSTALL = "printf 'install %s\\n'"

[vars.git]
user_email = "local@example.test"

[vars.nvim]
colorscheme = "industry"
```
