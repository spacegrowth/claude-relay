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

**Choosing each executor's model — decide per packet, by judgment demanded and cost of a wrong-but-
plausible result, never by file count:**

- **Haiku-class** — mechanical, fully-specified, verifiable-by-command: rename sweeps, boilerplate
  tests from a shown template, config plumbing, doc formatting. The packet must be tight (exact
  files, exact acceptance commands). If the packet needs a "why" explained, it's not haiku work.
- **Sonnet-class (the workhorse, default)** — bounded features, bugfixes with a repro, test suites,
  refactors within one module. Most packets land here.
- **Opus-class** — unknown-root-cause debugging, cross-cutting changes, anything touching core
  logic/ledgers/parity tests, and any task where a wrong-but-plausible result would likely survive
  your review. Pay for judgment exactly where review can't catch its absence.
- **Upgrade signals**: two fix-list rounds haven't landed it (respawn stronger + `--supersede`);
  ambiguity you can't spec away in the packet. **Downgrade signal**: your acceptance criteria could
  be checked by a script.
- **Effort is a quality lever on top of the right model, never a cost lever** (packet `EFFORT:`
  line or `--effort`). Leave it unset. Raise it (`xhigh`) only when you've already chosen opus for
  unknown-root-cause work AND the executor's whole territory is that kind of work — it's fixed per
  process and executors are reused, so you're setting it for every packet the session will get.
  Don't use `low`/`medium` to save money: thinking tokens are a minority of an executor's spend
  (reading files dominates), and a weaker tier thinking longer doesn't gain the judgment it lacks —
  haiku is the cost lever.
- **Version hygiene**: pass the TIER alias (`haiku`/`sonnet`/`opus`) and let relay resolve the
  concrete id through this machine's CLI — never type version ids (`…-4-6`) from memory; a stale id
  silently pins an old model. `relay doctor` shows what each alias resolves to here.

Write the task-specific packet content (ROLE / REQUIRED READING / WORK PACKET only — the standing
GATES and REPORT FORMAT are in the executor agent's system prompt, and `relay` appends the
per-packet report path / self-diff / closing line) to a file, then run:

`${CLAUDE_PLUGIN_ROOT}/bin/relay spawn $worktree $topic $packet --model $model --lead "$CLAUDE_CODE_SESSION_ID"`

**`$worktree` is the ABSOLUTE path of the project directory the executor works in** — the *shared*
project dir (e.g. `/tmp/calc`), NOT a per-task or per-module name. Parallel executors on the same
project all pass the **same** worktree; they just touch different files in it. (relay resolves it to
an absolute path and refuses to spawn if it isn't an existing directory.)

Pass `--lead "$CLAUDE_CODE_SESSION_ID"` (bash expands it to your own lead session id) so the
executor inherits your ownership — it's stamped with your lead id and project.

(Call relay via `${CLAUDE_PLUGIN_ROOT}/bin/relay` — Claude Code substitutes the plugin's absolute
path — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.)

Relay lints every outgoing packet (advisory ⚠/ℹ lines at spawn and send — e.g. the text mentions
Linear but no `MCP:` line; big reading pinned to 200K; the packet tells the executor to commit or
to ask someone). Fix what it flags before the executor burns a turn on it; `relay lint <packet>
--worktree <dir>` runs the same checks without sending.

**Succeeding a retired session?** Pass `--seed <retired_session_id>` (or a path to its
`successor-seed.md`) to inherit what that session did on this territory — its packet index and each
report's outcome/risk/UNVERIFIED lines get appended to this packet as context, ahead of the GATES.
See `/relay:retire`.

(`--model` optional — omit for the default. Add `--keep` if you already know a follow-up packet is
coming soon and don't want auto-close to park it in between (it can always be resumed; `relay keep
<sid> --off` unpins). Add `--name <label>` for a custom session name, or
`--scope <tag>` for the short area tag shown in `/relay:list`. Add `--pane` to open this executor
as a split pane in your own window instead of a tab (`--tab` to force a tab), overriding the
`executor_layout` config default; iTerm only, falls back to a tab if your session can't be found.
Executors launch with NO MCP servers by default — saves tokens every turn and removes a
side-effect surface. **If the packet genuinely needs one, declare it IN the packet** with a line
`MCP: linear` (strict allowlist of servers configured in `~/.claude.json` / the worktree's
`.mcp.json`; comma-separate several) or `MCP: inherit` (everything, incl. plugin/connector MCPs
like Chrome or Gmail). The packet is the source of truth — relay reads the line at spawn and, on a
later `/relay:send`, relaunches the executor's conversation with the wider set if it lacks what the
new packet declares. `--mcp SPEC` on the command line overrides the line. Decide per packet; don't
declare inherit by habit.
Context window: executors default to the **1M** window (`executor_default_context`, shipped `1m` —
the window is a ceiling, not consumption, so a bounded packet costs the same at either). To pin one
executor down, put `CONTEXT: 200k` in its packet; haiku always runs 200K. The window can't change
on resume — a heavy session is widened by `/relay:retire` + respawn.)
