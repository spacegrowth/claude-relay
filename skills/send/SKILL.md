---
name: send
description: >-
  Send a follow-up packet into an EXISTING executor session (reuse, not a fresh spawn). Invoke
  with /relay:send, or when asked to route a fix-list or related task to a session that's
  already working that area.
arguments: [session_id, packet]
---

Write the follow-up packet content to a file (same rules as `/relay:spawn` — task-specific
content only, GATES/REPORT FORMAT are auto-appended), then run:

`${CLAUDE_PLUGIN_ROOT}/bin/relay send $session_id $packet`

(Call relay via `${CLAUDE_PLUGIN_ROOT}/bin/relay` — Claude Code substitutes the plugin's absolute
path — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.)

This types into the SAME tab/process — same conversation context, no cold start. Only works on a
`reported`/idle session; `relay` refuses on `busy`/`stalled`/`superseded` targets (writing into a
mid-turn session risks corrupting it). Run `/relay:check $session_id` first if unsure of status.

**Terminal.app backend note**: Terminal cannot type into a running session (no iTerm `write text`
equivalent), so there `relay send` automatically closes the old window and reopens the SAME
conversation via `claude --resume` in a fresh window with the packet delivered — context is fully
preserved; it just costs a new window instead of reusing the tab in place.
