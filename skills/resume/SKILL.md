---
name: resume
description: >-
  Bring back a closed/dead executor tab — OR a crashed lead — WITH its full context. Reopens the
  same conversation via `claude --resume`, staged work intact. Invoke with /relay:resume, or when
  asked "bring back X", "reopen X", "X's tab closed, restore it", "resume that executor",
  "restore the crashed lead".
arguments: [session_id]
---

Run: `${CLAUDE_PLUGIN_ROOT}/bin/relay resume $session_id` (Claude Code substitutes the plugin's
absolute path when this skill loads — call it this way, not as bare `relay`, which often isn't on
the Bash tool's non-interactive PATH).

This opens a fresh iTerm tab running `claude --resume <the executor's Claude session id>` in its
worktree, so the executor comes back with its **entire conversation/context** and the worktree's
**staged work intact**, and picks up its packet where it left off. Prefer this over `/relay:restart`
whenever you want to keep what the executor already did.

Requires the Claude session id captured at spawn — only sessions spawned after that capture landed
have it. If relay says there's no captured id, use `/relay:restart` instead (re-runs the packet as
a fresh session, losing prior context). If the session still looks alive, relay refuses unless you
pass `--force` (avoid two live instances of the same conversation).

**Restoring a crashed lead.** `/relay:resume <session-id>` also brings back a **crashed lead** — pass
the lead's session id (find it in the LEADS section of `/relay:list`). Because a lead's `session_id`
*is* its own Claude Code session id, restore is `claude --resume <lead_sid>`: the lead's conversation
reopens in the marker's `cwd` with full context. The routing marker survived the crash on disk, so
the restored lead is **still armed** — no need to re-run `/relay:mode`. relay reopens its own tab,
refreshes the marker's `last_active` (so it looks fresh in `relay list`), and writes no executor
state. The restored lead should run `/relay:list` to reconstruct what executors are in flight.
