# relay — delegate Claude Code work across terminal tabs

A Claude Code plugin for macOS (iTerm2 or Terminal.app) that turns one session into a **lead** —
it plans, delegates, and reviews — and spawns **executor** sessions in their own terminal tabs,
windows, or split panes, each seeded with a work
packet. Executors **stage their work (never commit)**, write a report, and stay idle for reuse; the
lead reviews the staged diff and commits. You stay in the loop at every gate: the lead proposes a
split and waits for your go, and wakes you when an executor finishes.

## Requirements

- macOS with **iTerm2** (the full experience) or **Terminal.app** (supported, with differences:
  executors open as new *windows* — Terminal has no scriptable tab-create; follow-up `send`s reopen
  the conversation via `claude --resume` in a fresh window — Terminal can't type into a running
  session; `relay close` kills the process but the window may linger for you to Cmd-W — some macOS
  versions ignore scripted window-close; and no tab colors or lead-tab focus). Auto-detected from
  `$TERM_PROGRAM`; override with `RELAY_TERMINAL=iterm|terminal` or `"terminal_app"` in the config
  (below).
- **Claude Code**
- Fully local, no telemetry — see [PRIVACY.md](PRIVACY.md).
- **terminal-notifier** (`brew install terminal-notifier`) — recommended, not required: it gives
  desktop banners that name which executor finished, coalesce per lead, and **click through to the
  lead's tab**. Without it, relay falls back to macOS's built-in notification (same info, not
  clickable) and `/relay:mode` prints a one-line nudge.

## Install

**Via the plugin marketplace** (the repo doubles as its own single-plugin marketplace):

```
/plugin marketplace add spacegrowth/claude-relay
/plugin install relay@claude-relay
```

**Or from a local clone** (development / trying changes): start sessions with
`claude --plugin-dir /path/to/claude-relay`.

Either way the skills invoke relay by its plugin-absolute path
(`${CLAUDE_PLUGIN_ROOT}/bin/relay`), so nothing needs to be on PATH.

Optional, for typing bare `relay` in your own terminal:

```
ln -sf /path/to/claude-relay/bin/relay ~/.local/bin/relay
```

## Quick start

**First run, copy-paste (~10 min, cheap):** the [`examples/mini`](examples/mini/) smoke test —
serial foundation → two parallel executors (one reused, one fresh) → lead integration, sized for
haiku executors.

```bash
rm -rf /tmp/textops && mkdir -p /tmp/textops && cd /tmp/textops && git init -q
claude --plugin-dir /path/to/claude-relay
```

Then, inside that session:

> I want to build `textops` — the spec's in `/path/to/claude-relay/examples/mini/BRIEF.md`.
> Use haiku executors. How would you approach it?

…and type `/relay:mode` when it proposes the split. The general flow, any project:

1. Start a session in your project and describe the work (or point it at a brief).
2. Type `/relay:mode`. The session adopts the lead role, checks its model is strong enough, and arms
   the routing gate.
3. The lead proposes a decomposition — one line per executor, with each packet's file path — and
   **waits for your explicit go**. It never spawns without it.
4. On your go, executors open in their own tabs and work in parallel. `relay list` shows what's in
   flight; you get a notification as each one reports.
5. Review each report + staged diff (the lead helps), commit yourself, and close the executors.

More examples: [`textkit`](examples/textkit/) (parallel fan-out) and [`calc`](examples/calc/)
(the full serial → parallel → serial build).

## Mental model

The flow, in five beats:

1. **Design** — tell the session what to build, or point it at a brief.
2. **`/relay:mode`** — arm it as the lead. (Order is flexible: arm first and then describe the
   work, or design first and arm after — both work.)
3. **Approve the split** — the lead proposes executors + packet files and **waits for your go**.
4. **Spawn** — executors build in parallel, each in its own tab/pane; the lead wakes you as each
   one reports.
5. **Review → commit → close** — diff page per executor, you approve, the lead commits, sessions
   close (or take follow-up packets).

And the three nouns:

- A **session** = one executor in its own terminal tab (or window/pane), working one worktree/topic. It stays alive
  across packets — one engineer you keep assigning related work to, not a disposable one-shot.
