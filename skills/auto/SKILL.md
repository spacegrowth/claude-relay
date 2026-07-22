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
- **Committing executor work is gated separately** (#16 phase 2) — turning auto on does *not* by
  itself license a commit. It is permitted without asking only when all five conditions hold:
  (1) `relay verify` says `COUNTS-MATCH`; (2) the report's TL;DR is `Status: clean`, `Risk flags:
  none`, `UNVERIFIED: none` — `clean-with-caveats` stops; (3) the packet was in the approved plan;
  (4) nothing sign-off-gated is touched (core logic, ledgers, parity/golden tests, migrations,
  deploys — and for relay itself, `hooks/`, `lib/lead_guard.py`, ledger formats); (5) you have
  ACTUALLY READ the staged diff. Check it with
  `${CLAUDE_PLUGIN_ROOT}/bin/relay verify <sid> --for-autocommit --in-plan --diff-reviewed` —
  it prints `CLEARED` or `NOT-CLEARED-BECAUSE-<reason>`. Conditions 3 and 5 are *your* attestations;
  pass those flags only if they are true. Anything not cleared → stop and ask, naming the condition.
- The posture is **scoped to this session and this plan**: it resets to the config default the next
  time lead mode is armed (`/relay:mode`), so it can't silently outlive the plan it was scoped to.

Do NOT turn autonomous mode on by yourself. The user opts in — it is their trust to extend, not
yours to assume. If the round-trips are getting tedious, you may *suggest* `/relay:auto on`; then
wait for them to ask for it.
