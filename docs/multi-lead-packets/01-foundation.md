# Packet 1 — ownership foundation (multi-lead)

Add the **ownership** substrate that the rest of the multi-lead feature builds on: leads know their
project, and every spawned executor is stamped with the lead that spawned it. This is a foundation
packet — later packets (wake-scoping, list, restore, send-fix) depend on the exact field names you
establish here, so keep them as specified.

Design doc for context: `docs/multi-lead.md` (§ "Ownership model"). You are building **relay itself**
— the worktree is the `claude-relay` repo.

## Build

### A. Lead marker gains project / cwd / heartbeat (`lib/lead_guard.py`)

Extend `write_marker(...)` so the marker records these fields (keep the existing `session_id`,
`started`, `model`, `iterm_session`):

```jsonc
{
  "session_id": "<uuid>",
  "project":    "webapp",       // NEW
  "cwd":        "/abs/path/...",     // NEW — where a restored lead should reopen
  "last_active":"2026-07-07T...",    // NEW — set to now() on every write_marker call (heartbeat)
  "model": ..., "started": ..., "iterm_session": ...
}
```

- Signature: `write_marker(state_root, session_id, model=None, iterm_session=None, project=None, cwd=None)`.
- `last_active` = `now()` on every call.
- `read_marker` already returns the dict — no change needed beyond the new keys flowing through.

### B. `/relay:mode` names the project (`bin/relay` `cmd_lead_start` + `skills/mode/SKILL.md`)

- `cmd_lead_start` gains `--project` (optional) and captures the lead's cwd:
  - `project = args.project or os.path.basename(os.getcwd())` (default to the working-dir name).
  - `cwd = os.getcwd()`.
  - pass both into `write_marker`.
- `skills/mode/SKILL.md`: the `relay lead-start` line becomes
  `${CLAUDE_PLUGIN_ROOT}/bin/relay lead-start "${CLAUDE_CODE_SESSION_ID}" --project "<project>"`,
  where `<project>` is any name the user gave when invoking `/relay:mode`, else omit `--project`
  (cmd_lead_start defaults it). Add one line to the skill prose: the lead may name its project, and
  it defaults to the directory name.

### C. Executors are stamped with their owner (`bin/relay` `cmd_spawn` + `skills/spawn/SKILL.md`)

- `cmd_spawn` gains `--lead <session_id>` (optional). Compute:
  - `owner_lead = args.lead or os.environ.get("CLAUDE_CODE_SESSION_ID") or None`
  - `owner_project = lead_guard.read_marker(STATE_ROOT, owner_lead).get("project") if owner_lead else None`
- Add both to the executor's `session.json`: `"owner_lead": owner_lead, "owner_project": owner_project`.
  (Place them near the other identity fields.)
- `skills/spawn/SKILL.md`: the `relay spawn` invocation passes `--lead "${CLAUDE_CODE_SESSION_ID}"`
  so the executor inherits the calling lead's ownership.

## Gates

- **Stage your work, do NOT commit.** The lead reviews before anything lands.
- **Only touch:** `lib/lead_guard.py`, `bin/relay`, `skills/mode/SKILL.md`, `skills/spawn/SKILL.md`,
  and `tests/`. Do not edit the hooks, other skills, or `scripts/iterm.py`.
- **All 88 existing tests must still pass** (`python3 -m pytest tests/test_relay.py tests/test_lead_guard.py -q`),
  and `claude plugin validate .` must pass.
- **Add tests** (`tests/`): marker records project/cwd/last_active; `cmd_lead_start` defaults
  `project` to the cwd basename and records cwd; `cmd_spawn` stamps `owner_lead` + `owner_project`
  derived from the lead's marker; and the **unowned** path — no `--lead` and no
  `$CLAUDE_CODE_SESSION_ID` → `owner_lead`/`owner_project` are `None`. Cover both positive and the
  unowned/negative case.
- Backward-compat: existing executor `session.json` files without these fields must not break
  anything you touch (treat missing as `None`).

## Report

State exactly which fields/signatures you added (the later packets depend on these names), the files
touched, the new test names, and the final `pytest` + `validate` results.
