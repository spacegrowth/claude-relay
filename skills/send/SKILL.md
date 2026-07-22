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

This types into the SAME tab/process — same conversation context, no cold start. `relay` refuses
only `busy`/`stalled` (mid-turn — injecting risks corrupting it) and `superseded` (abandoned)
targets. A `reported`/idle session is sent to in place. A **`closed` or `dead`** session that still
has its captured Claude conversation is **automatically resumed** and delivered the packet in one
shot (full context + staged work back) — so you do NOT need `/relay:resume` first; just send. Only a
closed/dead session with NO captured conversation needs a fresh `/relay:spawn`. Run
`/relay:check $session_id` first if unsure of status.

**Busy target? Queue it with `--when-idle`** instead of waiting or retrying:

`${CLAUDE_PLUGIN_ROOT}/bin/relay send $session_id $packet --when-idle`

The packet is persisted and delivered automatically the moment that session next goes idle — via
its own Stop hook, so nothing has to poll. **Never write a shell `until relay check …; do sleep N;
done` loop for this**: that burns your turn, relay can't see it, and it dies when your turn ends.
Queued packets deliver oldest-first, **one per idle transition** (a second one in the same breath
would inject mid-turn, which is the exact thing this avoids). `--when-idle` on a session that is
*already* idle just sends immediately. It does not soften the other refusals — `superseded` and
`launch-failed` still refuse.

Inspect or cancel with `${CLAUDE_PLUGIN_ROOT}/bin/relay queue $session_id [--cancel <id|all>]`;
`/relay:check` shows a 📥 queued count.

**Terminal.app backend note**: Terminal cannot type into a running session (no iTerm `write text`
equivalent), so there `relay send` automatically closes the old window and reopens the SAME
conversation via `claude --resume` in a fresh window with the packet delivered — context is fully
preserved; it just costs a new window instead of reusing the tab in place.

**Ownership follows the send.** Sending into an executor also **adopts** it — re-points its
auto-wake to the acting lead — which matters after a handoff (an inherited executor otherwise
keeps waking its old, retired lead, silently, forever). If it's currently owned by a live *other*
lead, relay warns instead of stealing it; `relay adopt <session_id> --force` takes it explicitly.
