# Packet 3 — lead restore / crash recovery (§3)

Make `relay resume` able to bring back a **crashed lead**, not just an executor. Because a lead's
`session_id` *is* its Claude Code session id, restore is `claude --resume <lead_sid>` — the lead
returns with full context, and its marker (which survived the crash on disk) means it's still armed.

Design doc: `docs/multi-lead.md` (§3). You are building **relay itself** (the `claude-relay` repo).

Foundation you build on (already landed):
- Lead marker holds `session_id`, `project`, `cwd`, `last_active`, `model`
  (`lead_guard.read_marker(state_root, sid)` returns it; `lead_guard.is_lead(state_root, sid)` tests
  existence; `lead_guard.write_marker(...)` writes it, refreshing `last_active`).
- `relay list` shows leads (so a human finds the sid to restore).
- Existing `cmd_resume(args)` handles **executors** via `_relaunch` + `claude --resume
  <claude_session>`. `iterm.spawn(..., resume_id=<id>)` opens a tab running `claude --resume <id>
  "<prompt>"`.

## Build (`bin/relay` `cmd_resume` + `skills/resume/SKILL.md`)

Extend `cmd_resume` to route **lead vs executor**:

1. If `read_session(sid)` returns an executor → **existing behavior, unchanged**.
2. Else if `lead_guard.is_lead(STATE_ROOT, sid)` → **lead-restore path**:
   - Read the marker (`project`, `cwd`, `model`).
   - Open a fresh tab that reopens the lead's own conversation:
     `iterm.spawn(cwd=marker["cwd"], prompt=<restore-nudge>, label=<a lead label, e.g. f"lead-{project}">,
      pidfile=<lead_dir/pid>, model=marker.get("model"), skip_perms=<config>, resume_id=sid)`.
     (Use the lead's own dir under `~/.relay-tasks/lead/<sid>/` for the pidfile so you don't create a
     bogus executor `session.json`. A lead has NO `session.json` — do not write one.)
   - **Refresh the marker** (`write_marker` with the existing project/cwd/model) so `last_active`
     updates — the restored lead should look fresh in `relay list`.
   - The restore nudge:
     > *"You are the resumed lead for project '<project>'. Your relay routing marker and any in-flight
     > executors survived — run /relay:list to reconstruct what's in flight, then continue where you
     > left off."*
   - Print something like `restored lead '<project>' (session <sid>) — reopening its conversation`.
3. Else → the existing `no such session` error.

`skills/resume/SKILL.md`: add that `/relay:resume <session-id>` **also restores a crashed lead** (by
its lead session id) — reopening the lead conversation with context, marker intact.

## Notes / decisions

- The marker persists across the crash, so the restored lead is **still armed** (no re-`/relay:mode`).
  The resumed session keeps the same `session_id` (that's what `--resume` does), so the marker keyed
  by that id still matches — the routing gate + ownership all still line up.
- Do **not** invent a `session.json` for the lead; leads live only under `lead/<sid>/`.
- Per-turn `last_active` heartbeat (Stop hook) is a **separate** concern (§1) — out of scope here.
  Refreshing `last_active` at restore time is enough for this packet.

## Gates

- Changes on disk, not committed (repo isn't under git).
- **Only touch:** `bin/relay`, `skills/resume/SKILL.md`, `tests/`.
- **All 118 existing tests must still pass**, `claude plugin validate .` must pass.
- **Add tests** (mock `iterm.spawn`): `cmd_resume` on a lead sid calls `iterm.spawn` with
  `resume_id == sid`, `cwd == marker["cwd"]`, and a nudge mentioning the project; refreshes the
  marker's `last_active`; writes NO executor `session.json`. `cmd_resume` on an executor is
  unchanged (still uses `claude_session`). An unknown id still errors.

## Report

Function/branch names added, files touched, new test names, `pytest` + `validate` results, and a
one-line description of what `relay resume <lead-sid>` now does.
