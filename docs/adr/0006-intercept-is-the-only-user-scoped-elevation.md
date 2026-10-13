# Intercept is the only user-scoped command elevation

Command elevation supports `none`, `root`, and `intercept`. The `lease` and `broker` modes and the `dotman elevation request` helper were removed (#111). This reverses `plans/elevation-broker.md`, which preferred `broker` and kept `intercept` as a last resort for scripts that cannot be edited.

- `broker` gave nothing `intercept` lacks. Intercept starts the same broker and exports `DOTMAN_ELEVATION_BROKER`, and a script authenticates early with `sudo -v` through the shim. Broker also tied scripts to dotman.
- `lease` only added eager authentication for `sudo` called by absolute path or with a stripped `PATH`.
- Without a controlling terminal, sudo's default `timestamp_type=tty` falls back to per-parent-process tickets (#108, reproduced). Dotman cannot make its ticket reach a `sudo` that a script calls directly, which is how `lease` and `broker` worked. Under `intercept`, the shim can at least let each call authenticate itself; see ADR 0007.
- The repo default stays `none`. Defaulting to `intercept` would let any hook ride dotman's cached ticket without the repo opting in.

Revisit if a real repo needs a tool that cannot be edited, calls `sudo` by absolute path, and only sometimes needs it.
