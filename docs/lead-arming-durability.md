# Lead arming is not durable across exit→resume

**Status:** finding + proposed fix. Branch `lead-arming`, based on `main`, **intended to land on
`main` once verified.**

**Deliberately scoped apart from the wake redesign.** The push-vs-watch wake rework lives on branch
`wake-push` (`wake-watch-design.md` §9) and needs extensive live testing before it goes anywhere near
`main`. This document is a *different, smaller, more certain* problem: whether a session is armed as
a lead **at all**. It is upstream of the wake, independently valuable, and cheap to verify — so it
ships on its own track.

---

## 1. What happens

`hooks/sessionend_lead_cleanup.py` clears the lead marker when `SessionEnd` fires with a reason in:

```python
REAL_END_REASONS = {"clear", "logout", "prompt_input_exit", "exit"}
```

`prompt_input_exit` — quitting from the prompt — is **the most common way a human leaves a session**.
But Claude Code sessions are *resumable*: `--resume` restores the same session id **and the full
conversation**. So the routine cycle **quit → resume** deletes the lead marker and brings the session
back **unarmed, silently.**

## 2. Evidence (a real session's ledger)

```
07-13 09:15:39  lead_started                                          ← armed
07-14 06:20:09  session_end  reason=prompt_input_exit  was_lead=TRUE  ← UNARM #1
07-14 07:25:02  lead_started                                          ← re-armed BY HAND (masked it)
07-14 20:10:29  session_end  reason=prompt_input_exit  was_lead=TRUE  ← UNARM #2 (stuck)
07-14 20:10:58  session_end  reason=prompt_input_exit  was_lead=false ← nothing left to clear
07-17 19:04:01  session_end  reason=prompt_input_exit  was_lead=false ← still unarmed
```

No corruption, no plugin-reload churn, no prune (the only `pruned` events were executors, days
earlier). **The hook did exactly what it was written to do.** Note it happened *twice*: the first
occurrence was manually re-armed, which hid the problem entirely.

Cross-checked against commit timestamps: all delegated work that day ran while properly armed; the
unarm landed ~60s after the last executor closed. Everything afterward ran unarmed.

## 3. Why this matters more than it looks

If a session is not armed, then for that session:

- the **routing gate never fires** (`pretool_route_guard` fast-exits on `is_lead`),
- **no wake is possible** — `stop_lead_watch.py` fast-exits on `is_lead`, so no executor report can
  wake anything, regardless of how good the wake design is,
- **ownership breaks** for anything spawned afterward (`owner_lead` points at a sid with no marker).

So **"the wake didn't fire" and "nothing was armed" are indistinguishable from the outside.** Some
unknown share of recurring "the wake missed again" reports may be this, not the wake. Any wake work
that doesn't fix arming durability is building on sand.

There is also a **split-brain**: the model still believes it is the lead (its conversation context
says so — it keeps announcing, proposing packets, spawning executors) while relay's on-disk truth
says it is not. Nothing reconciles the two. In the observed case the discrepancy surfaced only by
accident, when an unrelated command happened to error.

## 4. The reason-bucket error

The four reasons are not equivalent:

| Reason | What happens to the conversation | Should it unarm? |
|---|---|---|
| `clear` | wiped — model returns with **no lead context** | **Yes.** Armed + contextless is worse than unarmed |
| `logout` | session genuinely over | **Yes** |
| `exit` / `prompt_input_exit` | **resumable** — same id, full conversation restored | **No.** This is a *pause*, not a death |

Lumping them together is the bug: **the current code treats a pause as a death.**

## 5. The principle: liveness is derived, not destroyed

relay already works this way everywhere else — `relay list` renders a `LAST ACTIVE` age so a
probably-dead lead is *visible*; `relay prune` sweeps genuinely-old ghosts; unique-naming ignores
stale leads (`LEAD_LIVE_WINDOW_SECONDS`); and a **crashed** lead's marker is deliberately *preserved*,
which is the entire basis of `/relay:resume` for a crashed lead.

Which exposes the inconsistency plainly:

> **Crash → marker survives → resume comes back armed.
> Clean exit → marker deleted → resume comes back unarmed.**

The tidier exit path is the one that loses state.

