---
name: spawn
description: >-
  Open a new executor session in its own iTerm tab, seeded with a work packet. Invoke with
  /relay:spawn, or when asked to delegate genuinely new work to an executor.
arguments: [worktree, topic, packet, model]
---

**First run `/relay:list`** — if a live idle session already owns this worktree/branch/topic,
use `/relay:send` on that session instead of spawning fresh (cheaper, keeps context). Only spawn
when nothing relevant is idle, the relevant session is dead/stalled, or you're upgrading to a
stronger model (a session's model is fixed at launch).

Write the task-specific packet content (ROLE / REQUIRED READING / WORK PACKET only — `relay`
auto-appends GATES and REPORT FORMAT) to a file, then run:

`${CLAUDE_PLUGIN_ROOT}/bin/relay spawn $worktree $topic $packet --model $model --lead "$CLAUDE_CODE_SESSION_ID"`

**`$worktree` is the ABSOLUTE path of the project directory the executor works in** — the *shared*
project dir (e.g. `/tmp/calc`), NOT a per-task or per-module name. Parallel executors on the same
project all pass the **same** worktree; they just touch different files in it. (relay resolves it to
an absolute path and refuses to spawn if it isn't an existing directory.)

Pass `--lead "$CLAUDE_CODE_SESSION_ID"` (bash expands it to your own lead session id) so the
executor inherits your ownership — it's stamped with your lead id and project.

(Call relay via `${CLAUDE_PLUGIN_ROOT}/bin/relay` — Claude Code substitutes the plugin's absolute
path — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.)

**Succeeding a retired session?** Pass `--seed <retired_session_id>` (or a path to its
`successor-seed.md`) to inherit what that session did on this territory — its packet index and each
report's outcome/risk/UNVERIFIED lines get appended to this packet as context, ahead of the GATES.
See `/relay:retire`.

(`--model` optional — omit for the default. Add `--name <label>` for a custom session name, or
`--scope <tag>` for the short area tag shown in `/relay:list`. Add `--pane` to open this executor
as a split pane in your own window instead of a tab (`--tab` to force a tab), overriding the
`executor_layout` config default; iTerm only, falls back to a tab if your session can't be found.)
