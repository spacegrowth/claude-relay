# Example: `textkit` — the real relay flow, end to end

A hands-on walkthrough of how relay is actually used: you **agree on a plan**, say **`/relay:mode`**,
and the lead **decomposes the work into parallel packets, delegates, reviews, and integrates**. The
thing built is a tiny stdlib-only Python CLI — `textkit slug | count | palette` — so the whole run
is cheap and verifiable with `pytest`.

> Rough cost: **~250–450k tokens** (3 executors on a small module each + the lead's
> review/integration). Estimates swing with how much each executor iterates.

The only artifact here is [`BRIEF.md`](BRIEF.md) — **the plan you hand the lead** (goal + the exact
interfaces the parallel modules must honor + acceptance). There is deliberately no pre-built
scaffold and no pre-written packets: **the lead produces those from the brief** — that's the part
relay exists to do.

## Run it

**1. Start a session in a *fresh empty* dir** (a real project, or a clean demo dir — not a
leftover one) and **agree on the plan first**. Ask it to *propose* a split — don't tell it to
delegate yet:

> **You:** I want to build `textkit` — the spec's in
> `<path-to-claude-relay>/examples/textkit/BRIEF.md`. How would you approach it?
>
> **Claude:** *(reads the brief)* It's three independent modules (`slug`/`count`/`palette`) plus a
> CLI dispatcher. I'd build the three in parallel, then integrate `cli.py` myself and run pytest.
> Want me to lead it and delegate?
>
> **You:** yes — looks right.

Note it should **propose and wait here** — it should NOT arm lead mode or spawn anything yet. That's
your call in the next step.

**2. YOU become the lead** — type this yourself (it's your deliberate switch into delegation mode;
the assistant shouldn't self-invoke it). Bare — the project auto-names from the directory:

```
/relay:mode
```

Only now does it delegate. **Order is flexible** — you can also `/relay:mode` *first* and then say
what to build; the lead will propose the split and wait for your go either way.

**3. The lead delegates.** It writes three packets from the brief and spawns three executors in
parallel (same dir, independent files — no conflict). You don't pre-write anything. Watch it:

```
relay list
```
```
LEADS
  PROJECT   SESSION           MODEL  LAST ACTIVE
  textkit   abc1234d-…        opus   just now

EXECUTORS
  SESSION    STATUS   TOPIC     SCOPE   PROJECT   MODEL  PKT   REPORTED
  tk-slug    busy 1m  slug      …       textkit   opus   001   no
  tk-count   busy 1m  count     …       textkit   opus   001   no
  tk-palette busy 1m  palette   …       textkit   opus   001   no
```

**4. The lead auto-wakes as each executor reports** (scoped to *this* project — no cross-wake from
other leads):

> **Claude (lead):** 🚦 [relay] — review needed: `tk-slug` reported. (…then `tk-count`, `tk-palette`.)

**5. Review + integrate.** Tell the lead to review; it checks each report + staged module against
the brief's interfaces — each executor's closing line includes a clickable file:// link to its diff
page — then writes `cli.py` (the fan-in) and runs the suite:

```
python -m pytest                       # module tests + the integrated CLI → green
python -m textkit palette sunset --n 3
```

Green = the parallel build and the fan-in both worked.

## If an executor's tab dies mid-build

```
relay resume  tk-count     # reopen the same conversation, context + staged work intact
relay restart tk-count     # or re-run its packet fresh (loses context)
```

## What this teaches

- **The real flow** — agree on a plan → `/relay:mode` → the lead decomposes and delegates. No
  scaffolding ritual.
- **Fan-out** — three executors building genuinely-independent work at once.
- **A shared contract** — the brief's interfaces are what keep three parallel builds composable.
- **Project-scoped awareness** — `relay list` shows this lead's project; wakes are scoped to it.
- **Fan-in** — the lead integrates the pieces and a single `pytest` proves they compose.
