---
name: auto
description: >-
  Flip this lead session's autonomous posture: proceed by default on routine in-plan steps instead
  of asking, or go back to waiting. Invoke with /relay:auto, or when asked to "go autonomous",
  "stop asking me every time", "just proceed", "turn auto off", "what posture am I in".
arguments: [on|off|status]
---

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.

Run ONE of these, matching what the user asked for (default to `status` if they only asked what
posture you're in):

```
${CLAUDE_PLUGIN_ROOT}/bin/relay auto on     --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay auto off    --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay auto status --session "$CLAUDE_CODE_SESSION_ID"
```

This only works in a lead session (`/relay:mode` first); it exits with an error otherwise.

**Then tell the user, in one line, which posture you now hold and what that changes** — the command
prints it, but the user needs to hear it from you, because the posture governs *your* behavior from
here on. Use the `🚦 [relay]` marker like every other lead message.

- **`on`** → you now hold the **autonomous** posture. Your default inverts from *wait* to *proceed*
  on the routine, in-plan approval beats — sending the obvious next packet, spawning an executor the
  approved plan already calls for, reviewing a clean report. Read the "Autonomous mode" section of
  `/relay:mode`'s role text for exactly what this does and does not license, and follow it: it holds
  the non-negotiable stop-list, and the rule that **every autonomous action is announced along with
  what you would have asked**.
- **`off`** → back to announce-and-wait on every approval beat. The ordinary posture.
- **`status`** → report the posture *and its origin* (set by command this session, vs. inherited from
  the `autonomous_mode` config default).

**Two things to say plainly whenever you turn it ON**, so nobody mistakes its scope:
- **Committing executor work still stops for the human** — always, in phase 1, no exception.
- The posture is **scoped to this session and this plan**: it resets to the config default the next
  time lead mode is armed (`/relay:mode`), so it can't silently outlive the plan it was scoped to.

Do NOT turn autonomous mode on by yourself. The user opts in — it is their trust to extend, not
yours to assume. If the round-trips are getting tedious, you may *suggest* `/relay:auto on`; then
wait for them to ask for it.
