---
name: stop
description: >-
  Step down from lead mode for this session. Invoke with /relay:stop, or when asked to "unarm",
  "stop relay", "step down from lead", "turn off the gate".
arguments: []
---

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.

Run `${CLAUDE_PLUGIN_ROOT}/bin/relay stop "$CLAUDE_CODE_SESSION_ID"` to step down from lead mode
for this session — deactivates the routing gate and auto-wake for this session.

This command is idempotent: stopping twice is not an error. Stepping down is reversible — run
`/relay:mode` to re-arm lead mode for this session.