## 6. `SessionStart` exists, and it carries `source`

relay registers only `PreToolUse`, `UserPromptSubmit`, `Stop`, `SessionEnd`. **`SessionStart` is a
supported event relay simply doesn't use** — confirmed on disk (Anthropic's own
`learning-output-style` plugin ships one; identical `{matcher, hooks:[{type:"command", command}]}`
shape).

Documented payload (`command` hooks get full stdin and may run any shell command):

```json
{ "session_id": "...", "transcript_path": "...", "cwd": "...",
  "hook_event_name": "SessionStart",
  "source": "startup" | "resume" | "clear" | "compact",
  "model": "...", "agent_type": "...", "session_title": "..." }   // last three optional
```

Two facts make the fix trivial:

- **it fires on `--resume` / `--continue` / `/resume`**, and
- **`--resume` preserves the same `session_id`**, so per-session state keys straight through.

> **VERIFICATION STATUS — read before building.** The above is from the official hook docs and is
> **not yet empirically confirmed on this machine's Claude Code build.** This project has repeatedly
> been bitten by doc-vs-reality gaps (see `async-rewake-findings.md`, where a documented mechanism
> behaved differently in practice and two separate spikes returned false results). **Spike it first**
> (§8).

## 7. Proposed fix — a closed state machine

`SessionEnd.reason` and `SessionStart.source` are complementary, which turns arming into a proper
lifecycle instead of a one-way delete:

| Event | Value | Action |
|---|---|---|
| `SessionEnd` | `clear`, `logout` | **hard-clear** — context genuinely gone |
| `SessionEnd` | `exit`, `prompt_input_exit` | **tombstone** (`ended: true` + ts) — a pause, not a death |
| `SessionStart` | `resume` | **re-arm** from the tombstone (or warn loudly) |
| `SessionStart` | `clear` | stay unarmed — no lead context to resume |
| `SessionStart` | `startup`, `compact` | no-op |

Three properties worth stating:

1. **Hook-driven end to end** — no instruction to any model, nothing depending on model compliance.
2. **`source` is a refinement, not a prerequisite.** Even without it, the hook could ask *"is there a
   tombstone for this sid?"* — disk state already answers "was this a lead". `source` lets us be
   precise about `clear` vs `resume` instead of inferring.
3. **Loudness is the minimum bar; auto-re-arm is the luxury.** `sessionend_lead_cleanup` already
   writes `was_lead: true` to the ledger at the exact instant it unarms a lead. The warning
   information existed and went into a log nobody reads. **Silence is the actual defect.**

## 8. Plan

1. **Spike (gates everything).** A throwaway `SessionStart` hook that does nothing but append its raw
   stdin payload to a file. Verify empirically, on this build:
   - does it fire on a fresh start, and what is `source`?
   - does it fire on `claude --resume <id>`, and is `source == "resume"`?
   - is `session_id` present, and **identical** to the original session's id?
   Ground truth = the logged payloads, not the docs. Same discipline as `async-rewake-findings.md`.
2. **Implement** (only if the spike confirms): tombstone-on-pause in `sessionend_lead_cleanup.py`,
   new `SessionStart` hook, registration in `hooks/hooks.json`, `relay list` surfacing of tombstoned
   leads, tests, version bump.
3. **Decide re-arm vs warn.** Auto-re-arm is convenient but arming has side effects (tab rename,
   colour, gate on). Warning loudly is the safe floor. Open question — resolve before implementing.
4. **Merge to `main`** once verified. This branch stays independent of `wake-push`.

## 9. Open questions

- **Auto-re-arm, or warn-and-let-the-human-run `/relay:mode`?** (§8.3)
- **What exactly does a tombstone retain?** Everything (project, cwd, iterm_session, colour,
  predecessor) so re-arm is lossless — or a minimal identity record?
- **Does a tombstone reserve its project name?** Today ghosts don't (`LEAD_LIVE_WINDOW_SECONDS`).
  A tombstoned-but-resumable lead arguably *should* hold its name, or a resume could come back
  renamed (`claude-relay` → `claude-relay-2`).
- **`compact`** — confirm a compaction doesn't unarm anything today, and that `SessionStart(compact)`
  needs no action.
