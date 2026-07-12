# relay — delegate Claude Code work across terminal tabs

A Claude Code plugin for macOS (iTerm2 or Terminal.app) that turns one session into a **lead** —
it plans, delegates, and reviews — and spawns **executor** sessions in their own terminal tabs,
windows, or split panes, each seeded with a work
packet. Executors **stage their work (never commit)**, write a report, and stay idle for reuse; the
lead reviews the staged diff and commits. You stay in the loop at every gate: the lead proposes a
split and waits for your go, and wakes you when an executor finishes.

## Requirements

**Dependencies**

- **Claude Code** — the only hard dependency.
- **Notifications** —
  - **iTerm2** (default): built-in, clickable, nothing to install.
  - **Terminal.app**: install `terminal-notifier` (brew) for clickable banners; without it you
    still get macOS's plain notification — it shows the info, but clicking does nothing.
- **Optional**: `pip3 install iterm2` + enable iTerm's Python API (Settings → General → Magic) —
  new executor tabs then open right next to the lead's tab instead of at the end of the tab bar.

**iTerm2 vs Terminal.app** — auto-detected via `$TERM_PROGRAM` (see [Config](#config)):

| | **iTerm2** (full experience) | **Terminal.app** |
|---|---|---|
| Executors open as | tabs next to the lead (or split panes) | new windows |
| Follow-up `send` | typed into the running session | reopens via `--resume` in a fresh window |
| Per-lead tab colors | yes | — |
| Clickable notifications | yes, nothing to install | only with terminal-notifier |
| `relay focus` | jumps to tab/pane, leads too | brings the window forward |
| `relay close` | closes the tab | window may linger (Cmd-W it) |

Fully local, no telemetry — see [PRIVACY.md](PRIVACY.md).

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
/relay:handoff <handoff.md>                 succeed this lead: pre-armed successor tab, then step down
relay status [session_id] [--statusline]   read-only, statusline-safe one-liner (see below)
```

Also: `relay list` hides closed/superseded/dead sessions by default; pass `--closed` to reveal them (capped at 15 most recent). `relay report <sid>` prints a finished report in a green banner, and
`relay prune [--days N] [--dry-run]` clears old closed/dead session state, and also clears dead
lead markers older than `--days` ("ghost" leads from crashed/abandoned tabs — a lead you're
actively using is never pruned). `relay diff <sid>
[--open] [--all]` renders an executor's `git diff --staged` to a self-contained, offline HTML page
(side-by-side via a vendored, checksummed diff2html — see [VENDOR.md](VENDOR.md) — with a stdlib
fallback if that bundle is missing or fails its integrity check) so you review diffs in one click
without spending model tokens on it. Its output (and every executor's closing line) includes a
cmd+clickable `file://` URL to the page.

Type them, or just describe what you want ("check on my sessions") — the lead invokes the right one.

### Status line integration (optional)

`relay status` is deliberately dumb: it reads markers + `session.json` files as they already are
and checks report-file existence — **it writes nothing** — so it's safe to call on every status-line
render (Claude Code can re-run your `statusLine` command multiple times a minute). It prints one
line: the LEAD view (busy-executor count, reported names, a `WAKE` warning if auto-wake is
unhealthy) or the EXECUTOR view (its packet + a pointer to its lead's tab) — whichever role the
current session has. For any other session it prints nothing, so it never adds noise to an
unrelated status line.

Claude Code pipes a JSON payload with a top-level `session_id` field to your `statusLine` command's
stdin (confirmed against the [statusline docs](https://code.claude.com/docs/en/statusline.md)). That
stdin is only read once — if your status line already parses it for other purposes, capture it into
a variable and re-pipe it to `relay status --statusline`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline.sh"
  }
}
```

```bash
#!/bin/bash
# ~/.claude/statusline.sh
input=$(cat)
# ... your existing statusline bits, reading from $input ...
echo "$input" | ~/path/to/relay status --statusline
```

If you'd rather not thread stdin through, `--statusline` is optional: `relay status
"$CLAUDE_CODE_SESSION_ID"` (or with no argument at all, since `relay status` falls back to that same
env var) works from a plain shell command with no JSON parsing.

Honest limit: `relay status` reads stored state + report-file existence only — no liveness refresh.
A crashed executor may still read `busy` in your status line until the next `relay list`/`relay
check` runs; those commands remain the decision surface for whether something actually needs
attention.

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
or auto-commits. You also get a macOS notification naming the project and executor. Three tiers,
first one that applies wins:

1. **iTerm native** (no external tool needed): the hook writes the notification straight to the
   lead's own tty using iTerm's OSC 777 escape. Clicking it **focuses the lead's session natively**
   — iTerm's own click-to-source behavior, confirmed live. No coalescing: several wakes in a row
   stack as separate banners rather than replacing one another.
2. **terminal-notifier** (if installed, and tier 1 didn't apply — e.g. Terminal.app, or the lead's
   iTerm session couldn't be resolved): clicking runs `relay focus <lead>`, and repeated wakes
   **coalesce** per lead (replace rather than stack) via `-group`.
3. **osascript fallback** (neither of the above): macOS's built-in `display notification`, same
   info, **not clickable**.

One-time gotcha for tier 1: macOS must allow iTerm to post notifications — **System Settings →
Notifications → iTerm → Allow Notifications** (iTerm's own in-app setting is not enough).

Tier 1's banners carry a **"Session …" title that iTerm forces** — no escape parameter overrides or
suppresses it. To get a clean, relay-set title/subtitle instead, set `"notify_via":
"terminal-notifier"` in the config: relay then skips the OSC tier and uses terminal-notifier (or
osascript if it's not installed). You lose the OSC tier's native click-to-the-posting-session, but
terminal-notifier's click still runs `relay focus <lead>`.

Wakes are scoped to executors the lead owns — multiple leads on different projects don't cross-wake.

Separately, relay nudges a lead **once** (ever, per session) when its transcript file grows past
`handoff_nudge_mb` (default 5MB) — a proxy for session weight. The suggested flow: write a handoff
md, then `/relay:handoff <md>`.

### Handing off a long-lived lead

Heavy session (large transcript, or just wanting a fresh context)? Distill what matters to a
handoff md — what's in flight, what's reviewed/committed, open questions, next steps — then run
`/relay:handoff <handoff.md>`. It opens a **pre-armed** successor tab (gate + auto-wake already
active, no `/relay:mode` needed), seeds it with a pointer to the handoff file, and steps this
session down as its final act. Inherited executors adopt automatically on the successor's first
`send`/`resume` — nothing to re-wire.

This is a different tool from `relay resume`/`restart`: **resume/restart is for CRASH
recovery** (reopens the identical conversation, same context back). **Handoff is for WEIGHT**
(deliberately starts a fresh context on a NEW session id). Use whichever matches the problem —
a crashed tab needs its old context back; a bloated one needs to shed it.

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

Settings live in `~/.relay-tasks/lead/config.json`. If absent, relay creates it with defaults; missing keys fall back to defaults; unknown keys are ignored; changes take effect on the next relay command or hook run.

| Setting | Default | What it does |
|---------|---------|------|
| `edit_line_threshold` | 40 | Block routing a single edit to executors if it adds this many lines or more |
| `block_on_new_file` | true | Block routing to executors when creating a new file |
| `grace_seconds` | 120 | Grace period (seconds) when lead uses `/relay:route retain` to bypass the gate |
| `auto_wake` | true | Wake idle lead when an executor reports |
| `surface_commits` | false | Wake idle lead to surface commits it made this turn (off by default; opt in if desired) |
| `poll_seconds` | 1800 | How long idle lead's report-watcher waits before timing out |
| `poll_interval` | 5 | Interval (seconds) for report-watcher to re-check for new executor reports |
| `notify_on_wake` | true | Send macOS notification when lead wakes to review |
| `notify_via` | "auto" | "auto" \| "terminal-notifier". "auto" uses iTerm's OSC banner first (native click→session, but iTerm forces a "Session …" title you can't override); "terminal-notifier" skips that tier for a clean title/subtitle (falls back to osascript) |
| `executor_skip_permissions` | false | Spawn executors with `--dangerously-skip-permissions` (false = prompt before edits/commands; true = hands-off but requires careful review before landing) |
| `terminal_app` | "auto" | "iterm" \| "terminal" \| "auto" (auto-detect via `$TERM_PROGRAM`; iTerm default) |
| `tab_colors` | true | iTerm only; color each lead's tab and its executors' tabs uniformly |
| `executor_layout` | "tab" | "tab" \| "pane" (pane = iTerm only, split into lead's window) |
| `handoff_nudge` | true | Suggest handing off once when the lead's transcript gets heavy |
| `handoff_nudge_mb` | 5 | Transcript-size threshold (MB) for the handoff nudge — a proxy for session weight, not context-window occupancy; calibrated on real sessions (a full working day ≈ 3MB, the heaviest marathon session ever ≈ 6MB) |

`poll_seconds` must stay under the `Stop` hook's `timeout` in `hooks/hooks.json` (currently 1900s) — the harness kills the hook's background poller at that timeout regardless of `poll_seconds`, so raising one without the other silently breaks auto-wake (see [async-rewake-findings.md](docs/async-rewake-findings.md#addendum-silent-auto-wake-death-2026-07-10)).

Per-spawn override for `executor_skip_permissions`: pass `--skip-perms` or `--no-skip-perms` at `relay spawn` time.

**Environment variable overrides:**
- `RELAY_TERMINAL`: force "iterm" or "terminal" (beats `terminal_app` in config)
- `RELAY_NO_NOTIFY`: suppress all notification banners (useful for tests, CI)

## Troubleshooting

- **`/relay:check --all`** tells you the real state (busy/reported/stalled/dead) — trust it over how
  a tab looks. `stalled` means go look at that tab.
- **Tab died mid-build?** `relay resume <sid>` reopens the same conversation with context and staged
  work intact; `relay restart <sid>` re-runs the packet fresh.
- **Executor finished but the lead never woke?** First check `relay list` — a **`WAKE=STALE`** on the
  lead means it's bound to a pre-fix wake hook and will keep missing late reports. Get it onto the
  fixed hook: `/plugin update relay@claude-relay` (if not already updated) → `/reload-plugins` →
  re-run `/relay:mode` to re-arm (which also re-stamps the version). Otherwise check
  `ls ~/.relay-tasks/lead/` — if empty, arming failed; re-run `/relay:mode`. A landed report surfaces
  on the lead's next idle either way, and `relay report <sid>` pulls it by hand. **After a lead
  handoff**, an inherited executor still owned by the retired lead won't wake you at all — run
  `relay list` and check the footnote for orphaned executors; `relay send`/`relay resume` adopt them
  automatically, or use `relay adopt <sid>` to re-point ownership without sending anything.
- **After updating relay, refresh running leads.** A plugin update only caches the new version — a live
  session keeps using the old hook path until you run **`/reload-plugins`** (which re-points hooks for
  the session; relay ships no monitors, so no full restart is needed). Then re-run `/relay:mode` on
  each lead so its next wake poller runs the new code and its marker re-stamps — `relay list`'s
  `VER`/`WAKE` columns confirm which version each lead is actually on. (A poller already in flight
  keeps the old code until it next arms, so re-arming is the clean step.)
- **A stale row in the LEADS table with an old LAST ACTIVE** is a dead lead (tab closed/crashed
  without `/relay:stop`) — `relay prune` clears it once it's older than `--days`; a lead you're
  actively using is never pruned.
- **A brand-new worktree may ask "trust this folder"** once — relay pre-approves this when it can;
  if not, click trust once.
- **`/plugin list` may not show relay** when loaded via `--plugin-dir` — a display quirk. If
  `/relay:list` responds, it's working.
- **Model note:** `/relay:mode` checks the session's model and recommends switching up if it's too
  weak to lead. Decide your model once, then arm — the self-check gets unreliable after repeated
  `/model` switches in one session.
