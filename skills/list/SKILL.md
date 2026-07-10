---
name: list
description: >-
  Show every relay session: worktree/topic/scope, model, status (idle/busy/stalled/dead/
  superseded), round count, whether it's reported. Invoke with /relay:list, or when asked
  "what's running", "check my sessions", "what's in flight".
---

Run: `${CLAUDE_PLUGIN_ROOT}/bin/relay list --lead "${CLAUDE_CODE_SESSION_ID}"` (Claude Code
substitutes the plugin's absolute path and the current session id when this skill loads — call it
this way, not as bare `relay`, which often isn't on the Bash tool's non-interactive PATH).

The output has two sections: a **LEADS** block (every lead/project in flight, always shown in full,
with a relative `LAST ACTIVE` age so you can spot a probably-crashed lead) and an **EXECUTORS**
table (with a `PROJECT` column). Passing `--lead "${CLAUDE_CODE_SESSION_ID}"` scopes the executors
to *this* lead's project — its own executors plus any unowned ones — so a lead sees its own work by
default. If the calling session isn't a lead the scoping simply matches no owned executors (unowned
ones still show), which is harmless.

By default, closed/superseded/dead sessions are hidden — pass `--closed` to reveal them (capped at
15 most recent by update time); use `relay prune --dry-run` to see which ones are safe to delete.

Use `${CLAUDE_PLUGIN_ROOT}/bin/relay list --all` for the **global** view — every executor across
every project, regardless of owner.

This is the decision-informing surface — always run it before spawning a new session, so you can
see whether a live idle session already owns the relevant worktree/branch/topic and should be
reused via `/relay:send` instead. It's also the crash-recovery surface: if you're a fresh lead
session picking this up cold, run this first to reconstruct what's already in flight before
asking anything.

Use `${CLAUDE_PLUGIN_ROOT}/bin/relay list --json` instead if you need structured output to parse
programmatically.
