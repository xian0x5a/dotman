---
status: done
---

# Elevation broker plan

> Superseded: `lease`, `broker`, and `dotman elevation request` were removed in #111. See `docs/adr/0006-intercept-is-the-only-user-scoped-elevation.md`.

Date: 2026-04-24

## Goal

Replace newly-added command `privileged = true` metadata before it spreads, and add future-proof dotman-managed elevation for hook/reconcile commands.

Target schema:

```toml
{ run = "...", elevation = "none" }
{ run = "...", elevation = "root" }
{ run = "...", elevation = "lease" }
{ run = "...", elevation = "broker" }
{ run = "...", elevation = "intercept" }
```

Default is `elevation = "none"`.

## Decisions

### Field name: `elevation`

Use `elevation`, not `sudo` or `privilege`.

Reasons:

- `sudo` names one backend and is too narrow for future `doas`, `pkexec`, or platform helpers.
- `privilege` describes a state and is too close to old `privileged = true`, which implies the whole command runs as root.
- `elevation` describes the capability/action dotman manages.

### Mode names

| Mode | Meaning | Command identity |
| --- | --- | --- |
| `none` | no elevation support | user |
| `root` | run the whole command through sudo/noninteractive root shell | root |
| `lease` | acquire + keep sudo lease before command starts | user |
| `broker` | expose explicit lazy broker request API to child | user |
| `intercept` | expose broker plus PATH sudo shim interception | user |

`broker` means explicit request mode. `intercept` means sudo shim mode.

### Drop `privileged`

Because `privileged = true` was just released and is semantically misleading, remove it sooner rather than later.

Rules:

- New manifests should use `elevation = "root"` instead of `privileged = true`.
- Parser should reject `privileged` in command objects with a clear migration error.
- If preserving a temporary compatibility path is needed for one patch release, translate `privileged = true` to `elevation = "root"` only with a deprecation warning; do not keep it long-term.
- Reject configs that define both `privileged` and `elevation`.

Preferred direct cutover: reject `privileged` now.

## Behavior

### `elevation = "none"`

Current normal behavior:

- pipe commands use captured stdout/stderr and `stdin=DEVNULL`
- tty commands get the terminal
- no dotman sudo request

### `elevation = "root"`

Replacement for old `privileged = true`:

- dotman requests sudo before execution
- command is wrapped with noninteractive sudo shell, e.g. `sudo -n -E /bin/sh -c ...`
- process runs as root

Use for commands like:

```toml
{ run = "systemctl restart sddm", elevation = "root" }
```

Do not use for `yay` or other AUR helpers.

### `elevation = "lease"`

Dotman owns sudo lease for the command lifetime, but command still runs as user.

- request sudo before command starts
- keep lease alive while command runs
- do not wrap command with sudo

Use when command may call sudo later and pre-auth is acceptable.

### `elevation = "broker"`

Dotman starts/attaches a broker and exports env for explicit child requests.

Command still runs as user. Dotman does not prompt unless child requests elevation.

Environment:

```sh
DOTMAN_ELEVATION_BROKER=...
DOTMAN_ELEVATION_REASON=...
```

Canonical request command:

```sh
dotman elevation request "install missing Arch packages"
```

Expected install script shape:

```sh
[ -n "$missing_packages" ] || exit 0

dotman elevation request "install missing Arch packages"

printf '%s\n' "$missing_packages" |
  xargs -r yay -S --needed --answerdiff=None --answeredit=None
```

If no broker socket env exists, helper fails clearly. The helper does not need to know which manifest mode created the socket; it only cares whether `DOTMAN_ELEVATION_BROKER` points to a reachable broker. Do not silently fall back to `sudo -v`.

### `elevation = "intercept"`

Dotman exposes the same broker plus a temporary `sudo` shim first in `PATH`.

Flow:

1. command runs as user
2. child/tool calls `sudo ...`
3. shim contacts dotman broker
4. dotman requests/refreshes sudo lease
5. shim execs real sudo with `-n`

Use only when scripts/tools cannot be edited.

Safety rules:

- Real sudo path is resolved before PATH is modified.
- Shim never reads password.
- Shim never shells untrusted args; it execs real sudo directly.
- If broker fails, shim exits nonzero instead of prompting/hanging.

## CLI helper

Add hidden/internal command first:

```sh
dotman elevation request [reason]
```

Behavior:

- requires `DOTMAN_ELEVATION_BROKER`
- treats broker availability as capability discovery: socket present + reachable means request can proceed
- does not inspect or care whether the parent command used `elevation = "broker"` or `elevation = "intercept"`
- sends reason to broker over Unix socket
- waits for broker success/failure
- exits `0` on ready lease
- exits nonzero with clear stderr if no broker or broker denies/fails
- does not fall back to direct `sudo -v` by default

Possible future flag:

