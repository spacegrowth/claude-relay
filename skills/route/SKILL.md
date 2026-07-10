---
name: route
description: >-
  Retain escape hatch for lead mode: when the routing gate blocks a large inline edit that is
  genuinely lead-appropriate work, declare a reason and open a short grace window so the edit can
  proceed. Invoke with /relay:route, or when a PreToolUse block tells you to.
arguments: [reason]
---

Use this ONLY when the edit gate has blocked an `Edit`/`Write`/`MultiEdit` and the edit is
genuinely lead work you should not delegate (e.g. committing/finalizing an executor's staged diff
that needs a small hand-adjustment, or a change that truly can't be packaged as an executor
packet). It is not a way to opt out of delegating real implementation work — that's exactly the
discipline the gate exists to keep.

Run:

```
${CLAUDE_PLUGIN_ROOT}/bin/relay route retain "<one-line reason>" --session "$CLAUDE_CODE_SESSION_ID"
```

This opens a short grace window (default 120s) during which inline edits pass ungated, and records
one durable `retained` event (with your reason) in the shared ledger, so the decision is visible
later. Make the edit within the window; if it expires, the gate re-engages and you'd run this
again. Prefer delegating (`/relay:spawn` / `/relay:send`) whenever the work reasonably fits a
packet — retain is the exception, not the default.
