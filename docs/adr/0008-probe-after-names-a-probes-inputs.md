# A probe waits only for the inputs it names

Probes run during planning, before any write, so a probe that reads what the same run writes sees stale state and drops its hooks (xian0x5a/dotfiles#8: a skill added in a push was not linked, a removed one kept its link). A probe target now names those inputs with `probe_after = ["package", "package.target", ...]`. When a named input has work in the run, the probe skips planning, shows as conditional Probe Work (`Probe Work?`, `would-check`, JSON `conditional`), and runs once right before the target's own hooks. Otherwise it is probed at planning as before, because nothing earlier changes what it reads.

Entries must already run before the target: its own package (whose normal targets it follows) or its `depends` closure, without a cycle back. The target activates only its own hooks, because its package's and repo's pre hooks have run by its turn.

`probe_after` targets may wait for each other. Planning decides them after the other probes, one at a time in execution order, so each sees every row that runs before it, an earlier conditional row included, and none after it. Within a package they run in declaration order, so an entry naming one declared later is rejected. Without this, a chain needs a workaround that names the earlier target's own inputs instead, which goes stale when those inputs change.

Rejected:

- `run_noop = true` on the hook, or `--run-noop`: the hook runs on every push. That covers up the ordering instead of fixing it.
- A boolean that waits for the whole dependency closure: in the dotfiles history, unrelated `claude` and `agents` changes outnumber skill changes, so most `?` rows would be noise. A list is also the long-term shape, which avoids a later move from boolean to list.
- A post target that runs at the end of the whole run: packages that depend on it would run before it, and the row would be conditional on any change.
- A hook-level `reprobe = true`: when planning says 100 there is no row, so the hook is never scheduled. It also cannot cover pre hooks.
- Watched paths: blind to state written by hooks, such as skills installed through `npx skills`.
- Rechecking every probe after writes: always-on rows and twice the probe cost.