```sh
dotman elevation request --fallback=sudo [reason]
```

Keep this out of the initial implementation unless a concrete non-dotman execution workflow needs it.

Optional later alias:

```sh
dotman sudo request [reason]
```

Do not make alias canonical.

## Broker design

Use a per-dotman-process Unix socket under runtime temp dir.

Broker responsibilities:

- validate request comes from expected user/session where possible
- serialize requests so only one sudo prompt can happen at a time
- call existing `request_sudo(reason)`
- ensure existing sudo keepalive thread remains alive
- return structured success/failure

Broker lifetime:

- start lazily when first command with `elevation = "broker"` or `"intercept"` runs
- stop after execution session ends
- cleanup socket/temp shim dir on exit

No password flows through broker protocol.

## Manifest/model changes

Update `HookCommandSpec`:

```py
ElevationMode = Literal["none", "root", "lease", "broker", "intercept"]

@dataclass(frozen=True)
class HookCommandSpec:
    run: str
    io: HookCommandIO = "pipe"
    elevation: ElevationMode = "none"
```

Update `HookPlan` similarly.

Update `to_dict()` payloads and human rendering to show non-default elevation:

- `[root]` for `root`
- `[lease]` for `lease`
- `[broker]` for `broker`
- `[intercept]` for `intercept`
- retain `[tty]` for tty IO

Do not show `[none]`.

## Execution changes

In `_run_command` / `_run_command_with_terminal`, replace `privileged: bool` with an elevation mode or already-prepared execution context.

Pseudo behavior:

```py
if elevation == "root":
    request_sudo(reason)
    command = sudo_prefix_command(command)
elif elevation == "lease":
    request_sudo(reason)
elif elevation in {"broker", "intercept"}:
    env.update(broker.env(reason=reason, intercept=elevation == "intercept"))
```

Pipe-mode stdin stays `DEVNULL`.

For `intercept`, prepend shim dir to PATH only for that command env.

## Preflight behavior

Current execution preflights all privileged steps. New behavior:

- `root`: preflight sudo before execution session starts
- `lease`: preflight sudo before command starts, not necessarily whole session
- `broker`: no preflight; lazy only
- `intercept`: no preflight; lazy only

Reason: broker/intercept exist specifically to avoid prompting when script condition is false.

## UI/docs

Update command display wherever hook commands are rendered:

```txt
[root] systemctl restart sddm
[broker] sh install_arch_packages.sh
[intercept] sh legacy-installer.sh
[tty] nvim file
```

If combined:

```txt
[tty] [root] some-terminal-root-command
```

Docs:

- command object schema in repository docs
- elevation mode table
- warn against `elevation = "root"` for AUR helpers
- example Arch package install hook with `elevation = "broker"`

## Tests first

Add/adjust tests for:

1. Manifest parsing
   - accepts each elevation value
   - defaults to `none`
   - rejects unknown elevation
   - rejects `privileged`
   - rejects both `privileged` and `elevation` if compatibility path temporarily exists

2. Planning/info output
   - `HookPlan.elevation` preserved
   - JSON includes elevation
   - human output shows elevation badges

3. Execution
   - `root` prefixes command through sudo and requests sudo
   - `lease` requests sudo but does not prefix command
   - `broker` does not request sudo at command start and injects broker env
   - explicit helper request triggers `request_sudo`
   - `intercept` injects PATH shim and broker env
   - pipe stdin remains `DEVNULL`

4. Failure modes
   - broker helper without env fails cleanly
   - shim broker failure exits nonzero and does not prompt
   - noninteractive pipe mode never lets child read dotman stdin

## Implementation order

1. Rename model/parser from `privileged` to `elevation`; reject old field.
2. Update planning, JSON, info, and hook rendering badges.
3. Replace executor `privileged` bool with elevation mode.
4. Implement `root` and `lease` modes using existing sudo lease machinery.
5. Add explicit broker service + `dotman elevation request` helper.
6. Implement `broker` mode env injection.
7. Implement `intercept` mode PATH shim.
8. Update docs and examples.
9. Patch dotfiles Arch install hook to call `dotman elevation request` after missing-package check.

## Open questions

- Should `dotman elevation request` be hidden from top-level help initially?
- Should `intercept` shim support only `sudo` initially, or also `sudoedit`?
- Should `root` be allowed with `io = "tty"`, or should tty root commands require explicit terminal confirmation?

## Settled follow-ups

- `dotman elevation request` discovers broker support by `DOTMAN_ELEVATION_BROKER` socket availability. It does not inspect manifest mode.
- `dotman elevation request` fails hard when no broker socket is available. No implicit `sudo -v` fallback.

## Non-goals

- storing sudo passwords
- parsing child stderr for sudo prompts
- running AUR helpers as root
- broad policy engine for arbitrary privileged operations
- cross-process long-lived daemon beyond one dotman execution session
