---
name: restart
description: >-
  Re-run a closed/dead executor's packet in a fresh tab as a NEW conversation (loses prior context).
  Invoke with /relay:restart, or when asked "restart X", "re-run X's packet", "start X over". Prefer
  /relay:resume if you want to keep the executor's context.
arguments: [session_id]
---

Run: `${CLAUDE_PLUGIN_ROOT}/bin/relay restart $session_id` (Claude Code substitutes the plugin's
absolute path when this skill loads — call it this way, not as bare `relay`, which often isn't on
the Bash tool's non-interactive PATH).

This opens a fresh iTerm tab and re-runs the session's **current packet** as a brand-new `claude`
conversation. It does NOT carry over the prior conversation — use `/relay:resume` for that. The
worktree (and any staged work) is untouched, so restarting re-does the packet on top of whatever is
already there; use it when the previous attempt was botched and you want a clean redo.

If the session still looks alive, relay refuses unless you pass `--force`.
