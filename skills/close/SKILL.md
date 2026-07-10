---
name: close
description: >-
  Mark an executor session done or superseded, OR step down from lead mode for this session.
  Invoke with /relay:close, or when asked "close that session", "mark it done", "this session is
  superseded by X", "step down from lead", "exit lead mode".
arguments: [session_id, superseded_by]
---

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.

**Stepping down from lead mode** (no executor session_id — this session stops being a lead and the
routing gate deactivates for it): run `${CLAUDE_PLUGIN_ROOT}/bin/relay close --self "$CLAUDE_CODE_SESSION_ID"`.

If just closing an executor session: run `${CLAUDE_PLUGIN_ROOT}/bin/relay close $session_id`

If superseding (e.g. a session's model wasn't strong enough and you spawned a replacement): run
`${CLAUDE_PLUGIN_ROOT}/bin/relay close $session_id --supersede $superseded_by`

A session's model is fixed at launch — there's no way to change it mid-session. "Upgrading the
model" always means spawning a fresh session (via `/relay:spawn`) with a stronger `--model`, then
closing the old one with `--supersede` so `/relay:list` shows it was replaced rather than looking
like stale, still-awaiting-review work.
