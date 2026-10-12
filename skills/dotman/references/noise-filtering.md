# Filtering noise from a settings file

Most app settings files mix **portable** preferences with **noise**: state the app writes for itself. Sync the portable part; keep the noise live-local. Paths like `docs/...` resolve as described in `SKILL.md` § Where the docs live.

## 1. Find the noise

Look for **churn** instead of guessing from key names:

1. Copy the live file to a scratch directory.
2. Ask the user to open, use and close the app without changing any setting. Diff against the copy: every key that changed is noise.
3. Ask the user to change one real setting. Diff again: that key is portable, and its neighbours show where settings live.
4. Classify the unchanged keys with the catalogue below. Ask the user about any key that is still unclear.

Done when every top-level key, and every mapping you plan to split, has a verdict.

| Noise kind | Examples | Treatment |
| --- | --- | --- |
| UI and session state | window bounds, panel widths, last tab, zoom | live-local |
| History | recent files, recent projects, search history | live-local |
| Counters and timestamps | launch count, last update check, "tip shown" flags | live-local |
| Machine identity | device or install IDs, hostnames, absolute project paths | live-local |
| Migration markers | schema versions, `migrated*` flags | live-local; the app rewrites them |
| Accounts and secrets | tokens, emails, org IDs | live-local; secrets never enter the repo |
| Home paths inside portable values | `/home/me/...` | portable, stored as `~/...` with the home path rewrite |
| Values that differ per OS or host | fonts, scale factors | live-local inside a transform pair; a templated value cannot round-trip through `capture` |

## 2. Pick the lowest tier that expresses the split

- Whole file portable → plain target, no projection.
- Noise is whole files or subtrees of a directory target → `ignore.patterns` (`docs/repository.md` § Unified exclusions).
- Those subtrees are listed in live state, such as a package manager's lock file → `ignore.command`, which prints the exclusion lines. Do not copy the list into `patterns`; it drifts.
- Noise is keys inside one JSON, YAML, TOML, plist or XML file → **transform pair** (below) on the file target.
- Noise is keys inside child files of a directory target → the same transform pair as `render` and `capture` on a `path_rules.<name>` entry matching those files (`docs/repository.md` § Targets).
- The split depends on values, not key paths (for example rendering `null` instead of `""`) → a Jinja template for the whole file (`docs/templates.md`) or a package-local script.

## 3. Choose allowlist or denylist

Ask: when the app adds a new key upstream, should it sync by default?

- **Denylist** (yes): a preferences file with a few noisy keys. List the noise. Pull or sync before pushing, or push deletes keys the app added since the last capture.
- **Allowlist** (no): a file that is mostly app state, such as `~/.claude.json` or Electron app data. List the portable keys. To leave a few keys of a portable subtree live-local, select the subtree and exclude them with `not:`: `settings 'not:settings.windowBounds'`, or `'not:re:(^|\.)cache$'` for a key at any depth. XML has no `not:`.

## 4. Write the transform pair

Follow `docs/repository.md` § Partial structured files: the render/capture table, the example target, and the deletion and convergence rules.

- If the repo already defines shorthand vars for these commands (for example `JSON_RENDER` in a profile), reuse them.
- Keep the selector list in `[vars.<package>]`, grouped, with a comment per group saying why it syncs or stays local.
- Add `chmod = "600"` when the live file holds anything private.
- Selector syntax and per-format flags: `docs/cli.md` § Structured JSON transforms, or `dotman transform <format> --help`.

## 5. Verify the round-trip

Run both commands by hand against the real live file, substituting paths for `$DOTMAN_LIVE_PATH` and `$DOTMAN_REPO_PATH`:

- Capture output holds every portable key and no noise keys.
- Render output equals the live file except for the portable keys taken from the repo.
- Capturing the render output reproduces the repo file.

Then, with the package tracked, `dotman --no-confirm pull --dry-run <repo>:<package>.<target>` shows `[would-apply] repository write` for a new target, and `push --dry-run` shows the expected live change. A `sync --dry-run` on a new target only reports that it needs a first review, so it verifies nothing.

## Pitfalls

- Lists are atomic: select a whole list or split at its parent.
- An app that holds the file in memory rewrites it on exit. Note in a comment that push should run while the app is closed.
- A secret that shares a subtree with portable keys: exclude it with `not:` so it never enters the repo.
