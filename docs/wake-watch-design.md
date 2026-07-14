# Design doc: fixing the idle-lead wake for good — executor-side escalation

**Status:** DRAFT for review. Design converged; two empirical spikes gate the build (§7).
**Author:** lead session (claude-relay), synthesizing four incident reports, a code read, and a long
design conversation.
**Why this exists:** the idle-lead wake has been "fixed" at least four times and still fails. That
recurrence is the signal that the fixes have been treating symptoms of a structural problem. This
doc names the structure and proposes a fix that removes the *class* of bug, not the next instance.

---

## 1. The recurrence (the thing to explain)

The wake's job: when an executor files its `NNN-report.md` while the lead is idle (or gone), make
sure the report is *noticed* — surface it to the lead, and/or tell the human. It has failed,
silently, in at least six distinct ways:

| # | Vector | What actually stopped the wake | Source |
|---|--------|--------------------------------|--------|
| 1 | owner_lead re-parenting | report surfaced to the wrong/no lead after a handoff | wake bug #1 |
| 2 | stale `poll.lock`, dead pid | a leftover lock made a new poller think one was already running | wake bug #2 |
| 3 | old-version hooks | a stale-stamped session never armed the poller at all | wake bug #3 |
| 4 | `stalled` ≠ `busy` | long-running executors dropped out of the in-flight set the watch keys on | `wake-bug-stalled-not-inflight` |
| 5 | 30-min one-shot timeout | poller exited on deadline; re-arm requires a *lead turn* = human input | `RELAY-missed-wake-report` |
| 6 | lead process death / token exhaustion | the lead was killed → no process to run any poller | user report, this session |

These are not six unrelated bugs. They are six preconditions of a single mechanism, each of which,
when unmet, silently disables the whole thing.

---

## 2. Root structure (why patching hasn't converged)

### 2.1 The foundation is `asyncRewake`, and two of the vectors are *inherent to it*

relay's wake is built on **`asyncRewake`** — a Claude Code Stop-hook capability (spike-verified, see
`async-rewake-findings.md`). A `Stop` hook marked `"asyncRewake": true` runs its command in the
**background** when the lead goes idle; **exit 2** wakes the idle session (injects a fresh turn),
**exit 0** leaves it asleep. That is the "lead senses a report on its own" behaviour.

Its two documented constraints are exactly two of our failure vectors — they are **not relay bugs,
they are properties of the primitive**:

- *"The session process must still be alive… nothing wakes a dead session."* → **vector 6.** No edit
  to the poller can wake a dead lead.
- *"A hook only fires from a lifecycle event, not on its own."* → **vector 5.** An `asyncRewake` hook
  cannot self-re-fire; only the *next* `Stop` event re-arms it, and an idle lead emits none. So after
  the poller's window expires, re-arming needs human input.

### 2.2 Why it is a recurring-bug *generator*

The wake is **one in-process background poller** whose existence at any instant depends on ALL of:
(1) the report's executor is owned by this lead; (2) no stale lock; (3) hooks current enough to arm
it; (4) the executor still counted `busy`; (5) wall-clock inside the poller's budget; (6) the lead's
OS process alive. Two properties make this generate incidents rather than settle:

- **AND-of-many-preconditions.** Six independent things must hold. Fixing one leaves five able to
  fail — and the release history *is* the proof: each version removed one row from §1's table and the
  next incident came from a different row.
- **Fails silent.** When the poller isn't running, *nothing says so.* No signal in `relay list`, none
  on screen. The failure is found hours later by a human noticing a report that never pinged. **A
  silent failure with six triggers recurs indefinitely.**

Two numeric coincidences make the common case worse:

