# dotman Template Targets

Use this when the repo stores a Jinja source file, but the live file is the rendered result.

## The Important Part

For template targets, `push` is easy. `pull` is the tricky part.

## Safe Shell Argument Arrays

Use `shell_args` when a command template expands a TOML array into command arguments:

```toml
selectors = ["application settings", "re:^projects\\.", "$literal"]
command = "dotman transform json base.json output.json --mode cleanup --selectors {{ selectors|shell_args }}"
```

`{{ selectors|shell_args }}` quotes every array element with POSIX shell quoting. Each element reaches the command as exactly one literal argument, including whitespace, quotes, backslashes, regex syntax, newlines, and shell metacharacters. An empty string remains one empty argument. An empty array renders an empty fragment.

Input must be a flat list of strings. Bare strings, mappings, tuples, nested lists, `null`, booleans, numbers, and lists containing non-strings fail with a Jinja render error instead of being coerced. Filter is available in command/string templates, file templates, and templated variable values.

Raw Capture treats live bytes as repository representation; it cannot infer template source. Configure a suitable Capture or deliberately use the Proposal Editor before approving a template replacement.

For a Jinja target, make the forward render explicit:

- `render = "jinja"`

`jinja` is just a shortcut for the built-in renderer. If you want the command form explicitly, use:

- `render = 'dotman render jinja "$DOTMAN_SOURCE"'`

Important: that command form is only equivalent when **dotman** launches it as a target command. In that case, dotman injects the resolved selector/profile context through env vars such as `DOTMAN_PROFILE`, `DOTMAN_OS`, `DOTMAN_CPU_ARCH`, and `DOTMAN_VAR_*`.

If you run `dotman render jinja ...` manually in your shell, dotman does **not** look up a repo, package, or profile on its own. You must provide context explicitly with `--profile`, `--os`, `--cpu-arch`, and `--var`, or export the corresponding `DOTMAN_*` env vars first.

The shortcut is preferred in manifests. The explicit command form is mainly useful for understanding what dotman runs internally and for manual debugging with explicit inputs.

See [`repository.md`](./repository.md) for template var resolution.

For Pull or live-to-repository-capable Sync, you usually also want:

- `compare.repo = "render"`
- `compare.live = "raw"`
- and **either**:
  - `capture` for a non-interactive reverse projection, or
  - `editor` for an interactive workflow

For the common Jinja editor workflow, you can use the built-in shortcut:

- `editor = { type = "jinja" }`

`editor = { type = "jinja" }` recursively discovers static Jinja template dependencies such as `{% include %}`, `{% extends %}`, `{% import %}`, and `{% from ... import ... %}`, then runs the built-in editor flow with those files added as extra editable sources.

The string shorthand `editor = "jinja"` is also accepted and normalizes to the same tty-backed builtin workflow.

If you want the whole common bundle as defaults, you can also use:

- `preset = "jinja-editor"`

That preset supplies default values for:

- `render = "jinja"`
- `compare.repo = "render"`
- `compare.live = "raw"`
- `editor = { type = "jinja" }`

Explicit target keys still win over the preset.

If your template references other files dynamically, use an explicit editor provider and list those files with `additional_sources`.

If `editor` opens an editor or otherwise needs a real terminal, set:

- `editor = { run = "...", io = "tty" }`

## Built-In `capture = "patch"`

`capture = "patch"` is the built-in reverse-capture helper for patch-first template file workflows where dotman can apply reviewed live edits back onto canonical repo source.

Current built-in presets and CLI examples pair it with file targets that already have the forward render and pull review split configured as:

- `render = "jinja"`
- `compare.repo = "render"`
- `compare.live = "raw"`

The helper three-way merges the reviewed live edits onto the raw repo source, using the reviewed rendered repo view as the common ancestor. It then reprojects the candidate through the forward render path and fails unless that projection matches the review live bytes exactly.

What transfers and what fails:

