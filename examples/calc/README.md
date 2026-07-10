# Example: `calc` — serial → parallel → serial, building something useful

This example shows relay handling **real dependencies**, not just a flat fan-out: a working
arithmetic calculator (`calc "2 + 3 * 4"` → `14`) built in three phases — **serial** foundation,
**parallel** extensions, **serial** integration. It exercises the whole toolkit: sequencing,
session reuse (`/relay:send`), parallel spawn, project-scoped wakes, and lead fan-in.

The only artifact here is [`BRIEF.md`](BRIEF.md) — the plan you hand the lead. No pre-built scaffold;
the lead writes the packets from the brief.

## Run it

**Setup** — shell:
```bash
rm -rf /tmp/calc && mkdir -p /tmp/calc && cd /tmp/calc && git init -q
```
Open a fresh Claude Code session with cwd `/tmp/calc`.

**1. Describe the work:**
> I want to build `calc` — the spec's in `<path-to-claude-relay>/examples/calc/BRIEF.md`. How would you approach it?

The lead should propose the phased plan: **serial** `tokenize` → `evaluate`, then **parallel**
`functions`/`constants`/`ops`, then it integrates `cli.py`. It shows each packet's goal + file path,
and waits.

**2. Approve + arm (bare):**
> yes
```
/relay:mode
```
✅ Tab renames `[lead] calc`; it presents the phased split (with packet paths) and waits.

**3. Phase 1 — serial:**
> go — start with tokenize

✅ One executor builds `tokenize.py`; when it reports, the wake shows a **brief of what it did**.
Review it, then have the lead **reuse the same session** for `evaluate.py`:
> looks good — now send it evaluate.py

(demonstrates `/relay:send` — reuse over re-spawn.)

**4. Phase 2 — parallel:**
> now fan out the three extensions

✅ Three executors (`functions`, `constants`, `ops`) build **at once**. Notifications name each
(`relay · calc / …-functions reported`), and clicking jumps to the tab. Review each.

**5. Phase 3 — integrate (lead):**
> wire up cli.py and run the tests

✅ The lead writes `cli.py`, runs `pytest`.

**6. Verify what you built** — shell:
```bash
cd /tmp/calc && python -m pytest
python -m calc "2 + 3 * 4"      # 14
python -m calc "sqrt(16) + pi"  # ~7.14
python -m calc "(2 + 3) * 4"    # 20
python -m calc "10 % 3"         # 1
```
✅ Green + a working calculator.

**7. Close out** — the lead offers to close the executors:
> yes, close them

## What this teaches (beyond `textkit`)

- **Serial dependencies** — `evaluate` can't start until `tokenize` is done and reviewed.
- **Session reuse** — `/relay:send` continues an existing executor instead of a cold spawn.
- **Parallel where it's actually parallel** — the three extensions are independent, so they fan out.
- **Fan-in** — the lead integrates the CLI and one `pytest` proves the whole thing composes.
- And it produces something you'd **actually use**.
