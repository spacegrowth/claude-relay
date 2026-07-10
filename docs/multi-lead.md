# Design: multi-lead, lead restore, reliable reuse

Three related capabilities, all built on one new idea — **ownership**. Today relay assumes a single
lead: executors are a global unowned pool, `relay list` is a flat global view, leads are invisible
(no way to see or restore one), and reuse (`send`) is unreliable. This design fixes all four.

Guiding constraint: **reuse the existing seams**, don't fork parallel paths. Ownership is a couple
of new fields + an `owner` filter param threaded through helpers that already exist
(`has_inflight_executors`, `new_reports_for`, `_check_one`, `cmd_send`, `cmd_resume`). No new
subsystem; no growth in `bin/relay` beyond the spawn fields and one refreshed guard.

---

## Ownership model (project-named)

**`/relay:mode [project-name]`** — the lead names its project (defaults to the cwd basename, e.g.
`webapp`). `relay lead-start` records it. The lead **marker** becomes:

```jsonc
// ~/.relay-tasks/lead/<lead_session_id>/marker.json
{
  "session_id": "<uuid>",   // == the lead's Claude Code session id ($CLAUDE_CODE_SESSION_ID)
  "project":    "webapp",
  "cwd":        "/Users/you/dev/webapp",  // where to reopen on restore
  "model":      "opus",
  "started":    "…",
  "last_active":"…"          // touched by the Stop hook each lead turn (rough liveness, no pid needed)
}
```

**Executors inherit ownership at spawn.** `relay spawn --lead "$CLAUDE_CODE_SESSION_ID"` (the
`/relay:spawn` skill passes it); `cmd_spawn` reads the lead's marker to derive the project, and
stamps the executor's `session.json` with:

```jsonc
"owner_lead":    "<lead_session_id>",   // the uuid — used for restore + scoping
"owner_project": "webapp"           // human-readable, for `relay list`
```

Single source of truth: the **marker** holds the project; the spawn only needs `--lead`.
**Unowned** = `owner_lead` absent (pre-feature executors, or spawned bare outside a lead).

---

## 1. Multi-lead wake scoping (no cross-wake)

The awareness helpers gain an `owner` filter — the change that stops Lead A being woken about Lead
B's executors:

- `has_inflight_executors(state_root, owner_lead=None)` — count only executors owned by `owner_lead`
  (or unowned). `None` → global (back-compat).
- `new_reports_for(state_root, lead_sid)` — **filter reports to those `owner_lead == lead_sid` (or
  unowned)**. This is the fix for the cross-wake noise.
- `stop_lead_watch` already holds the lead's `sid` → passes it as the owner. One-line hook change.

**Policy:** unowned executors are surfaced to **all** leads (nothing gets orphaned; back-compat).

---

## 2. `relay list` shows leads + is project-aware

Two sections instead of one flat table:

```
LEADS
  PROJECT      SESSION (uuid)         MODEL   ARMED     LAST ACTIVE
  webapp   4a71bfd9-…             opus    12m ago   30s ago
  blog         9c2e…                  sonnet  2h ago    1h ago      ← stale → maybe crashed

EXECUTORS  (grouped by project)
  webapp / auth-fix    reported  …
  blog       / dark-mode    busy 4m   …
```

- New `owner`/`project` column on executors.
- `relay list --lead <sid>` scopes to one lead's executors; `--all` = global; the `/relay:list`
  skill passes `$CLAUDE_CODE_SESSION_ID` so **each lead sees its own project by default**.
- Lead "liveness" is best-effort via `last_active` (Stop-hook heartbeat) — a stale timestamp is the
  signal to restore, since we can't cheaply get the lead's real pid.

---

## 3. Lead restore (crash recovery)

Because a lead's `session_id` **is** its Claude session id, restoring a crashed lead is `claude
--resume`:

- **`relay resume <sid>`** detects lead-vs-executor (marker present → lead; `session.json` → executor)
  and, for a lead, opens a fresh tab running `claude --resume <sid>` **in the marker's `cwd`**, with a
  restore nudge:
  > *"You are the resumed lead for project `webapp`. Your routing marker and in-flight executors
  > survived the crash — run `/relay:list` to reconstruct what's in flight, then continue."*
- The marker persists on disk across the crash, so the restored lead is **still armed** (no
  re-`/relay:mode` needed). It reconnects to its executors via `list --lead <sid>` (they were
  stamped with its uuid).
- `relay list` (§2) is how you find the sid to restore.

---

## 4. Reliable reuse (the `send` bug)

Two root causes, both fixed:

**(a) Stale status → false "busy" refusal.** `cmd_send` refuses on the *stored* `s["status"]`, which
is only refreshed by `relay check`/`list`. Right after an executor reports, its stored status is
still `busy` from spawn → send refuses a session that's actually idle-and-reported.
→ **Fix:** `cmd_send` **refreshes liveness first** (runs the `_check_one` logic), then decides on the
*fresh* status. An alive, idle-after-report process becomes a valid send target.

**(b) Closed tab → forced fresh spawn (context lost).** When `iterm.send` can't find the tab, send
marks the session dead and tells you to `relay spawn` — throwing away the context `send` exists to
preserve.
→ **Fix:** if the tab is gone but the session has a `claude_session`, **fall back to resume**: reopen
`claude --resume <claude_session>` in its worktree and deliver the new packet, instead of a cold
spawn. Only truly unrecoverable (no claude_session) falls through to "spawn fresh".

**Convention (prevents (b) in the first place):** executors should **stay idle after reporting** —
they don't exit; the tab persists so `send` can reuse it. The report-format footer and the executor
prompt state this explicitly, and the lead is the one who `relay close`s them when done. (Claude
sessions are interactive and already stay idle after a turn — the fix is mostly *not* closing them
prematurely, plus the resume-fallback for when they are.)

---

## Migration & policy decisions

- **No data migration.** Existing executors lack `owner_lead` → unowned → visible to all leads.
- **Lead step-down** (`close --self`) leaves its executors owned by the (now-defunct) uuid;
  `relay list --all` still finds them. A `relay adopt <session> --lead <sid>` reassign is a v2 nicety.
- **Lead liveness** is a heartbeat timestamp, not a real pid — good enough to flag "probably crashed,
  consider restoring."

---

## What it touches (scope)

- `lib/lead_guard.py` — marker gains project/cwd/last_active; `write_marker`/`read_marker`;
  `owner` filter on `has_inflight_executors` + `new_reports_for`; a `list_leads()` helper.
- `bin/relay` — `cmd_spawn` stamps owner_lead/owner_project; `cmd_lead_start` takes `--project`;
  `cmd_list` shows leads + owner column + `--lead`/`--all`; `cmd_send` refreshes status + resume
  fallback; `cmd_resume` handles the lead case.
- `hooks/stop_lead_watch.py` — pass the lead sid as owner (one line); touch `last_active`.
- `skills/` — `mode` (optional project arg), `spawn` (pass `--lead`), `list` (pass `--lead`),
  `resume` (mention lead restore).
- `tests/` — ownership stamping; wake scoping / no cross-wake; `list` leads + scope; lead-resume
  routing; send status-refresh + resume-fallback.

## Build order (dependencies)

1. **Ownership foundation** (marker fields + spawn stamping) — everything else depends on it.
2. Then **§1 wake-scoping**, **§2 list**, **§3 lead-restore**, **§4 send-fix** are largely
   independent — a natural parallel fan-out (a good relay dogfood: one foundation packet, then
   4 parallel executor packets).
