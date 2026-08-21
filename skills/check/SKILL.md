---
name: check
description: >-
  Poll a session (or all sessions) for a report and update its status. Invoke with /relay:check,
  or when asked "is it done", "check on session X", "has it reported yet".
arguments: [session_id]
---

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.

If `$session_id` is given, run: `${CLAUDE_PLUGIN_ROOT}/bin/relay check $session_id`

Otherwise run: `${CLAUDE_PLUGIN_ROOT}/bin/relay check --all`

Liveness is judged by the executor's recorded **process id**, not its tab title — Claude Code
mutates its own iTerm tab title while working, so a title change never by itself means the executor
died. Status flips: `busy` → `reported` once the report file exists (a report means done,
regardless of process/tab state); → `stalled` if the process died with no report but the tab's
still open, or it's been busy far longer than expected; → `dead` only when the process is gone AND
the tab has closed. A `stalled` session needs you to look at that tab directly — `relay check` only
detects it, it doesn't fix it.

`check` also runs the auto-close sweep on the session(s) it checks: a finished executor whose
report you've already seen gets parked once its work has landed (claimed files clean in the
worktree) or it has idled past `auto_close_idle_minutes` — printed as a 🛏 line. Nothing is lost;
`/relay:send` resumes it. Pin with `relay keep <sid>` if a follow-up is imminent.
