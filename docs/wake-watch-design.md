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

### 9.6 Open questions to settle before building

1. **Busy lead: send anyway, or guard?** Claude Code *queues* typed input and submits it at
   turn-end, so sending into a busy lead is probably harmless and arguably correct. Today's
   `nudge-lead` *refuses* when busy, which manufactures a miss. If we send-always, `lead_turn_state`
   and the `UserPromptSubmit` hook stop being load-bearing. **Needs a live check.**
2. **Do we delete the lead-side fast path entirely, or keep the synchronous at-Stop report check?**
   Keeping it is cheap and covers "report already present when the lead ends a turn", but it
   reintroduces a second mechanism (and therefore dedup). Leaning: delete, keep one path.
3. **Executor spawned by an older relay has no hook, permanently** — it cannot be retrofitted into a
   running session (this is exactly the `fix-dcompose` incident). Do we detect and surface
   un-armed in-flight executors so the gap is *visible* rather than silent?
4. **Report written but executor killed before its Stop fires** — narrow, but real. Is
   reconcile-on-return (any relay command surfaces unhandled reports) enough as the floor?

### 9.7 How this gets tested without touching `main`

- relay resolves the executor hook path from **whichever `bin/relay` is invoked**, so running
  `./bin/relay spawn …` from the `wake-push` checkout arms executors with **the branch's** hooks —
  no install, no effect on `main`.
- The plugin marketplace is `{"source": "github", "repo": "spacegrowth/claude-relay"}` with **no
  branch field**, pinned to `main` — so `/plugin install` cannot pull a branch. If lead-side hook
  changes need real-install testing, add the **local checkout** as a marketplace
  (`/plugin marketplace add /Users/vamsi/development/claude-relay`) so the checked-out branch is what runs.

---

## 10. Arming is not durable across exit→resume (upstream of everything in §9)

**Status:** finding + proposed fix, on branch `wake-push`. Discovered live while writing §9.

### 10.1 What happens

`hooks/sessionend_lead_cleanup.py` clears the lead marker when `SessionEnd` fires with a reason in:

```python
REAL_END_REASONS = {"clear", "logout", "prompt_input_exit", "exit"}
```

`prompt_input_exit` — quitting from the prompt — is **the most common way a human leaves a session**.
Claude Code sessions are *resumable*: `--resume` restores the same session id **and the full
conversation**. So the routine cycle *quit → resume* deletes the lead marker and brings the session
back **unarmed, silently.**

### 10.2 Evidence (this session's own ledger)

```
07-13 09:15:39  lead_started                                          ← armed
07-14 06:20:09  session_end  reason=prompt_input_exit  was_lead=TRUE  ← UNARM #1
07-14 07:25:02  lead_started                                          ← re-armed BY HAND (masked it)
07-14 20:10:29  session_end  reason=prompt_input_exit  was_lead=TRUE  ← UNARM #2 (stuck)
07-14 20:10:58  session_end  reason=prompt_input_exit  was_lead=false ← nothing left to clear
07-17 19:04:01  session_end  reason=prompt_input_exit  was_lead=false ← still unarmed
```

No corruption, no reload churn, no prune (the only `pruned` events are executors from 07-11). **The
hook did exactly what it was written to do.** It happened *twice*; the first time it was manually
re-armed, which hid the problem.

### 10.3 Why this is upstream of §9

If the lead is not armed then, for that session:

- the **routing gate** never fires (`pretool_route_guard` fast-exits on `is_lead`),
- the **wake is structurally impossible** — `stop_lead_watch.py` fast-exits on `is_lead`, so no
  report can wake anything, no matter how well §9 is built,
- **ownership breaks** for anything spawned afterward (`owner_lead` points at a sid with no marker →
  the `owner-missing` branch).

So **"the wake didn't fire" and "nothing was armed" are indistinguishable from the outside.** Some
share of the recurring "the wake missed again" reports may be this, not the wake. Any wake redesign
that doesn't fix arming durability is building on sand.

Worse, there is a split-brain: **the model still believes it is the lead** (its conversation context
says so — it keeps announcing, proposing packets, spawning executors) while relay's on-disk truth
says it is not. Nothing reconciles them. In this session the discrepancy surfaced only by accident,
when a `route retain` call happened to error.

