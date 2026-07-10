---
name: focus
description: >-
  Jump iTerm to a session's tab — an executor's or a lead's. Invoke with /relay:focus, or when
  asked "go to X's tab", "show me session X", "focus/switch to X", "take me to that executor".
arguments: [session_id]
---

Run: `${CLAUDE_PLUGIN_ROOT}/bin/relay focus $session_id` (Claude Code substitutes the plugin's
absolute path when this skill loads — call it this way, not as bare `relay`, which often isn't on
the Bash tool's non-interactive PATH).

This activates iTerm and selects the session's tab whose title matches its relay label (a bounded
match tolerant of Claude Code's title suffix), then selects that tab's window and the tab — the
reliable osascript mechanism from claude-sessions-swiftbar. **Works for executors AND leads**: both
get a stable relay-controlled tab title (executors at spawn, leads when `/relay:mode` renames their
tab), so pass either kind of session id. If an executor's tab is closed, relay points you to
`/relay:resume` (keeps context) or `/relay:restart` (re-runs the packet); if a lead's tab can't be
matched, re-running `/relay:mode` in that lead resets its title.
