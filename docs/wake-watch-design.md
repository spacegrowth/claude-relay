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

## 9. REVISION (2026-07-19) — push, not watch. §4 is superseded.

**Status of this section:** proposed, on branch `wake-push`. Nothing on `main` changes until this is
proven. §4's executor-side *watcher* shipped as 0.3.21; this section argues it was the right
*location* but the wrong *shape*, and proposes replacing it with a push.

### 9.1 Why revise something we just shipped

§2.3 got the key fact right — *the executor is the one process guaranteed alive when a report
exists* — but §4 then built a **watcher** on the executor (grace window, poll loop, backoff, ledger,
lock, six-branch decision tree) instead of the obvious thing: **just send a message.**

The tell is where the complexity lives. Nearly all of it exists to manage a **race between two
mechanisms**:

- the grace window = "let the lead's own poller win first",
- the separate ledger = "don't double-notify what the lead already surfaced",
- the decision tree = "what state is the lead in right now",
- the backoff = "don't spam while we keep re-checking".

**Delete the second mechanism and all of that evaporates.** There is no race to manage if only one
thing is responsible for delivery.

### 9.2 The design

`relay send` (lead types into an executor's tab) is relay's most reliable primitive — it is how
every packet is delivered. The push is that same primitive, reversed:

```
executor's Stop hook fires (harness event, on idle)
  → is there a report for my current packet?      no  → exit 0
  → have I already sent for this packet?          yes → exit 0
  → type a message into the owning lead's tab  (relay send, in reverse)
  → mark sent. exit 0.
       (tab gone / no owner → notify the human instead — the ONE fallback)
```

That is the whole mechanism. No polling. No grace. No backoff. No window to expire. No lock to go
stale. No in-flight set to fall out of. Vectors 1–5 of §1 stop being *possible* rather than being
*patched*, because none of their preconditions exist anymore.

### 9.3 The one hard constraint: hook-triggered, never prompt-triggered

This is the whole design. The *action* is identical either way; the *trigger* determines the
reliability class.

- **Prompt-triggered** — the packet instructs the executor's model *"after writing your report, run
  `relay nudge-lead …`"*. This is **compliance, not mechanism**: it works most of the time and fails
  silently the rest. Evidence from the session that produced this revision: the packet footer says,
  in bold, *"STAY IDLE AFTER REPORTING — do NOT exit"*, and an executor closed its own tab anyway;
  another was told to clean up its throwaway tabs and two lingered. **Do not build on this.**
- **Hook-triggered** — the harness fires the executor's `Stop` hook on idle. The model is not
  consulted and cannot forget. Deterministic by construction.

Residual prompt-dependency, honestly named: the executor's model must still **write the report
file**. That is unchanged from today, and it fails *loudly* (no file = obviously not done), unlike a
skipped nudge which is invisible. We are converting an invisible failure into an already-visible one.

### 9.4 `asyncRewake` mostly goes away

`asyncRewake` exists to run a hook in the **background for a long time** and wake the session on exit
2. The push needs neither half: the executor's hook is a few hundred ms of osascript, synchronous,
exit 0. With the lead-side poller gone there is no long-running watcher to host.

Everything downstream of background execution goes with it: the `1900s` hook `timeout`,
`poll_seconds`, the `poll_seconds < hook-timeout` coupling rule, the 30-minute cliff, and the
"silent auto-wake death" failure mode (a killed background process leaving a stale lock).

### 9.5 What gets deleted

**Dies completely:**

| Component | Why |
|---|---|
| `hooks/executor_escalation.py` (~206 lines) | Replaced by a ~20-line synchronous send |
| `escalation_decision` (6-branch tree) | Only consumer was that hook |
| escalation ledger (`load/save_escalation`, `escalation.json`) | Only consumer was that hook |
| escalation lock (`acquire/heartbeat/release_escalation_lock`) | No long-running process to serialize |
| lead-side background poller in `stop_lead_watch.py` | Push replaces pull |
| `poll.lock` machinery (`acquire/heartbeat/release_poll_lock`, `poll_lock_state`) | Only existed to serialize that poller |
| `has_inflight_executors` | Usage-traced: its only consumers are the poller's two call sites |
| `stalled`-counts-as-in-flight + `stall_threshold_seconds` decouple | Only mattered because `has_inflight_executors` gated the poller |
| Config: `poll_seconds`, `poll_interval`, `executor_escalation_grace_seconds`, `_poll_interval`, `_max_runtime_seconds` | Nothing left to tune |
| `WAKE` column's `stuck` state | Derived from `poll_lock_state` |

**Shrinks / becomes optional:** `surfaced_reports.json` + `mark_surfaced`/`new_reports_for` (a
lead-side dedup ledger becomes a one-bit "already sent" flag on the executor); `lead_turn_state` +
`stamp_lead_state` + the `UserPromptSubmit` hook (they exist to power `nudge-lead`'s busy-guard —
see 9.6); `relay nudge-lead` itself is **kept and simplified**.

**Stays:** `_notify` (the one fallback — lead's tab gone → tell the human); the lead Stop hook's
*other* jobs (commit surfacing, handoff nudge, `touch_lead`); `relay send` / id-based addressing /
the tab-label fix, which are the transport the push rides on.

### 9.5b SPIKED (2026-07-19): typing into a BUSY lead is safe — the busy-guard is dead weight

The biggest open question was whether a push can just *always send*, or must detect that the lead is
mid-turn and wait. **Tested directly, in both busy modes:**

| Lead state when text was injected | Result |
|---|---|
| Blocked on a foreground tool call (~35s busy-wait) | ✅ queued in the input box, processed cleanly at turn-end |
| Mid-token-stream writing 1200 words of prose | ✅ queued; the 8,495-char reply completed **intact**, then the injected message was answered |

Ordering from the second run's transcript, the conclusive one:

```
[USER]       118 chars  Write a detailed 1200-word essay…
[ASSISTANT] 8495 chars  # The History of Tea Cultivation…   ← COMPLETE, uncorrupted
[USER]        52 chars  STREAMTEST: reply exactly STREAM-OK ← injected mid-stream
[ASSISTANT]    9 chars  STREAM-OK
```

Injection happened while the prose was visibly mid-sentence, and the essay still finished in full —
so the session was genuinely streaming, not idle. **No interruption, no truncation, no interleaving.**

**Consequence: the busy-guard never protected anything, and actively caused misses.** Refusing to
nudge a busy lead doesn't avoid damage — it just fails to deliver, which IS the bug. The rule becomes
**always send**, and all of this becomes deletable:

- `lead_turn_state` (idle/busy/stale-busy) — `lib/lead_guard.py`
- `stamp_lead_state` + the `UserPromptSubmit` hook that stamps `busy` at turn-start
- `nudge-lead`'s busy/stale refusals (`bin/relay`) — they manufacture the miss
- the escalation's `wait` branch, `BUSY_WAIT_TIMEOUT_SECONDS`, the backoff schedule, and the
  `stale-busy` → notify-human branch

It also **retires an unanswered question instead of answering it**: whether `UserPromptSubmit` fires
on an `asyncRewake`-injected turn (§7 spike #1, never run) stops mattering once nothing reads the
busy stamp.

> **Corroborating, independently checked:** Claude Code exposes **no** way to query whether a session
> is busy from outside — no CLI command, no state file, no IPC; transcripts are append-only JSONL in
> an explicitly internal format. relay built a whole subsystem to derive a signal the harness doesn't
> publish *and* that it turns out not to need.

**Residual limit, stated honestly:** both tests injected via `iterm.send` into an iTerm session.
Terminal.app's backend uses a different addressing path and was not re-tested.

**Methodology note (this bit us):** the FIRST attempt at the busy test was invalid — the task was
`sleep 40 && echo`, which the harness blocked, so the session re-ran it with `run_in_background` and
went idle immediately. The tab title still *looked* busy. Same false-positive shape as the
"Not enough messages to compact" decline in `lead-arming-durability.md` §11. Any re-run must confirm
the session is genuinely mid-turn, not merely activity-glyphed.

### 9.6 Open questions to settle before building

1. ~~**Busy lead: send anyway, or guard?**~~ — **SETTLED by spike, see §9.5b. Always send.** The
   busy-guard protected nothing and caused misses; `lead_turn_state`, the `UserPromptSubmit` stamp,
   and the escalation's wait/backoff/stale branches are all deletable.
2. ~~**Delete the lead-side fast path, or keep it?**~~ — **SETTLED: KEEP BOTH PATHS.** The lead's
   synchronous at-Stop check stays. Rationale is a product one, not a technical one: *it is worth
   seeing the LEAD say "I found something" rather than always hearing it second-hand from the
   executor.* The lead noticing its own executor's work is the natural, legible thing; the push is
   the safety net beneath it.

   This does reintroduce the dedup question the "one mechanism" option would have avoided — but the
   answer already exists and is cheap: the executor checks whether its report key is in the owning
   lead's `surfaced_reports.json`, and the ~60s grace window gives the lead first crack. So keeping
   both paths is safe *because* the executor can tell the lead already picked it up. See 9.6a.

3. **Executor spawned by an older relay has no hook, permanently** — it cannot be retrofitted into a
   running session (this is exactly the `fix-dcompose` incident). Do we detect and surface
   un-armed in-flight executors so the gap is *visible* rather than silent?
4. **Report written but executor killed before its Stop fires** — narrow, but real. Is
   reconcile-on-return (any relay command surfaces unhandled reports) enough as the floor?

### 9.6a `resolved` must SAY so, not exit silently

Today, when the executor's watcher finds the report already in the lead's `surfaced_reports.json`,
`escalation_decision` returns `resolved` and the hook simply exits. Correct behaviour, invisible
execution.

Given everything this document is about, a silent non-event is the wrong default. The executor
should record/announce **"lead already picked this up (packet NNN)"** rather than vanishing. Two
reasons:

1. **It is the dedup working.** With both paths kept (9.6 #2), `resolved` is the mechanism that stops
   double-announcing — the single most important thing to be able to *observe* when diagnosing "did
   the wake work?". Silence makes a working dedup and a dead hook look identical.
2. **It closes the diagnostic gap that made this whole investigation slow.** Every incident in §1 was
   hard to attribute because the non-events left no trace. `lead_tombstoned` / `lead_rearmed` in the
   ledger proved their worth immediately (they are how the arming fix was verified in production);
   `escalation_resolved` earns its keep the same way.

Cheap: one ledger line. No behavioural change, no new failure mode.

### 9.6b `source="fork"` — a FIFTH silent-unarm route, observed live (2026-07-19)

A background session forked from this lead's transcript took over the `claude-relay` lead role, and
this session lost it **with no signal** — discovered only because the user asked why a message went
unanswered.

```
10:23:27  384c39f1  lead_stepped_down
10:23:32  c65b2bca  lead_started        ← predecessor: None → NOT a handoff
```

The fork was launched as:

```
claude.exe --bg-pty-host …/pty/c65b2bca.sock
claude --session-id c65b2bca --fork-session --resume …/384c39f1.jsonl --reply-on-resume
```

**Spiked directly**, and it produced an undocumented `source` value:

| Invocation | `session_id` | `source` |
|---|---|---|
| fresh start | A | `startup` |
| `claude --resume A` | **A (preserved)** | `resume` |
| `claude --fork-session --resume <transcript>` | **NEW id** | **`fork`** |

Two things follow:

- **The docs' `source` list (`startup\|resume\|clear\|compact`) is incomplete — `fork` is a fifth
  value.** Third doc-vs-reality gap this project has hit; consistent with the rule of spiking rather
  than trusting.
- **Our arming design is *correct* here but incomplete.** A fork has a new id, so no tombstone exists
  for it and `sessionstart_lead_rearm` rightly no-ops (`fork` ∉ `REVIVE_SOURCES`). A fork inheriting
  lead arming would be worse — two live leads for one project. But the fork can still *separately*
  arm itself, which is exactly what happened, and the original then holds a stale belief that it is
  the lead while `is_lead` says otherwise.

**Implication (not yet built):** `source="fork"` deserves an explicit branch that says so — the fork
is a different conversation and is NOT the armed lead, and staying silent about that is what cost an
hour here. Note the auto-suffix DID behave correctly once the original re-armed (`claude-relay` was
taken by the live fork → `claude-relay-2`), which is how the collision became visible at all.

### 9.7 How this gets tested without touching `main`

- relay resolves the executor hook path from **whichever `bin/relay` is invoked**, so running
  `./bin/relay spawn …` from the `wake-push` checkout arms executors with **the branch's** hooks —
  no install, no effect on `main`.
- The plugin marketplace is `{"source": "github", "repo": "spacegrowth/claude-relay"}` with **no
  branch field**, pinned to `main` — so `/plugin install` cannot pull a branch. If lead-side hook
  changes need real-install testing, add the **local checkout** as a marketplace
  (`/plugin marketplace add $HOME/dev/claude-relay`) so the checked-out branch is what runs.

---

## 10. Arming durability — SOLVED, moved out

While drafting §9 we found a problem *upstream* of the wake: a routine quit (`prompt_input_exit`)
deleted the lead marker, so quit→resume returned a silently **unarmed** session — gate off, wake
structurally impossible, and the model still believing it was the lead. "The wake didn't fire" and
"nothing was armed" are indistinguishable from outside, so an unknown share of past wake incidents
may have been this rather than the wake.

It was split onto its own track precisely because it was smaller and more certain than the wake
rework, and it **shipped in 0.3.23–0.3.25**: `SessionEnd(exit|prompt_input_exit)` tombstones instead
of deleting, `SessionStart(resume)` re-arms losslessly (name preserved), and armed/paused is now
visible in `relay status` / `relay list`.

**Full write-up, evidence and spike results: [`lead-arming-durability.md`](lead-arming-durability.md).**
Kept here only as a pointer — that document is the single source of truth.

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
