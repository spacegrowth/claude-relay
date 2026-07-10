# Example: `mini` — the cheap serial → parallel → serial smoke test

The smallest run that still exercises relay's whole shape: a **serial** foundation, then **two
parallel extensions** — one delivered by **reusing** the first executor (`/relay:send`), one by a
**fresh spawn** — then the lead integrates and runs `pytest`. Use it to sanity-check an install or
demo the flow without spending real money: executors on `--model haiku`, each module ~15 lines.

The only artifact is [`BRIEF.md`](BRIEF.md) — the plan you hand the lead.

## Run it

```bash
rm -rf /tmp/textops && mkdir -p /tmp/textops && cd /tmp/textops && git init -q
```
Open a fresh Claude Code session there (plugin loaded), then:

> I want to build `textops` — the spec's in `<path-to-claude-relay>/examples/mini/BRIEF.md`.
> Use haiku executors. How would you approach it?

`/relay:mode`, approve its proposed split, and watch for, in order:

1. **Serial**: one executor builds `core.py`; the lead wakes you when it reports.
2. **Parallel + reuse**: on your go, the lead `/relay:send`s `stats.py` into the SAME executor and
   spawns a fresh one for `shout.py` — both busy at once in `relay list`, tabs sharing the lead's
   color (iTerm). For a tighter cluster, executors can open as split panes (`--pane` / `"executor_layout": "pane"`, iTerm only).
3. **Fan-in**: the lead writes `cli.py` (+`__main__.py`), runs `pytest`.

Verify:
```bash
cd /tmp/textops && python -m pytest && python -m textops stats "Hello   world"   # 2
```

Close out: the lead offers to close both executors; say yes and the tabs go away.