### 10.4 The reason-bucket error

The four reasons are not equivalent:

| Reason | What happens to context | Should it unarm? |
|---|---|---|
| `clear` | conversation wiped — model returns with **no lead context** | **Yes.** Staying armed would mean gate+wake on a model that has no idea why |
| `logout` | session genuinely over | **Yes** |
| `exit` / `prompt_input_exit` | **resumable** — same id, full conversation restored | **No.** This is a *pause*, not a death |

Lumping them together is the bug. The current code treats a **pause as a death**.

### 10.5 The principle

**Liveness should be derived, not destroyed.** relay *already* works this way everywhere else:
`relay list` renders a `LAST ACTIVE` age so a probably-dead lead is *visible*; `relay prune` sweeps
genuinely-old ghosts; unique-naming ignores stale leads (`LEAD_LIVE_WINDOW_SECONDS`); and a
**crashed** lead's marker is deliberately *preserved* — that is the entire basis of `/relay:resume`
for a crashed lead.

Which exposes an inconsistency: **crash → marker survives → resume comes back armed. Clean exit →
marker deleted → resume comes back unarmed.** The tidier exit path is the one that loses state.

### 10.6 The `SessionStart` hook exists, and it carries `source`

relay registers only `PreToolUse`, `UserPromptSubmit`, `Stop`, `SessionEnd`. **`SessionStart` is a
supported event relay simply doesn't use** — confirmed on disk (Anthropic's own
`learning-output-style` plugin ships one; same `{matcher, hooks:[{type:"command", command}]}` shape).

Documented payload (`command` hooks get full stdin and may run any shell command):

```json
{ "session_id": "...", "transcript_path": "...", "cwd": "...",
  "hook_event_name": "SessionStart",
  "source": "startup" | "resume" | "clear" | "compact",
  "model": "...", "agent_type": "...", "session_title": "..." }   // last three optional
```

Two facts make the fix trivial:

- **It fires on `--resume` / `--continue` / `/resume`.**
- **`--resume` preserves the same `session_id`**, so per-session state keys straight through.

> **Verification status:** the above is from the official hook docs, **not yet empirically confirmed
> on this machine's Claude Code build.** Given this project's history of doc-vs-reality gaps, prove it
> with a throwaway `SessionStart` hook that just logs its payload, exactly like the `asyncRewake`
> spike (`async-rewake-findings.md`). Cheap, and it de-risks the whole section.

### 10.7 Proposed fix — a closed state machine

`SessionEnd.reason` and `SessionStart.source` are complementary, which turns arming into a proper
lifecycle instead of a one-way delete:

| Event | Value | Action |
|---|---|---|
| `SessionEnd` | `clear`, `logout` | **hard-clear** — context is genuinely gone |
| `SessionEnd` | `exit`, `prompt_input_exit` | **tombstone** (`ended: true` + ts) — a pause, not a death |
| `SessionStart` | `resume` | **re-arm** from the tombstone (or warn loudly) |
| `SessionStart` | `clear` | stay unarmed — the model has no lead context to resume |
| `SessionStart` | `startup`, `compact` | no-op |

Three properties worth stating:

1. **It's hook-driven end to end** — no instruction to any model, consistent with §9.3.
2. **The `source` field is a refinement, not a prerequisite.** Even without it, the hook could just
   ask *"is there a tombstone for this sid?"* — disk state already answers "was this a lead". `source`
   lets us be precise about `clear` vs `resume` rather than inferring.
3. **Minimum bar is loudness, not automation.** `sessionend_lead_cleanup` already writes
   `was_lead: true` to the ledger at the exact instant it unarms a lead — the warning information
   existed and went into a log nobody reads. Auto-re-arm is the convenience; **visibility is the
   requirement.**

### 10.8 Relationship to §9

§9 makes report-delivery deterministic. §10 makes *being a lead at all* durable. **§10 must land
first or alongside** — a perfect push mechanism still delivers nothing if the receiving session was
silently unarmed by a routine quit.

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
