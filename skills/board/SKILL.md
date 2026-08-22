---
name: board
description: >-
  Render the relay board — one self-contained HTML page with every lead, its executors, each
  executor's packet timeline (gist, report outcome, TL;DR), status, launch flags (mcp/context/role),
  token spend, warnings, and copyable commands — and open it. Invoke with /relay:board, or when
  asked "show me the board", "give me the overview", "what's going on across everything", "open
  the relay dashboard".
---

Run: `${CLAUDE_PLUGIN_ROOT}/bin/relay board --open`

(Call relay via `${CLAUDE_PLUGIN_ROOT}/bin/relay` — Claude Code substitutes the plugin's absolute
path — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.)

This writes `~/.relay-tasks/board.html` (override with `--out PATH`) and opens it. It is a
**static snapshot** built from exactly the data `relay list`/`check` use (liveness is refreshed
and finished executors are auto-closed on the way, same as `list`), so it never disagrees with
the table — re-run to refresh. Light theme by default; the ☀️/🌙 button switches and remembers.

On the page: a summary strip (leads / live executors / busy / reported / closed), warning banners
(orphans, reports not yet delivered to their lead, heavy sessions, stale wake hooks), then one
card per lead with its executors. Click an executor row to expand its packet timeline — each
packet's gist, the report's outcome sentence and TL;DR (Status · Risk · UNVERIFIED), and links to
the packet, report and diff page — plus copyable `relay …` commands. The filter box narrows
executors by name/topic/status/model/worktree; "show closed" reveals parked sessions.

Pass `--lead "${CLAUDE_CODE_SESSION_ID}"` to scope executors to this lead's (plus unowned ones);
`--json` emits the board data instead of HTML for programmatic use.