- A **packet** = a work order (a `.md` file). relay auto-appends the rules every executor follows
  (stage-don't-commit, one deliverable per packet, required report format) — you never write those.
- A **report** = what the executor writes back when done, at a path relay assigns.

## Commands

```
/relay:mode                          adopt the lead role (arms the gate + auto-wake)
/relay:spawn <worktree> <topic> <packet.md> [--model NAME] [--name LABEL]
/relay:send  <session_id> <packet.md>      follow-up into the SAME session (reuse > respawn)
/relay:check [<session_id> | --all]        busy / reported / stalled / dead
/relay:list                                leads + active executors (closed hidden; --closed shows)
/relay:close <session_id> [--supersede <new_id>]
/relay:stop                                unarm: step down from lead mode (gate + auto-wake off)
/relay:focus <session_id>                  jump to that session's tab/pane/window (executor or lead)
/relay:resume <session_id>                 reopen a dead tab's conversation, context intact
/relay:restart <session_id>                re-run a dead session's packet fresh (loses context)
/relay:route retain "<reason>"             open a grace window when the gate blocks lead work
/relay:diff <session_id>                   render staged changes to an HTML review page and open it
```

Also: `relay list` hides closed/superseded/dead sessions by default; pass `--closed` to reveal them (capped at 15 most recent). `relay report <sid>` prints a finished report in a green banner, and
`relay prune [--days N] [--dry-run]` clears old closed/dead session state. `relay diff <sid>
[--open] [--all]` renders an executor's `git diff --staged` to a self-contained, offline HTML page
(side-by-side via a vendored, checksummed diff2html — see [VENDOR.md](VENDOR.md) — with a stdlib
fallback if that bundle is missing or fails its integrity check) so you review diffs in one click
without spending model tokens on it. Its output (and every executor's closing line) includes a
cmd+clickable `file://` URL to the page.

Type them, or just describe what you want ("check on my sessions") — the lead invokes the right one.

## The routing gate (friction, not trust)

Once armed, a hook **blocks large inline edits** by the lead (over ~40 new lines, or creating a new
file) and tells it to delegate — or to run `/relay:route retain "<reason>"` for genuinely
lead-appropriate work (a ~2-minute grace window). Small review-class fixes pass silently. Packet
files are exempt — writing packets is the lead's job. Every block and retain is logged to
`~/.relay-tasks/sessions.jsonl`.

Honest limits: it does **not** gate `Bash` (`git commit`, `sed -i`, heredocs pass ungated — that
discipline stays on the lead), and it only acts in `/relay:mode` sessions — every other session on
the machine is untouched (the hook fast-exits, fail-open).

## Auto-wake and notifications

While the lead sits idle, a Stop hook watches in the background. When an executor's report lands,
the lead **wakes**, announces what's ready, and **waits for your direction** — it never auto-reviews
or auto-commits. You also get a macOS notification naming the project and executor; with
terminal-notifier installed, clicking it jumps to the lead's tab (without it, the built-in banner
carries the same info, unclickable). Wakes are scoped to executors the lead owns — multiple leads
on different projects don't cross-wake.

## Telling tabs apart

- **Role-prefixed titles**: lead tabs are `[Lead] <project>`, executor tabs `[Exec] <session>`.
- **Per-lead tab colors** (iTerm only): each lead gets a stable color from a 6-color palette, and
  every executor it spawns inherits it — so with multiple leads running, one glance groups each
  lead with its workers. Disable with `"tab_colors": false`.
- **Pane layout** (iTerm only): set `"executor_layout": "pane"` (or pass `--pane` at spawn) to open
  executors as split panes inside the lead's own tab instead of separate tabs; `--tab` forces a
  tab for one spawn regardless of config. Falls back to a tab if the lead's iTerm session can't be
  located. `relay focus` selects the exact pane, not just the tab (Terminal.app: always a window).
- **True adjacent-tab placement** (iTerm only, optional nicety): for `layout="tab"` spawns,
  AppleScript alone can only put a new tab in the lead's window, never truly next to it — install
  `pip3 install --user iterm2` and enable iTerm's Settings → General → Magic → "Enable Python API"
  (one-time toggle) to get the executor's tab created immediately at the lead's tab index + 1.
  Fully optional: without the package or with the API disabled, everything works exactly as
  before (same-window-at-end placement), just not index-adjacent — a spawn never hangs or fails
  over this being unavailable.

## Config

Optional `~/.relay-tasks/lead/config.json` (absent → these defaults):

```json
{"edit_line_threshold": 40, "block_on_new_file": true, "grace_seconds": 120,
 "auto_wake": true, "surface_commits": false, "poll_seconds": 1800, "poll_interval": 5,
 "notify_on_wake": true, "executor_skip_permissions": false,
 "terminal_app": "auto", "tab_colors": true, "executor_layout": "tab"}
```

`poll_seconds` must stay under the `Stop` hook's `timeout` in `hooks/hooks.json` (currently 1900s)
— the harness kills the hook's background poller at that timeout regardless of `poll_seconds`, so
raising one without the other silently breaks auto-wake (see
[async-rewake-findings.md](docs/async-rewake-findings.md#addendum-silent-auto-wake-death-2026-07-10)).

`executor_skip_permissions` (default `false`) controls whether executors run with
`--dangerously-skip-permissions`. Off = executors prompt before edits/commands. Set `true` for
hands-off runs — partly mitigated by design (executors stage, never commit; the lead reviews before
anything lands), but a real tradeoff. Per-spawn override: `--skip-perms` / `--no-skip-perms`.

## Troubleshooting

- **`/relay:check --all`** tells you the real state (busy/reported/stalled/dead) — trust it over how
  a tab looks. `stalled` means go look at that tab.
- **Tab died mid-build?** `relay resume <sid>` reopens the same conversation with context and staged
  work intact; `relay restart <sid>` re-runs the packet fresh.
- **Executor finished but the lead never woke?** Check `ls ~/.relay-tasks/lead/` — if empty, arming
  failed; re-run `/relay:mode`. A landed report surfaces on the lead's next idle either way, and
  `relay report <sid>` pulls it by hand.
- **A brand-new worktree may ask "trust this folder"** once — relay pre-approves this when it can;
  if not, click trust once.
- **`/plugin list` may not show relay** when loaded via `--plugin-dir` — a display quirk. If
  `/relay:list` responds, it's working.
- **Model note:** `/relay:mode` checks the session's model and recommends switching up if it's too
  weak to lead. Decide your model once, then arm — the self-check gets unreliable after repeated
  `/model` switches in one session.
