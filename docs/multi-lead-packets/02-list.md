# Packet 2 — `relay list` shows leads + project scoping (§2)

Build on the ownership foundation (packet 1, now landed) to make `relay list` legible when multiple
leads/projects are in flight: show a **LEADS** section, and scope/annotate executors by project.
This is the surface used to see and (later) restore leads.

Design doc: `docs/multi-lead.md` (§2). You are building **relay itself** (the `claude-relay` repo).

Foundation you build on (established by packet 1 — use these exact names):
- Lead marker (`~/.relay-tasks/lead/<sid>/marker.json`): `session_id`, `project`, `cwd`,
  `last_active`, `started`, `model`.
- Executor `session.json`: `owner_lead`, `owner_project` (either may be `None` = unowned).

## Build

### A. `lib/lead_guard.py` — `list_leads(state_root)`

Return a list of lead marker dicts — read every `<state_root>/lead/*/marker.json` (skip
`config.json` and any non-marker entries). Each item is the marker dict as stored. Sort by `started`
(oldest first). Fully defensive: an unreadable/kmalformed marker is skipped, never raises.

### B. `bin/relay` `cmd_list` — a LEADS section + an owner column

- Print a **LEADS** block first (from `list_leads`): columns `PROJECT`, `SESSION` (the uuid),
  `MODEL`, `LAST ACTIVE` (as a relative age, e.g. `30s ago` / `12m ago` / `2h ago`, computed from
  `last_active`). A clearly-stale `LAST ACTIVE` is how a human spots a probably-crashed lead.
- Then the existing **EXECUTORS** table, with a new `PROJECT` column showing `owner_project` (or
  `-` when unowned). Keep the existing columns and coloring.

### C. Scoping flags — `--lead <sid>` and `--all`

- `cmd_list` gains `--lead <session_id>` and `--all`.
- **LEADS section always shows ALL leads** (you always want to see/restore any project).
- **EXECUTORS section scoping:** with `--lead <sid>`, show only executors whose `owner_lead == sid`
  **plus unowned ones** (owner_lead is None); with `--all` (or no flag), show every executor
  (back-compat). Unowned executors are always visible.
- `skills/list/SKILL.md`: pass `--lead "${CLAUDE_CODE_SESSION_ID}"` so a lead running `/relay:list`
  sees its own project's executors by default; document `--all` for the global view. (If the calling
  session isn't a lead, `--lead <non-lead-id>` simply matches no owned executors — the unowned ones
  still show — which is fine.)

### D. `--json`

`relay list --json` returns `{"leads": [...], "executors": [...]}` — leads from `list_leads`,
executors as today plus their `owner_lead`/`owner_project`. Respect `--lead`/`--all` scoping on the
executors array. `--json` stays uncolored.

## Gates

- Changes on disk, not committed (the repo isn't under git; leave them uncommitted).
- **Only touch:** `lib/lead_guard.py`, `bin/relay`, `skills/list/SKILL.md`, `tests/`.
- **All 96 existing tests must still pass**, and `claude plugin validate .` must pass.
- **Add tests:** `list_leads` reads/sorts markers and skips junk; `cmd_list` renders a leads section;
  `--lead` scopes executors (owned + unowned shown, other-lead's hidden); `--all`/no-flag shows all;
  `--json` shape includes both arrays with owner fields. Cover the unowned-always-visible case.
- Relative-age formatting should degrade gracefully if `last_active` is missing/old-format (show
  `-`, don't crash).

## Report

The exact function/flag names added, files touched, new test names, `pytest` + `validate` results,
and a pasted sample of the new `relay list` output (leads section + executors with the project
column).
