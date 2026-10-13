# Tty-less intercept sudo authenticates itself

Without a controlling terminal, the intercept `sudo` shim skips dotman's broker and runs real `sudo` without `-n`, so each call authenticates on its own through `SUDO_ASKPASS` or a NOPASSWD rule (#108). With a terminal, the shim still asks dotman for the sudo lease and runs `sudo -n`.

sudo's default `timestamp_type=tty` falls back to per-parent-process tickets when no terminal is present. Only a `sudo` that dotman starts itself can reuse dotman's ticket, which is why `root` works and the shim's `sudo -n` failed with `a password is required`.

The cost is one askpass prompt per `sudo` call in tty-less runs, which are rare.

Rejected: the shim hands its arguments, cwd, environment, and stdio descriptors to the broker, and dotman runs `sudo -n` as its own child. It worked, but:

- It changed the terminal path that every intercept call uses, to fix a rare case. The first version leaked Ctrl-C because the sudo dotman started inherited dotman's ignored SIGINT; that part was fixable.
- Any process of the same user that can reach the broker socket could get root commands run without a password while an intercept command was active. Before, it could only make dotman ask for a password. Closing that needs a caller ancestry check per platform.

Revisit if tty-less runs with many `sudo` calls per hook become common enough that per-call prompts hurt. Then forward only tty-less calls and add the ancestry check.
