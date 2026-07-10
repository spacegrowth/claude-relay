---
name: diff
description: >-
  Render an executor's staged changes to a self-contained HTML review page and open it. Invoke
  with /relay:diff, or when asked "show me the diff", "review X's changes", "let me see what
  changed".
arguments: [session_id]
---

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.

Run: `${CLAUDE_PLUGIN_ROOT}/bin/relay diff $session_id --open`

This renders `git diff --staged` from the session's worktree into a self-contained HTML page
(diff2html side-by-side when the vendored bundle is available, a stdlib fallback otherwise — see
VENDOR.md) and opens it — zero model tokens spent reviewing the diff yourself. By default the page
is scoped to the files mentioned in the session's current packet report (best-effort); pass
`--all` (append it to the command above) for the full unfiltered staged diff. Output is written to
that packet's `NNN-diff.html` next to its report.