- Edits to literal lines are applied to the source, including inserts, deletions, and several separate edits, even when block tags make source and rendered line counts differ.
- Edits that overlap or directly adjoin template-generated output fail: changed expression values, a literal edit on a line that also contains an expression, edits inside loop bodies or included templates, and edits to a line right next to a tag or expression line. No partial result is kept.
- With `render = "jinja"`, Capture also fails if the candidate would change any Jinja tag, expression, or comment, even when the render still matches.
- Line endings follow the source: a CRLF source stays CRLF even though Jinja renders LF. A source that mixes line endings fails when they differ from the render.

A failed patch Capture is an ordinary Capture failure: the Proposal stays unapproved, nothing is written, and the Editor opens the unchanged repository source beside the live file. Use `editor` when reverse mapping needs human judgment.

Use the explicit CLI helper when you want to debug the algorithm directly. Pass the same forward render you configured on the target, plus any template context flags that renderer needs:

```sh
dotman capture patch \
  --repo-path "$DOTMAN_REPO_PATH" \
  --render "jinja" \
  --review-repo-path "$DOTMAN_REVIEW_REPO_PATH" \
  --review-live-path "$DOTMAN_REVIEW_LIVE_PATH" \
  --profile basic \
  --var greeting=hello
```

Use `--render "jinja"` for the built-in Jinja renderer, or pass the same stdout-producing render command string you would use in target config.

For the common bundle, use:

- `preset = "jinja-patch"`

That preset supplies default values for:

- `render = "jinja"`
- `capture = "patch"`
- `compare.repo = "render"`
- `compare.live = "raw"`

To make the Jinja Proposal Editor available for deliberate editing alongside Patch Capture, use:

- `preset = "jinja-patch-editor"`

That preset supplies the same defaults as `jinja-patch`, plus:

- `editor = { type = "jinja" }`

## Example

`packages/shell/package.toml`:

```toml
description = "Shell profile"

[targets.profile]
source = "files/profile"
path = "~/.profile"
preset = "jinja-editor"
```

Equivalent explicit form:

```toml
description = "Shell profile"

[targets.profile]
source = "files/profile"
path = "~/.profile"
render = "jinja"
compare.repo = "render"
compare.live = "raw"
editor = { type = "jinja" }
```

Or, if you want a fully explicit custom editor command:

```toml
description = "Shell profile"

[targets.profile]
source = "files/profile"
path = "~/.profile"
render = "jinja"
compare.repo = "render"
compare.live = "raw"

[targets.profile.editor]
run = 'vim "$@"'
additional_sources = ["files/env.core.sh"]
io = "tty"
```

`files/profile`:

```sh
# os: {{ os }}

{% include 'env.core.sh' %}
{% if os == "darwin" %}
export SUDO_ASKPASS="$HOME/bin/askpass-macos"
{% elif os == "linux" %}
export SUDO_ASKPASS="$HOME/bin/askpass-gui"
{% endif %}
{% if cpu_arch == "x86_64" %}
export PATH="$HOME/.local/bin/x86_64:$PATH"
{% endif %}
```

`files/env.core.sh`:

```sh
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
```

### What This Config Does

- `source = "files/profile"`
  - repo source file
- `path = "~/.profile"`
  - live target path
- `preset = "jinja-editor"`
  - optional built-in default bundle for the common Jinja editor workflow
- `render = "jinja"`
  - shortcut for the built-in Jinja renderer
  - equivalent command form: `dotman render jinja "$DOTMAN_SOURCE"`
- `compare.repo = "render"`
  - pull review compares the rendered repo-side result, not raw source text
- `compare.live = "raw"`
  - pull review compares against the actual live file
- `editor = { run = "...", io = "tty" }`
  - explicit editor-based reverse-sync workflow
- `editor = { type = "jinja" }`
  - shortcut for the built-in Jinja editor helper
  - auto-adds recursively discovered static template dependencies as editable sources
- `editor = { run = "...", io = "tty" }`
  - required for full-screen editor workflows
- `editor.additional_sources = [...]`
  - declares package-relative repository components to stage alongside the Primary Source

## When To Use `capture` vs `editor`

Use `capture` when you can convert the live file back into the canonical repo source **without user interaction**.

