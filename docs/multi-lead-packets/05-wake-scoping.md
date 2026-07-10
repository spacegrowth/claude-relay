# Packet 5 — wake scoping + lead heartbeat (§1) — the capstone

The core multi-lead fix: today an idle lead is woken about **every** executor globally, so two leads
cross-wake on each other's work. Scope the wake to ownership — each lead is only woken about ITS OWN
(and unowned) executors. Plus a per-turn lead heartbeat so `relay list`'s `last_active` reflects real
liveness (a stale one = probably crashed).

Design doc: `docs/multi-lead.md` (§1). You are building **relay itself** (the `claude-relay` repo).

Foundation you build on (already landed):
- Executor `session.json` has `owner_lead` (may be `None` = unowned).
- Lead marker has `last_active`; `lead_guard.read_marker`/`write_marker` exist.
- `hooks/stop_lead_watch.py` already holds the lead's own `sid` and calls
  `lg.has_inflight_executors(STATE_ROOT)` and `lg.new_reports_for(STATE_ROOT, sid)`.

## Build

### A. `lib/lead_guard.py` — scope the wake helpers by owner

- `has_inflight_executors(state_root, owner_lead=None)`: when `owner_lead` is given, count a `busy`
  executor only if its `owner_lead in (None, owner_lead)` (owned + unowned). `owner_lead=None` →
  global, unchanged (back-compat).
- `new_reports_for(state_root, lead_sid)`: add an ownership filter to the executor iteration — only
  surface a report if that executor's `owner_lead in (None, lead_sid)`. So a lead is only told about
  its own (and unowned) reports, never another lead's. (Keep the existing surfaced-tracking; just add
  the owner filter.)

### B. `lib/lead_guard.py` — a lead heartbeat

- `touch_lead(state_root, session_id)`: read the marker, set `last_active = now()`, write it back
  (read-modify-write, preserving the other fields). Defensive no-op if the marker is missing/
  unreadable — never raises.

### C. `hooks/stop_lead_watch.py` — use them

- Pass the lead's own `sid` as the `owner_lead` to `has_inflight_executors(STATE_ROOT, sid)` and rely
  on the now-scoped `new_reports_for(STATE_ROOT, sid)`. Net: the idle-poll waits only on this lead's
  executors, and only this lead's (or unowned) reports wake it.
- Call `lg.touch_lead(STATE_ROOT, sid)` once near the top (after confirming it's a lead), so every
  lead turn refreshes the heartbeat. Must be fail-open (wrap/no-op on error) — it must never block
  the hook.

## Gates

- Changes on disk, not committed (repo isn't under git).
- **Only touch:** `lib/lead_guard.py`, `hooks/stop_lead_watch.py`, `tests/`.
- **All 127 existing tests must still pass**, `claude plugin validate .` must pass.
- **Add tests:**
  - `has_inflight_executors` scoped: an executor owned by lead-A is counted for lead-A, NOT for
    lead-B; an unowned busy executor is counted for any lead; `owner_lead=None` still counts all.
  - `new_reports_for` scoped: lead-A gets its own + unowned reports, NOT lead-B's — the
    **two-leads-no-cross-wake** case explicitly.
  - `touch_lead` updates `last_active` and no-ops cleanly on a missing marker.
- Keep every hook path fail-open (any error → the existing exit-0/allow behavior).

## Report

Signatures added/changed, files touched, new test names, `pytest` + `validate` results, and confirm
the two-leads-no-cross-wake test passes (lead A's `new_reports_for` excludes lead B's executor).
