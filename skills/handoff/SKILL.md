---
name: handoff
description: >-
  Hand this lead session off to a fresh successor. Invoke with /relay:handoff, or when asked to
  "hand off", "hand this off to a new session", "start a successor lead".
arguments: [handoff_md]
---

Use this when the lead session has gotten heavy (long-lived, large transcript — see the auto-wake
handoff nudge) and the healthy move is a fresh context, not more of this one.

**First, write the handoff file** — a short packet-file-style summary under `~/.relay-tasks/`
(gate-exempt), covering: what's in flight, what's reviewed/committed, open questions, next steps.
Write it like a memo to your successor, not a transcript dump.

**Then run:**

```
${CLAUDE_PLUGIN_ROOT}/bin/relay handoff <path-to-handoff.md>
```

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive
PATH. Unlike other relay commands, handoff reads the caller's session id from
`$CLAUDE_CODE_SESSION_ID` automatically — no need to pass it yourself.

This opens a NEW lead tab, pre-armed (its routing gate and auto-wake are already active — the
successor does NOT need to run `/relay:mode`), seeded with a pointer to the handoff file. **As its
final act, this steps the CURRENT session down** — the gate and auto-wake here turn off. Any
executors this lead owned are inherited by the successor automatically the first time it sends or
resumes them (adopt-on-claim); nothing to re-wire by hand.

Optional flags: `--project NAME` and `--model NAME` override the successor's project/model
(default: inherited from this lead's own marker).