Use `capture = "patch"` when dotman can patch the canonical source automatically and verify that the forward projection matches the reviewed live bytes exactly.

Use `editor` when a human needs to decide how the live change maps back to one or more template source files.

Capture runs during Observation or lazy Proposal materialization, according to the comparison configuration. Failures remain visible and retryable; Dotman never launches Editor automatically. Open it deliberately to resolve a failed Capture or edit a healthy Proposal.

Typical template target:

- forward path: template source -> rendered live file
- reverse path: `editor`, not blind file copy

## Transactional editing

The configured Jinja Editor recursively discovers static dependencies. Declare
dynamic dependencies with `editor.additional_sources`; custom Editors receive
only staged editable paths through `"$@"`, not writable tracked sources.
Saving rematerializes a Proposal for review. Shared Additional Source Changes
have independent Approval; they do not become implicitly approved with a Sync
Proposal.

Provider defaults, inheritance, staging order and command environment are owned
by [repository configuration](repository.md#projection-and-editor-configuration).

## Built-In `dotman render jinja`

`dotman render jinja <source-path>` renders a Jinja source file to stdout.

It uses the same built-in renderer as `render = "jinja"`.

When dotman runs it as a target command, it first resolves the selector/profile context, then provides it through env vars.

Resolution source:

- `profile` comes from the selected selector+profile form, for example `...@basic`
- template vars come from `package vars -> composed profile vars -> repo local override vars`
- package vars are the package's own vars merged over the vars of the packages it depends on; a package that depends on it never changes them, so a dependency renders the same however it was tracked
- `os` is the target OS for that selector/profile context: a string `os` var wins; otherwise dotman splits the profile and its included profiles' IDs on `/`, `-`, `_` and `.` and looks for a whole segment, ignoring case. `mac`, `macos` or `darwin` gives `darwin`; `linux`, `arch` or `archlinux` gives `linux` (`darwin` wins if both appear). Otherwise it falls back to the host's Python `sys.platform`. So `os/mac` and `home-linux` infer an OS, while `machete` and `research` do not
- `cpu_arch` is the host CPU architecture from `uname -m`, normalized so one CPU has one name on every OS: `x86_64` (also for `amd64`) and `arm64` (also for Linux `aarch64`); other values such as `riscv64` pass through unchanged. Set a string `cpu_arch` var, `--cpu-arch`, or `DOTMAN_CPU_ARCH` to override it; overrides are normalized the same way. Map to an upstream's own names (for example Go's `amd64` or Rust's `aarch64`) at the call site

Injected env:

- `DOTMAN_PROFILE`
- `DOTMAN_OS`
- `DOTMAN_CPU_ARCH`
- `DOTMAN_VAR_*` (flattened with `__` as the nested key separator, for example `vars.git.user_name` -> `DOTMAN_VAR_git__user_name`)

`dotman render jinja` reconstructs the nested `vars` object from those env vars unless you override them with CLI flags.

For manual testing, you can also pass values explicitly:

```sh
dotman render jinja --profile basic --os linux --cpu-arch x86_64 --var git.user_name='Example User' path/to/file
```

## Rules Of Thumb

- No `template = true` flag or separate template target type exists
- Jinja file rendering is explicit: use `render = "jinja"`, `preset = "jinja-editor"`, `preset = "jinja-patch"`, or `preset = "jinja-patch-editor"`
- `capture = "patch"` is dotman's automatic reverse-capture helper; in the current built-in Jinja workflow it is paired with `render = "jinja"`, `compare.repo = "render"`, and `compare.live = "raw"`
- dotman follows the configured `render`, `compare`, `capture`, and `editor` workflow
- `.tmpl` is optional naming only
- If your source uses Jinja `{% include %}`, relative paths resolve from the source file directory
- For Jinja pull review, use:
  - `compare.repo = "render"`
  - `compare.live = "raw"`
- For template-style pull execution, use either `capture = "patch"` for automatic source patching or `editor` for interactive/manual work
- If `editor` is interactive, set `editor = { run = "...", io = "tty" }`
