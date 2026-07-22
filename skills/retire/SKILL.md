---
name: retire
description: >-
  Retire a heavy executor: close it AND write a successor-seed.md indexing its packets/reports, so
  spawning a fresh session over the same territory is cheap. Invoke with /relay:retire, or when
  asked "retire that session", "this exec is getting heavy", "rotate this executor",
  "close it but keep what it learned".
arguments: [session_id]
---

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.

Run: `${CLAUDE_PLUGIN_ROOT}/bin/relay retire $session_id`

That closes the session (as `superseded` by its seed, tab and all — same teardown as
`/relay:close`) and writes a **successor seed** to its state dir: an index of every packet it was
sent, with each report's outcome line, `Status`, `Risk flags` and `UNVERIFIED` — plus the worktree
and topic it owned. The seed is an INDEX, not a transcript: the detail stays in the linked reports.

**Then spawn the successor with the seed**, exactly as the retire message tells you:

`${CLAUDE_PLUGIN_ROOT}/bin/relay spawn $worktree $topic $packet --seed $retired_session_id --lead "$CLAUDE_CODE_SESSION_ID"`

The seed is appended to the fresh executor's packet as inherited context (task first, seed second,
GATES last), so the successor starts knowing what happened on this territory without reading
anything else. You can also pass a path to the seed file instead of the retired session's id.

**When to reach for this instead of `/relay:send`.** A long-lived executor accumulates context; a
fresh one starts sharp. Retire when a session is getting heavy, has finished a phase of work, or is
dead/stalled with useful history behind it — the seed is what makes respawning cheap enough to
actually do, so don't keep piling packets onto a heavy session just to avoid the rebuild cost.
Heaviness is not degradation, though: a disciplined executor can do fine work deep in a session, so
this is a judgement call, not a rule.

**Refusals you may hit:**
- *still busy on packet NNN and has not reported* — retiring now kills that packet's work
  unreported, and the seed can't summarise a report that was never written. Wait for it
  (`/relay:check`), or pass `--force` to retire anyway (that packet is then seeded as `NO REPORT`).
- *already retired* — the seed already exists; the message prints its path. Just spawn with it.

(`--keep-tab` retires but leaves the terminal tab open, same as `/relay:close`.)