- `STALL_THRESHOLD_SECONDS = 1800` (`bin/relay:58`) **==** `poll_seconds = 1800`
  (`lib/lead_guard.py:35`). A long executor is flagged `stalled` (vector 4) at the *same instant* the
  poller times out (vector 5) — the watcher dies exactly when the executor becomes invisible to it.
  (Raising `poll_seconds` alone doesn't even help: `has_inflight_executors` stops counting the
  executor once it's `stalled`, so the poller exits early regardless of window.)
- `poll_seconds` (30 min) is *shorter* than a substantial packet's first-report time (engine change +
  remote suite routinely exceeds 30 min — ~30% of packets by the user's estimate). So vector 5 is the
  expected case, not an edge one.

### 2.3 The fact that unlocks the fix: only the executor is reliably alive, and the report file is the only durable signal

- **Heartbeat is lead-only.** `last_active` is stamped by `touch_lead` once per *lead* turn (Stop
  hook). **Executors do not self-heartbeat.** The one thing an executor does autonomously to disk is
  write `NNN-report.md`.
- Therefore **the report file on disk is the only durable, process-independent signal** that an
  executor finished. It survives the executor pausing, the lead idling, tokens dying — everything.
- And critically: **the executor is guaranteed alive at report time** — it just took the turn to write
  the report. Every past design tried to keep a *lead-side or external* watcher alive across the gap;
  the executor is already alive at exactly the moment a report exists. **Put the watch there.**

---

## 3. What a fix must achieve (goals)

1. **Fail loud, not silent.** Whatever watches, its liveness/absence must be visible.
2. **Fewer preconditions.** Reduce the AND-of-six; design vectors away rather than patch them.
3. **Survive the lead.** The human-ping half must not depend on the lead process being alive.
4. **Idempotent, dedup-safe.** Surface each report exactly once; never re-announce handled work; a
   missed notification must lose nothing.
5. **Keep the good fast path.** The instant `asyncRewake` wake when the lead IS alive/idle/in-budget
   already works — keep it, add a net under it.

---

## 4. The design: executor-side escalation

**Relocate the watch into the executor** — the one process definitionally alive when a report exists.

### 4.0 Ground truth: a Claude session takes a turn ("wakes") in exactly three ways

1. **Input typed into its tab** + Enter — by a human, OR by another process via iTerm `write text`
   (this is how `relay send` delivers packets to executors, `scripts/iterm.py:424`).
2. **Its own `asyncRewake` hook exits 2** — the harness injects a turn.
3. **Its own `ScheduleWakeup`/`/loop` timer fires.**

There is **no** native "one session messages/wakes another idle session" primitive (the research-
preview `notifications/claude/channel` used by the unrelated `vildanbina/claude-relay` only delivers
at a turn boundary — it does **not** wake an idle session; see §5). relay's cross-session wake has
always been **way #1: type into the tab.** That means **"wake the lead" == `relay send` pointed at the
lead's `[Lead] <project>` tab instead of an executor's** — same proven mechanism, same busy-guard.

### 4.1 The loop

When an executor's report lands, an executor-side watcher (an `asyncRewake` Stop hook on the
executor — mirroring the lead's) runs in the background and:

```
executor writes NNN-report.md   (executor is alive — it just wrote it)
  → GRACE (~60s): let the lead's own fast-path win if it's healthy
  → is my report's key in the owning lead's surfaced_reports.json?
       yes → the lead already noticed. done.
       no  → read the OWNING lead's turn-state:
              idle        → nudge the lead's tab (relay-send-in-reverse) → lead wakes, surfaces
              busy        → WAIT (a busy lead surfaces the report at its next Stop, see 4.3);
                            re-check next cycle; escalate to human only on TIMEOUT
              stale-busy  → lead is wedged/crashed mid-turn → NOTIFY HUMAN now
              (dead/gone) → NOTIFY HUMAN
  → re-notify on widening backoff until handled (or executor is closed)
```

### 4.2 Lead busy/idle tracking (the one new piece of state)

The lead already has a Stop hook (turn-end) and a PreToolUse hook. Add a **turn-start** stamp so the
lead has a real busy/idle, symmetric to executors:

- **turn start** (`UserPromptSubmit` hook, or reuse PreToolUse) → write `state: busy` + timestamp to
  the lead's marker.
- **turn end** (existing Stop hook, where `touch_lead` already runs) → write `state: idle`.

The executor is "mindful" of this exactly like `relay send` is with executors: only nudge a lead
whose state is `idle`. The existing send-guard — *"refusing to send (would inject mid-turn). Only
reported/idle sessions are safe send targets"* (`bin/relay:756`) — is the same discipline.

**The one non-negotiable rule: `stale-busy` escalates to the human, never waits forever.** A lead that
crashed mid-turn leaves `state: busy` frozen. If the executor waits on `busy` unconditionally, a
dead-mid-turn lead silently swallows the report again — the original bug in a new outfit. So "busy but
the stamp is older than N minutes" → treat as wedged → notify the human.

### 4.3 Why `busy` is patient, not an alarm

A busy lead usually self-heals: when it finishes its current turn, its Stop hook's *synchronous*
fast-path (`stop_lead_watch.py:196`, `_report_lines`) surfaces any report already present at that
Stop. So `busy` is not a lost report — it's caught at the lead's next turn-end. Escalation on `busy`
is therefore a **timeout safety net** ("this has waited ~Nm and the lead still hasn't gotten to it —
your call"), not an immediate ping. Tone: informational, not alarm. No spam on momentary busy.

### 4.4 Dedup and the missed-notification floor

- **Two separate ledgers.** The executor's "I notified the human" state must be distinct from the
  lead's `surfaced_reports.json` — otherwise the executor pinging the human would consume the lead's
  own announcement and the lead would stay silent when you return. Use a separate `notified_human`
  record.
- **Notifications are never the source of truth.** A missed banner loses nothing: (a) the executor
  re-notifies on backoff until handled; (b) the report file + the executor's `reported` status persist
  on disk forever; (c) **reconcile-on-return** — any `relay` command / the lead's next turn surfaces
  anything still unhandled. Correctness rests on durable disk state; every notification is best-effort.

---

## 5. Why this over the alternatives

| Approach | Verdict |
|---|---|
| **Bigger `poll_seconds` (e.g. 2h)** | Cheap mitigation for the *common* vector (5), ~95% — but doesn't touch vectors 1–4/6, doesn't cover token-pause (unbounded), needs the hook-timeout raised (untested ceiling) AND the stall-coupling fixed. Reduces frequency; doesn't end recurrence. |
| **launchd tick (external poll)** | Robust (OS-owned liveness, survives lead death) but external machinery, can't drive the lead without tab-inject anyway, and needs its own dedup. Heavier than executor-side for the same result. |
| **`/loop` on the lead (`ScheduleWakeup`)** | Native, self-re-arming (dodges vectors 2/5), but burns *lead* tokens + pollutes lead context every tick, and dies with the lead. |
| **`/schedule` (cloud routines)** | Wrong location: runs in Anthropic's cloud, can't read local `~/.relay-tasks/` files or fire a local notification / touch local iTerm. Ruled out by locus. |
| **`notifications/claude/channel`** (prior art: `vildanbina/claude-relay`) | Different project, different problem (live peer Q&A). Its capability delivers only at a turn boundary — does **not** wake an idle session — and it has no persistence across disconnect (hub self-terminates 5 min after peers leave). *Less* durable than our report-file-on-disk for long-gap delegation. Confirms there's no native easy button. |
| **Executor-side escalation (this doc)** | Watch lives in the process guaranteed alive when a report exists; covers all six vectors for the notify-human job; wakes the lead via the already-proven `relay send` mechanism; no launchd, no cloud, no `/loop` token burn. |

---

## 6. Cheap correctness fixes that stand regardless

- **Count `stalled` as in-flight** in `has_inflight_executors` (`lib/lead_guard.py:673`) — a long-but-
  alive executor is the *most* likely to report while the lead idles; excluding it is backwards.
- **Decouple `STALL_THRESHOLD_SECONDS` from `poll_seconds`** so vectors 4 and 5 stop firing on one
  clock.
- **Evidence-based stall** (pid gone OR session.json untouched N min) instead of pure duration, so a
  long-running-by-design e2e never ages out of observation for taking its time.

These are low-regret even alongside the executor-side redesign; land them as part of it.

---

## 7. Spikes to run BEFORE building (two testable unknowns)

1. **Does a turn-start hook fire on an `asyncRewake` wake?** When the lead's own poller wakes it
   (exit 2), does that injected turn trigger `UserPromptSubmit` (so `state: busy` gets stamped)? If
   not, the busy stamp may miss wake-turns — need a fallback signal. Testable with a trivial hook.
2. **Does the executor reliably stay alive the ~60s to run its check?** After writing the report the
   executor goes idle; its `asyncRewake` background hook must survive that idle long enough to check.
   Confirm the executor's tab/process persists (it does until closed) and the hook's background
   process isn't killed early (the same `timeout` lesson as the lead's — `async-rewake-findings.md`).
   If an executor can be killed immediately post-report, reconcile-on-return (§4.4) is the backstop.
   **ANSWERED (2026-07-14): SURVIVES** — confirmed live, marker-file timestamps cross-checked
   against transcript timestamps to the second. See `async-rewake-executor-findings.md`.

---

## 8. Non-goals / accepted-unsolvable

- **Lead itself out of tokens** (vector 6, true form): if the *lead* is dead, no primitive wakes it —
  the human resumes it and reconcile-on-return surfaces everything. Explicitly accepted as manual.
- **Interactive-executor / ops-context-pollution** idea — separate feature, separate doc.
- The already-shipped executor-model policy (`executor_default_model`/`_ceiling`, 0.3.18) — done.

---

## Appendix — code anchors

- `asyncRewake` poller + arm + timeout: `hooks/stop_lead_watch.py:225-242`; synchronous fast-path
  `:184-220`.
- In-flight set (vector 4): `lib/lead_guard.py:656-682` (`has_inflight_executors`, the `!= "busy"`
  skip).
- Stall verdict / shared clock: `bin/relay:882-897`, `STALL_THRESHOLD_SECONDS` `bin/relay:58`.
- Poll budget: `poll_seconds`/`poll_interval` `lib/lead_guard.py:35-36`; hook `timeout: 1900`
  `hooks/hooks.json`.
- Cross-session wake mechanism (reuse for the lead-nudge): `scripts/iterm.py:424` (`iterm.send`);
  send busy-guard `bin/relay:756`.
- Report surfacing + dedup: `lib/lead_guard.py:529` (`mark_surfaced`), `:587` (`new_reports_for`),
  `surfaced_reports.json`.
- Lead heartbeat: `lib/lead_guard.py:380` (`touch_lead`); marker fields `:301-330`.
- Notification path (human-facing, survives independently): `hooks/stop_lead_watch.py:40` (`_notify`).
