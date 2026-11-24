# A probe target's triggers activate it; nothing re-runs its probe

Probes run during planning, before any write, so a probe that reads what the same run writes sees stale state and drops its hooks (xian0x5a/dotfiles#8: a skill added in a push was not linked, a removed one kept its link). A probe target now lists the dependencies whose writes it follows with `additional_triggers = ["package", "package.target", ...]`. It is active when its probe exits 0 or any entry has work in the run; its hooks then run at its normal position, after the entries, because packages run dependency-first whatever the input order. The hook must be idempotent: it runs whenever an entry has work, even when that work did not change what it acts on.

Entries come from the package's direct `depends`, so the coupling sits in the consumer's manifest next to the dependency that justifies it. The consumer acts only when it is in the run's scope.

Rejected:

- `run_noop = true` on the hook, or `--run-noop`: the hook runs on every push.
- `probe_after` (xian0x5a/dotman#358): re-ran the probe at execution right before the target's hooks, so the hooks ran only when needed. It cost a conditional row state, an execution `probe` step, executor changes, own-hooks-only activation and ordered planning, about 460 source lines for one consumer.
- A hook in the writer package (`agents.d_agents` `post_push`): the writer names its consumer, and on a machine without the consumer it creates the consumer's directory.
- Hooks a package declares on a dependency's target: the consumer still needs its own probe and hook to repair drift, so the same command lives twice, and a hook's owner differs from where it runs.
- Triggers inside `probe`, as a special command or a list: dotman cannot see a dependency hidden in a shell string to order or validate it, and a list of conditions reads as "all of".
- Names: `activated_by` and `triggered_by` read as the only condition; `probe_targets` collides with "probe target"; `additional_inputs` suggests the probe reads them. `additional_` matches editor `additional_sources`.
