# 🚦 relay — a lead/executor pattern for Claude Code

A Claude Code plugin for macOS (iTerm2 or Terminal.app) that delegates work from one Claude Code
session (the lead) to executor sessions in their own terminal tabs, windows, or split panes, each
seeded with a work packet. The lead plans, delegates, and reviews; executors **stage their work
(never commit)**, write a report, and stay idle for reuse (parking themselves once their work has
landed); the lead reviews the staged diff and commits. You stay in the loop at every gate: the lead proposes a split and waits for your go, and
wakes you when an executor finishes.

## Why

- **Per-executor model choice.** The lead picks `--model` at spawn time (and again per respawn via
  `--supersede`) — a strong model does the thinking (decomposition, review, integration), cheaper
  models do the bounded implementation. Net effect: your highest-cost tokens go to judgment calls,
  not to grinding out diffs a smaller model can produce just as well.
- **Bounded packets narrow the failure surface.** Each executor gets a small, self-contained work
  order with explicit acceptance criteria — narrow scope leaves less room to wander off-spec, and
  the report + staged-diff review is where mistakes that happen anyway get caught, before they
  land.
- **Context isolation.** Each executor burns its own context window, not the lead's — the lead
  stays light across a long session. When it doesn't, the transcript-weight nudge and
  `/relay:handoff` exist for exactly that.
- **Real parallelism.** Independent packets run concurrently, each in its own tab — no serializing
  unrelated work through one context.
- **Lean, fenced executors.** An executor launches as a dedicated agent with no MCP servers (no
  connector/plugin tool rosters in its context every turn) unless its packet says `MCP: linear`;
  it cannot spawn sub-agents or `git commit`/`push` (denied, not just told), gets a pinned model,
  and parks itself when done. The lead carries the integrations; executors carry the diff.
- **Human gates throughout.** Nothing spawns without your go, nothing lands without your review,
  and executors stage, never commit.
- **Hallucination risk is contained, not eliminated.** An executor can be confidently wrong —
  invent an API, misread the spec, report tests green that it never ran. relay's answer is
  structural, not trust: acceptance criteria live in the packet, the report has a required format,
  and the lead independently verifies the staged diff (re-running tests itself) before anything is
  committed. Wrong output still costs a review cycle — it just doesn't land.

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
| Lead push-wake (`nudge-lead`) | yes | no — Terminal.app can't inject text into a running process; a Terminal-hosted lead degrades to its own at-Stop check + desktop notification |

**Why the lead's own model matters**: this role's value is judgment calls (what to delegate, when
to reuse a session, whether a report is truly mergeable) — that needs a strong reasoning model, and
no skill can switch it programmatically, so `/relay:mode` has the session say its own tier out loud
rather than trust it silently. Known limitation, confirmed empirically: the self-check becomes
unreliable after multiple `/model` switches within one continuing session (self-knowledge of "which
model am I" doesn't reliably refresh on every switch), and there's no known way to verify the
running model programmatically. Decide the model once at session start — the session's launch
model, or a single `/model` switch made before invoking `/relay:mode` — and don't switch again
expecting the check to stay accurate; start a fresh session instead if you need a different one.

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

**Updating** (marketplace install):

```
/plugin update relay@claude-relay
/reload-plugins
```

then, in any lead you keep using, take one normal turn and check `relay list` — its `VER` column
re-stamps itself from the live hooks, so if it shows the new version you're current; if it doesn't,
restart that session (details and why under [Troubleshooting](#troubleshooting)). A running
executor keeps the role/flags it was launched with until it is resumed or restarted. A local-clone
install picks up changes on the next `claude --plugin-dir …` launch.

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
5. Review each report + staged diff (the lead helps) and commit yourself. Finished executors park
   themselves (auto-close) — or send them follow-up packets.

More examples: [`textkit`](examples/textkit/) (parallel fan-out) and [`calc`](examples/calc/)
(the full serial → parallel → serial build).

## Mental model

Deeper dive with diagrams: [docs/how-relay-works.md](docs/how-relay-works.md).

The flow, in five beats:

1. **Design** — tell the session what to build, or point it at a brief.
2. **`/relay:mode`** — arm it as the lead. (Order is flexible: arm first and then describe the
   work, or design first and arm after — both work. Armed with no task yet? Don't invent one —
   say you're ready and wait to be told what to build, then propose the split as usual.)
3. **Approve the split** — the lead proposes executors + packet files and **waits for your go**.
4. **Spawn** — executors build in parallel, each in its own tab/pane, each on the model the lead
   picked for it (`--model`, per executor — see [Why](#why)); the lead wakes you as each one
   reports.
5. **Review → commit** — diff page per executor, you approve, the lead commits. Finished
   executors then **park themselves** (auto-close once their work has landed or they've idled out;
   `relay send` brings one back with full context) — or take follow-up packets.

And the three nouns:

- A **session** = one executor in its own terminal tab (or window/pane), working one worktree/topic. It stays alive
  across packets — one engineer you keep assigning related work to, not a disposable one-shot.
- A **packet** = a work order (a `.md` file). The rules every executor follows (stage-don't-commit,
  one deliverable per packet, required report format) live in the executor agent's system prompt;
  relay appends only the per-packet report path and closing steps — you never write those.
  A packet also declares the MCP servers its executor needs (`MCP: linear`), if any — executors
  launch with none otherwise.
- A **report** = what the executor writes back when done, at a path relay assigns.

## Commands

```
/relay:mode                          adopt the lead role (arms the gate + auto-wake)
/relay:spawn <worktree> <topic> <packet.md> [--model] [--name] [--seed] [--mcp SPEC] [--effort LEVEL] [--keep]
                                            MCP/effort/context window: see Executor MCP servers,
                                            Executor effort, Executor context window below; --keep pins against auto-close
/relay:send  <session_id> <packet.md> [--rotate] [--upgrade] [--when-idle]
                                            follow-up into the SAME session (reuse > respawn); flags: see Retiring a heavy executor / Queueing below
relay queue <session_id> [--cancel ID|all] show/cancel packets queued with --when-idle
/relay:check [<session_id> | --all]        busy / reported / stalled / dead
/relay:board [--open] [--out PATH] [--lead] one HTML page for everything: leads → executors → packet timelines, status, launch, tokens, warnings; light/dark toggle
relay stats [--lead SID] [--since DAYS] [--json]   one row per packet ever sent → outcome (rounds, verdict, status) + a token trailer and a SUMMARY; see below
relay doctor [--offline] [--quick]         prove the installed claude CLI still honours relay's launch flags + plumbing; run after every Claude Code update
relay lint <packet.md> [--worktree W] [--model M] [--strict]   advisory packet checks (MCP undeclared, big reading on 200K, no Preconditions, shape hints…)
/relay:list [--all-leads]                  leads + active executors (closed hidden; --closed shows); TOKENS and LAUNCH (mcp/context/role); parks finished ones
                                            other projects' ghost/paused leads collapse to one line; --all-leads shows them
/relay:close <session_id> [--supersede <new_id>]   (rarely needed — finished executors auto-close)
relay keep <session_id> [--off]            pin/unpin an executor against auto-close
/relay:retire <session_id> [--force]       close it AND leave a successor-seed.md, so respawning fresh over the same territory is cheap
/relay:stop                                unarm: step down from lead mode (gate + auto-wake off)
/relay:focus <session_id>                  jump to that session's tab/pane/window (executor or lead)
/relay:resume <session_id> [--mcp SPEC]    reopen a dead tab's conversation, context intact
/relay:restart <session_id> [--mcp SPEC]   re-run a dead session's packet fresh (loses context)
/relay:route retain "<reason>"             open a grace window when the gate blocks lead work
/relay:auto on|off|status                  autonomous posture: proceed by default on routine, in-plan steps (per-session; committing still stops)
/relay:diff <session_id>                   render staged changes to an HTML review page and open it
/relay:verify <session_id> [--rerun]       machine-check a report against its staged reality: MALFORMED / MISMATCH / INCONCLUSIVE / COUNTS-MATCH (never "PASS" — see below for why)
relay verify <sid> --for-autocommit [--in-plan] [--diff-reviewed]   the auto-commit gate: CLEARED / NOT-CLEARED-BECAUSE-… (flags are the lead's own attestations)
/relay:handoff <handoff.md>                 succeed this lead: pre-armed successor tab, then step down
relay close-predecessor                    successor-only: close the outgoing lead's tab, on user go
relay tidy [--dry-run]                     re-group the tab bar: [Lead] [Exec 1] [Exec 2] … [Lead 2] … + re-apply each lead's color
relay status [session_id] [--statusline]   read-only, statusline-safe one-liner (see below)
relay whoami [<token>] [--json]            "who am I, who is my lead" — token: a lead's session id, an executor's relay name, or its claude_session uuid; defaults to $CLAUDE_CODE_SESSION_ID
```

Anywhere a command above takes a session id, you can pass the executor's name (set at `spawn
--name`), a lead's project name, or a unique prefix of either's id — no more pasting lead UUIDs:
`relay focus webapp` and `relay stop docs-site` just work. A project name matching more than one
lead (e.g. an old + new lead after a handoff) never guesses — it lists every candidate, newest-first.

Also: `relay list`'s `TOKENS` column is real spend — prompt (incl. cache reads/writes) / output
tokens from the executor's transcript; `--json` carries the full breakdown (`usage`: input,
cache_read, cache_create, output, requests, models). `LAUNCH` is `mcp/context/role`
(`none/1m/A`: no MCP servers, 1M window, agent-roled — `none/200k/A` for 200K; `G` = pre-agent
session on full-GATES packets). `relay list` hides closed/superseded/dead by default (`--closed`
reveals, capped at 15). `relay report <sid>` prints a finished report in a green banner; `relay
prune [--days N] [--dry-run]` clears old closed/dead state and stale lead markers (a lead you're
actively using is never pruned). `relay diff <sid> [--open] [--all]` renders an executor's `git diff
--staged` to a self-contained, offline HTML page (vendored, checksummed diff2html with a stdlib
fallback — see [VENDOR.md](VENDOR.md)) so you review diffs in one click, with the report's outcome
sentence and status at the top; its output (and every executor's closing line) includes a
cmd+clickable `file://` URL.

Type them, or just describe what you want ("check on my sessions") — the lead invokes the right one.

### Status line integration (optional)

`relay status` is deliberately dumb: it reads markers + `session.json` as-is and checks report-file
existence — **it writes nothing**, so it's safe on every status-line render. It prints the LEAD view
(busy/reported executor names, a `WAKE` warning if unhealthy) or the EXECUTOR view (packet + state,
"for <project>" when owned) — whichever role the session has; any other session gets nothing.

Claude Code pipes a JSON payload (a top-level `session_id` field) to your `statusLine` command's
stdin, and that stdin is read only once — if your script already parses it for other purposes,
capture it into a variable and re-pipe it to `relay status --statusline`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline.sh"
  }
}
```

A marketplace install lands at a **versioned** path with no `latest`/`current` symlink, and the
version changes on every `/plugin update` — hardcoding it silently breaks your status line's relay
segment on the next update. Resolve it version-agnostically instead:

```bash
#!/bin/bash
# ~/.claude/statusline.sh
input=$(cat)
# ... your existing statusline bits, reading from $input ...
relay_bin=$(ls -d "$HOME/.claude/plugins/cache/claude-relay/relay"/*/bin/relay 2>/dev/null \
            | sort -V | tail -1)
echo "$input" | "$relay_bin" status --statusline
```

A ready-to-copy, POSIX-`sh` version — plus the resolver's "not found" case and both the plain and
`🚦:(...)`-wrapped rendering: [`examples/statusline.sh`](examples/statusline.sh).

No stdin to thread? `--statusline` is optional: `relay status "$CLAUDE_CODE_SESSION_ID"` (or with no
argument at all, since it falls back to that same env var) works from a plain shell command.

The LEAD view also carries a context-weight segment — an early warning before the one-shot
[handoff nudge](#handing-off-a-long-lived-lead) fires — appearing only via `--statusline`.
Token-first: live context (from 60% of `lead_nudge_tokens` up — a lead's OWN line, window-capped;
see the Config table — never the executors-only `context_nudge_tokens`) is the primary reading;
transcript-MB rides alongside once it passes `handoff_nudge_mb` (MB never shrinks, so a big number
alone still means several compactions in):

```
🚦 busy: tk-parser,tk-render · ✅ tk-auth · 84k ctx
🚦 busy: tk-parser · 84k ctx · 5.2MB → /relay:handoff
```

Honest limit: `relay status` reads stored state + report-file existence only — no liveness refresh.
A crashed executor may still read `busy` until the next `relay list`/`relay check`; those commands
remain the decision surface for whether something actually needs attention.

## The routing gate (friction, not trust)

Once armed, a hook **blocks large inline edits** by the lead (over ~40 new lines, or creating a new
file) and tells it to delegate — or to run `/relay:route retain "<reason>"` for genuinely
lead-appropriate work (a ~2-minute grace window). Small review-class fixes pass silently. Packet
files are exempt — writing packets is the lead's job. Every block and retain is logged to
`~/.relay-tasks/sessions.jsonl`.

Honest limits: it does **not** gate `Bash` (`git commit`, `sed -i`, heredocs pass ungated — that
discipline stays on the lead), and it only acts in `/relay:mode` sessions — every other session on
the machine is untouched (the hook fast-exits, fail-open).

## Verifying a report (and why it can't tell you the report is true)

`relay verify <session_id>` machine-checks an executor's report against the staged reality in its
worktree, and stamps one of four verdicts:

| Verdict | Exit | Means |
|---|---|---|
| `COUNTS-MATCH` | 0 | The checkable numbers agree. **That is all it means.** |
| `MISMATCH` | 1 | A claim contradicts staged reality — a file claimed but never staged, a "staged" confirmation over an empty index, a declared test count that doesn't reproduce. |
| `MALFORMED` | 2 | The report breaks the REPORT FORMAT's TL;DR contract — usually a missing `UNVERIFIED:` line, which reads as *malformed*, never as "nothing to report". |
| `INCONCLUSIVE` | 3 | A check you asked for didn't complete. Not a pass with a caveat — nothing was compared. |

It checks the TL;DR block's four mandatory fields; the files the report claims under "What changed"
against `git diff --cached --name-only`; that the work is staged and uncommitted; and the test
counts and commands the report declares. It **echoes the report's risk flags and UNVERIFIED lines
verbatim** on every run — surfacing them, never absorbing them, and never letting them change the
verdict, because grading them is your job. Each run lands a `report_verify` event in the ledger.

Claimed paths are matched **by suffix** when the literal path isn't staged: a report whose "What
changed" is written relative to a subdirectory (`lib/types.ts` under a heading that says "paths
relative to `app/src`") resolves against the one staged `app/src/lib/types.ts` and is *confirmed*,
not accused — the match is segment-anchored, and a claim that names a real repo file is never
re-pointed. Two staged files matching one claim is genuinely ambiguous: it stays a `MISMATCH`, and
the note names both candidates.

Declared test commands are **not re-run by default**, so verify stays fast; `--rerun` runs them.
Only pytest-shaped commands with no shell metacharacters are ever executed, argv-only and never
through a shell — a report is text an executor wrote, and this must not become a way for one to run
arbitrary commands in your worktree.

**Now the important part.** A `COUNTS-MATCH` **must never be read as "the report is true."** Across
~15 real executor reports, a counts-verifier would have caught exactly **one** thing (a lint miss).
Every *dangerous* problem was premise-level — wrong oracle, wrong write-side, suite green in the
wrong venv — and all of those are **invisible** to re-running the declared commands. So the tool
says what it can prove and no more; there is deliberately no "PASS", no "VERIFIED" and no "clean"
anywhere in its vocabulary, and the caveat is printed on every single run, not just failures.

Use it as a cheap pre-filter that tells you where to look harder — **the lead's judgement on the
staged diff stays the real check.** A verifier that displaces that judgement makes relay *less*
safe, because it trades the thing that caught the real problems for the thing that catches lint
misses. That is also why a zero exit code is not, by itself, permission to commit.

**By default, that judgement runs through a fork, not your own context.** `/relay:review <sid>`
launches a same-model `Agent(subagent_type: "fork")` with a fixed prompt: it runs this verify
command and quotes the verdict, quotes the report's TL;DR, reads the FULL staged diff against the
packet's goal and acceptance criteria, runs the acceptance commands, and returns numbered findings
plus a one-line recommendation — under 40 lines, in the fork's own context, not yours. The fork
reads the whole diff every time — that applies to every project relay leads, with no blanket
carve-out for relay's own gated files. Read the findings, not the diff; open hunks inline yourself
only when a finding names a sign-off-gated path (`hooks/`, `lib/lead_guard.py`, ledgers) AND the
change there is more than a guard clause or a rename — a second look at those hunks, never a
blanket re-read. One 15-file review used to cost a lead ~40k tokens reading the diff inline; the
fork keeps that in its own disposable context and hands back a few thousand tokens of findings
instead. `relay check`'s output and `relay list`'s report footnote both show `(diff: N files
+A/-D)` next to a ready report, so you can see up front how big the fork's read will be.

## Autonomous mode

Sometimes you're confident about the plan and the approval round-trips are pure ceremony. `/relay:auto
on` inverts the lead's **default posture** for that session: instead of waiting on every routine beat,
it **proceeds and announces**. Same judgement, burden flipped — it still asks the moment it judges it
needs you.

```
/relay:auto on        # proceed by default on routine, in-plan steps
/relay:auto off       # back to announce-and-wait (the default)
/relay:auto status    # current posture + where it came from (command vs config)
```

What gets automated is only the reflexive-yes beats: sending the obvious next packet in an approved
plan, spawning an executor that plan already names, reviewing a clean report. **This is not the lead
deciding things unilaterally** — it never expands the plan, and it stops on all of:

- a report with a risk flag / failing tests / UNVERIFIED claim bearing on correctness;
- core logic, ledgers, parity/golden tests, migrations, deploys (the sign-off gate, unchanged);
- irreversible or outward-facing actions (push to a shared branch, delete, external send);
- new work not in the approved plan;
- genuine ambiguity the packet or plan can't resolve.

**Committing executor work has its own gate on top of the posture.** Turning autonomous mode on does
not by itself license a commit. In auto, the lead may commit without asking **only when all five of
these hold**:

1. `relay verify` says `COUNTS-MATCH` (`MISMATCH` / `MALFORMED` / `INCONCLUSIVE` always stop);
2. the report's TL;DR is `Status: clean`, `Risk flags: none`, `UNVERIFIED: none` — **`clean-with-caveats` stops**;
3. the packet was in the approved plan;
4. nothing sign-off-gated is touched — core logic, ledgers, parity/golden tests, migrations, deploys (and, for relay's own repo, `hooks/`, `lib/lead_guard.py`, ledger formats);
5. **the diff has been reviewed** — by default via [`/relay:review`](#verifying-a-report-and-why-it-cant-tell-you-the-report-is-true)'s fork, which reads the whole diff every time with no blanket carve-out for relay's own gated files; hunks opened inline only when a finding names a sign-off-gated path AND the change there is more than a guard clause or a rename.

```
relay verify <session_id> --for-autocommit --in-plan --diff-reviewed --findings <path>
```

`<path>` is wherever the fork's returned findings were saved — verify copies it into the session's
packets dir as a durable record and ledgers the finding count. Omit `--findings` (keep bare
`--diff-reviewed`) only when the diff was read inline instead. This prints `AUTO-COMMIT: CLEARED` or
`AUTO-COMMIT: NOT-CLEARED-BECAUSE-<reason>`, exits 0 only when cleared, and records an `auto_commit`
ledger event with the verdict and diff stat. Conditions 3 and 5 are not machine-knowable — they are
**the lead's explicit attestations**; without both flags the answer is always NOT-CLEARED, and every
NOT-CLEARED path falls back to stopping and asking.

This is the one place the verifier's own caveat matters most — see
[Verifying a report](#verifying-a-report-and-why-it-cant-tell-you-the-report-is-true) for why `COUNTS-MATCH` is never truth and condition 5 exists regardless.

Condition 4's stop-list is per-machine extensible: config key `signoff_paths` (default `[]`) takes a
list of repo-relative path substrings, matched exactly like the built-ins, and **merges with** them —
it can only ever add a marker, never remove or replace one of the built-ins above. A hit against a
configured entry names its source as `configured in signoff_paths` in the NOT-CLEARED detail line, so
it's never mistaken for a built-in. For example, `{"signoff_paths": ["billing/", "lib/pricing.py"]}`
in `~/.relay-tasks/lead/config.json` stops the gate on any staged change under `billing/` or to
`lib/pricing.py` on that machine, on top of everything condition 4 already checks. `relay doctor`
prints the effective list (built-in + configured) so a lead can see at a glance what will stop the
gate here.

Autonomy never becomes silence: every autonomous action is announced *with the round-trip it
replaced* ("proceeded: sent packet 003 — under manual mode this would have waited for your go"),
logged to `~/.relay-tasks/sessions.jsonl`, and stamped on the lead's row in `relay list` (an `AUTO`
column plus a footnote naming it) — a proceeded-without-you lead is **more** visible, not less.

The posture lives in that lead's own marker, so it's per-session and **resets on every fresh arm**
(`/relay:mode`) to whatever `autonomous_mode` config says — it can't silently outlive the plan you
scoped it to. Set `autonomous_mode: true` if you always work this way; the command still overrides it
either direction.

## Auto-wake and notifications

While the lead sits idle, a Stop hook watches in the background. When an executor's report lands,
the lead **wakes**, announces what's ready, and **waits for your direction** — it never auto-reviews
or auto-commits (unless you've turned on [autonomous mode](#autonomous-mode), where committing still
needs all five auto-commit conditions above). You also get a macOS notification naming the project
and executor. Three tiers, first one that applies wins:

1. **iTerm native** (no external tool needed): writes straight to the lead's own tty via iTerm's OSC
   777 escape. Clicking it **focuses the lead's session natively** (confirmed live). No coalescing —
   repeated wakes stack as separate banners.
2. **terminal-notifier** (if installed and tier 1 didn't apply — e.g. Terminal.app, or the lead's
   iTerm session couldn't be resolved): clicking runs `relay focus <lead>`; repeated wakes
   **coalesce** per lead via `-group`.
3. **osascript fallback** (neither of the above): macOS's `display notification`, same info,
   **not clickable**.

One-time gotcha for tier 1: macOS must allow iTerm to post notifications — **System Settings →
Notifications → iTerm → Allow Notifications** (iTerm's own in-app setting is not enough).

Tier 1's banners carry a **"Session …" title that iTerm forces** — no escape parameter overrides it.
Set `"notify_via": "terminal-notifier"` in the config for a clean, relay-set title/subtitle instead
(skips the OSC tier, falling back to osascript if terminal-notifier isn't installed; you lose native
click-to-the-posting-session, but terminal-notifier's click still runs `relay focus <lead>`).

**A second layer underneath.** Every spawned executor also carries a one-shot Stop-hook push (`relay
nudge-lead`, internal plumbing). Once its report lands and it goes idle, it fires once: it types
into the lead's tab if the lead hasn't already surfaced the report, or notifies you directly if the
owning lead is gone (crashed/closed/pruned). A net under the lead's own poller, not a replacement.

Wakes are scoped to executors the lead owns — multiple leads on different projects don't cross-wake.
Ownership changing hands carries the "already seen" stamps with it: `relay adopt` / `relay send` /
`relay resume` (adopt-on-claim) and a handoff's re-parenting each copy that executor's surfaced and
pending entries onto the new owner, so a report the previous lead already reviewed doesn't re-wake
the new one as brand new. Belt and braces on the same failure: the wake skips any report whose
claimed files are **already clean at HEAD** — the same `landed` test auto-close uses — and ledgers
`wake_skipped_landed` instead of announcing it. Landing is a terminal outcome, so that skip also
stamps the report surfaced: it is asked about once, not re-tested on every Stop hook and named by
`relay list` forever (which applies the same landed filter to its "NOT yet proven delivered" line).
That test is deliberately conservative: an unreadable worktree or a report claiming no paths still
wakes you.

Separately, relay nudges a lead **once** ever on two signals: primarily live context
(`lead_nudge_tokens`, default 300k on a 1M window — a lead's OWN, higher line; a lead's handoff
costs more than an executor's rotation and its context grows slowly once diffs are reviewed by a
fork, so it earns more room than the executors-only `context_nudge_tokens`, 150k, before nudging),
secondarily transcript size on disk (`handoff_nudge_mb`, default 5MB, which never shrinks). When
the lead's real context window is known (via `relay doctor`'s probe, or inferred from its model) to
be 200k rather than 1M, the effective line is capped to `context_nudge_tokens` instead — a
200k-window lead can never actually reach 300k live context, so it's still nudged, just on the same
line an executor would be. Either threshold fires the nudge; the flow is the same either way —
write a handoff md, then `/relay:handoff <md>`. When a wake carries this nudge, the lead surfaces it
alongside whatever else woke it and lets the user decide whether to hand off — it never steps down
or starts a fresh session unilaterally.

### Handing off a long-lived lead

Heavy session (large transcript, or just wanting a fresh context)? Distill what matters to a
handoff md — what's in flight, what's reviewed/committed, open questions, next steps — then run
`/relay:handoff <handoff.md>`. A handoff file should fit one screen — it's a distillation for the
successor to read once, not an archive. It opens a **pre-armed** successor tab (gate + auto-wake already
active from turn one), seeds it with a short pointer at a relay-prepared copy of your handoff file
(your source md is untouched — relay appends a SUCCESSOR AFTERCARE section to its own copy), and
steps this session down as its final act. Inherited executors adopt automatically on the
successor's first `send`/`resume` — nothing to re-wire. Once settled, the successor runs
`/relay:mode` to verify the pin held (idempotent), then asks you whether to close the predecessor's
now-unarmed tab — say yes and it runs `relay close-predecessor`.

This is a different tool from `relay resume`/`restart`: **resume/restart is for CRASH
recovery** (reopens the identical conversation, same context back). **Handoff is for WEIGHT**
(deliberately starts a fresh context on a NEW session id). Use whichever matches the problem —
a crashed tab needs its old context back; a bloated one needs to shed it.

### Queueing a packet for a busy executor

`relay send` refuses a `busy`/`stalled` session on purpose — typing into a session mid-turn can
corrupt it. The workaround leads reached for was a shell loop (`until relay check … ; do sleep 60;
done`), which burns the lead's turn, is invisible to relay, and dies whenever that turn ends.

`relay send <sid> <packet.md> --when-idle` replaces it: the packet is persisted under the session's
state dir and delivered automatically the next time that session goes idle. The trigger is the
executor's **own Stop hook** — the same one that pushes its report to the lead — so there is no
poller anywhere; a lead's `relay check` is only a net for sessions whose hook isn't armed. Delivery
runs the normal send path, so a queued packet is numbered, footer-stamped at delivery time, and
ledgered (`packet_queued` and `queue_delivered` are separate events — a queued packet that never
landed can never look like one that did).

Queued packets deliver **oldest-first, one per idle transition** — delivering two at once would
inject the second mid-turn, which is the whole thing being avoided. `--when-idle` against an
already-idle session simply sends immediately, and it does not soften the other refusals
(`superseded`, `launch-failed`). `relay check` shows a 📥 queued count; `relay queue <sid>` lists
what's pending, and `relay queue <sid> --cancel <id|all>` is the cancel path.

### Retiring a heavy executor

Handoff is for a heavy **lead**. `/relay:retire <session_id>` is the same idea one level down, for a
heavy **executor**: it closes the session exactly like `/relay:close`, but first writes a
`successor-seed.md` into its state dir — an index of every packet it was sent, each with its
report's outcome line, `Status`, `Risk flags` and `UNVERIFIED`, plus the worktree and topic it
owned. It's an index, not a transcript: the detail stays in the linked reports, and it's generated
from what's already on disk, so retiring costs the retired session nothing (and works fine on one
that's already dead or stalled).

Then spawn the successor with `relay spawn <worktree> <topic> <packet.md> --seed <retired_id>` — the
seed is appended to the fresh executor's packet as inherited context (task first, seed second, GATES
last), so it starts knowing the territory. The point is economics: picking up where a retired
executor left off costs one packet-read instead of archaeology, which is what makes rotating a heavy
session something you'll actually do rather than piling one more packet onto it.

Retire refuses on a session that's still busy with an unreported packet — that work would die
unsummarised — unless you pass `--force` (the packet is then seeded as `NO REPORT`).

`relay send <sid> <packet.md> --rotate` is the one-step form: retire the session, spawn
`<sid>-r2` over the same worktree/topic/scope/model/MCP set — widened to the 1M window when the
tier has one — with the seed inherited and this packet as its first. The heaviness gate's refusal
message points at it. The retired session's `--when-idle` queue travels to the successor, minus the
packet you are rotating in: a packet that was queued and is now being sent as 001 would otherwise
run twice, so its queue entry is dropped instead of moved (`queue_deduped`, and the rotate output
says how many).

### The board: one page for everything

`/relay:board` (`relay board --open`) renders a self-contained HTML snapshot of everything relay
knows: a summary strip, warning banners (orphaned executors, reports not yet proven delivered to
their lead, heavy sessions, stale wake hooks), then one card per lead with its executors — status,
model + `LAUNCH`, `TOKENS` (with cache warm/cold and a hit-rate chip)/MB, packet count — each row expanding to the executor's packet timeline
(gist, the report's outcome sentence and TL;DR, links to the packet / report / diff page) and
copyable `relay …` commands. Filter box, "show closed", light theme by default with a remembered
☀️/🌙 switch. It is built from exactly the functions `relay list` uses (and runs the same liveness
refresh and auto-close sweep), so it can't disagree with the table; by default it is a **snapshot** —
re-run to refresh. `relay board --live` (or config `board_live: true`) keeps it live instead, still
with no server process: it writes a sibling `board.json` next to `board.html`, adds a meta-refresh
(`board_refresh_seconds`, default 10s) so an open tab reloads itself, and relay then rewrites both
files in place on every `list`/`check`/`send`/`spawn` and the lead's own Stop hook — so a page left
open stays at most one turn stale, with the header's "updated HH:MM:SS" turning red once nothing has
rewritten it for 3× the refresh interval. Written to `~/.relay-tasks/board.html` (`--out` to change),
`--lead <sid>` to scope, `--json` for the data, `--live off` to turn live mode back off (removes the
`board.json` sidecar; a lingering `board_live: true` in config still holds it on).

### relay stats

`relay stats` joins each packet's (model, effort) to what happened, checkable against the rubric in
[Executor effort](#executor-effort) instead of memory. Reads ONLY what's on disk (ledger, packets,
reports, `relay list`'s usage cache) — writes nothing. Definitions:

- A **packet** = one `NNN-packet.md` under a session's packets dir.
- A packet is **landed** when its report exists, claims ≥1 path under "What changed", and every
  claimed path is clean in the worktree at a moment relay looks (`send`, `list`/`check`, or the
  auto-close sweep). Recorded once per (session, packet); a report claiming no paths never lands.
- **Rounds** = packets sent to the same session after this one, up to the lead's next commit
  boundary (`auto_commit` or `landed` — see `landed` above); relay's ledger doesn't record follow-up
  linkage directly, so this is the fallback, and it renders `-` when neither event follows.
- **Verdict** = the last `report_verify` event for that session+packet (`COUNTS-MATCH` / `MISMATCH` /
  `MALFORMED` / `INCONCLUSIVE`, see [Verifying a report](#verifying-a-report-and-why-it-cant-tell-you-the-report-is-true)), else `-`.
- **Tokens**: per-packet spend isn't recorded, only per-SESSION prompt/output; `relay stats` reports
  that total plus packet count, so `tok/pkt (avg)` is an honest average, not a per-packet measurement.

The table is one row per packet (SESSION, PKT, MODEL, EFFORT, ROUNDS, VERDICT, STATUS), a
per-session token trailer, then a SUMMARY grouped by (MODEL, EFFORT): packet count, mean rounds, %
`COUNTS-MATCH`, % exactly `Status: clean`, avg tok/pkt — closed/dead sessions included, that's where
the history is. `--lead <sid>` scopes to that lead (+ unowned); `--since DAYS` filters on send time;
`--json` emits `{rows, sessions, summary}`.

### Auto-close: finished executors park themselves

Executors used to sit idle for hours after reporting because nobody said `relay close` — tabs piling
up, `relay list` full of noise. Now relay parks them on two deterministic signals, no model call:

- **landed** — the report's "What changed" files are clean in the worktree (nothing staged, modified
  or untracked for them): you committed or discarded the work. Immediate, after a 2-minute grace.
- **landed (no change)** — an **ops** report that stages nothing on purpose: `Status: clean` plus a
  "What changed"/`Changed:` that positively says none/nothing, and a worktree that really is clean.
  It claims no files, so the rule above can never fire for it; without this it sat out the whole
  timer having finished. Same 2-minute grace. A blocked/partial/caveated or malformed report never
  lands this way, and neither does one whose worktree state git can't read.
- **idle** — reported for longer than `auto_close_idle_minutes` (default 60; 0 turns the timer off).

Both require that the owning lead has **already seen the report** (the auto-wake dedup set) — relay
never parks a report nobody looked at — and never touch busy/stalled sessions, ones with a
`--when-idle` queue, unowned ones, or pinned ones (`relay keep <sid>` / `spawn --keep`; `keep --off`
unpins). A heavy session (past the handoff threshold) is **retired** (seed written) instead of closed.

One exception to "ones with a `--when-idle` queue": a queue whose **head is undeliverable because
the session is heavy** is a deadlock, not work in flight — that session can neither receive the
packet (the heaviness gate refuses) nor be parked, so it used to sit `reported` forever. Such a
queue counts as **empty** for both rules; when the sweep parks the session it cancels the stuck
queue and ledgers `queue_cancelled_on_park` naming each packet's source, so nothing vanishes
silently. Until then `relay list` says so in words: `🛏 <sid>: not auto-closed earlier — queue
stuck: 1 packet undeliverable (heavy) — `relay send <sid> <packet> --rotate` carries it to a
successor`. A queue stuck on anything transient (tab unreachable, mid-supersede) still holds the
session open — it may yet deliver.

A **pin** records who set it and when (`keep_by` / `keep_at`, written by both `relay keep <sid>` and
`spawn --keep`), so `relay list` can say `📌 <sid>: pinned by <project> <age> ago — auto-close leaves
it alone; `relay keep <sid> --off` releases it` instead of a bare 📌 nobody can date. Pins survive a
handoff, so `relay handoff` names the ones the successor is inheriting (`📌 N pinned executors
inherited: … — `relay keep <sid> --off` to release`) — on the outgoing lead's console **and** as an
item in the successor's own handoff copy, which is the only one of the two the successor reads.
The sweep runs on `relay check`, `relay list`, and every lead turn-end (the Stop hook), scoped to
the lead's own executors; the ledger records `auto_closed` (and, right before it, a `landed` event
when the reason is "landed" — the same fact `relay stats` ROUNDS reads, see below) and `relay list
--closed` shows `closed (auto)`.

Closing is parking, not loss: the report is on disk, staged work stays in the worktree, and
`relay send <sid> <packet>` to a closed session resumes the **same conversation**.

## Telling tabs apart

- **Role-prefixed titles**: lead tabs are `[Lead] <project>`, executor tabs `[Exec] <session>`.
  Claude Code re-titles a fresh tab on its own a few seconds after launch, which used to clobber
  `[Exec]`; relay now re-asserts the label in the background past that point, so it holds. `send`
  and `focus` also address tabs by their iTerm session id rather than the (mutable) title, so a
  clobbered or shared title never misdirects them to the wrong tab.
- **Unique lead names**: if a lead's project name collides with another *live* lead's, relay
  auto-suffixes it (`claude-relay` → `claude-relay-2`) and prints a note; a crashed/stale lead never
  holds onto its name, so re-arming in the same folder reclaims the base name.
- **Per-lead tab colors** (iTerm only): each lead gets a stable color from a 6-color palette, and
  every executor it spawns inherits it — so with multiple leads running, one glance groups each
  lead with its workers. The color follows a lead's *identity*, not its session: a handoff
  successor keeps its predecessor's color, and an executor that changes hands (adopted, or
  re-parented at a handoff) is repainted in its new owner's color rather than keeping its old
  one's. Disable with `"tab_colors": false`.
- **Grouped tab order** (iTerm only): `relay tidy` puts the tab bar back into `[Lead]` `[Exec 1]`
  `[Exec 2]` … `[Lead 2]` `[Exec 2.1]` … order — each lead followed by the executors it currently
  owns, in spawn order — and re-applies every lead's color to its own group on the way past. It
  runs automatically after the events that disturb the order (spawn, `send --rotate/--upgrade`,
  handoff, `close-predecessor`, resume, restart); `"tidy_tabs": false` turns that off, and
  `relay tidy --dry-run` prints the order it would apply without moving anything. Executors in a
  different window from their lead are left where they are. Needs the same optional `iterm2`
  package and Python API toggle as adjacent-tab placement below — without them it degrades to one
  dim line, never a failed command. The automatic tidy races iTerm's own tab creation, which drops
  the API socket without a close frame, so a **connection** failure is retried once after a second
  (a "no tab holds these ids" answer is not — it would just be re-asked). Every tidy that reaches
  iTerm ledgers its outcome — `tidy` (windows reordered, ids unclaimed) or `tidy_skipped` (reason,
  attempts) — and `relay list` names a lead whose last tidy was skipped, since the dim line itself
  scrolls away.
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
| `executor_default_model` | "sonnet" | Model an executor launches with when `--model` is omitted — relay's own policy, never the CLI's personal `/model` default. An alias (`sonnet`/`opus`/`haiku`/`fable`, optionally `[1m]`) is **resolved through this machine's Claude Code at spawn** and the executor is launched with the concrete id (`claude-sonnet-5[1m]`), so the same alias can't mean different models on different machines and `[1m]` always rides a full id; cached per CLI version in `~/.relay-tasks/models.json`, shown in `relay doctor`. A full id is passed through untouched; an unrecognised model is refused before any tab opens |
| `executor_model_ceiling` | "opus" | Spawn refuses a requested executor model above this tier unless `--model-override "<reason>"` is passed (recorded in the ledger) |
| `executor_default_effort` | "high" | Thinking effort an executor launches with when neither `--effort` nor a packet `EFFORT:` line pins one — relay's own policy (the CLI default), never your personal `effortLevel` from `~/.claude/settings.json`. Validated against `--effort`'s own levels (`low`\|`medium`\|`high`\|`xhigh`\|`max`); an invalid value is refused at spawn |
| `terminal_app` | "auto" | "iterm" \| "terminal" \| "auto" (auto-detect via `$TERM_PROGRAM`; iTerm default) |
| `tab_colors` | true | iTerm only; color each lead's tab and its executors' tabs uniformly |
| `tidy_tabs` | true | iTerm only; after a spawn/rotate/handoff/close-predecessor/resume/restart, re-order the tab bar into `[Lead] [Exec…] [Lead 2] [Exec…]` and re-apply each lead's color to its group (see [Telling tabs apart](#telling-tabs-apart)). Needs the optional `iterm2` package + iTerm's Python API; degrades to one dim line without them. `relay tidy` runs regardless of this key |
| `executor_layout` | "tab" | "tab" \| "pane" (pane = iTerm only, split into lead's window) |
| `handoff_nudge` | true | Suggest handing off once when the lead's transcript gets heavy |
| `handoff_nudge_mb` | 5 | Transcript-size threshold (MB) — the secondary "session age" (compaction-count) signal for **both** leads and executors: MB on disk never shrinks, so a big number alone means several compactions in even when live context currently looks fine. Fires the lead's handoff nudge/statusline segment alongside tokens, and is the executor fallback reading (`relay send`'s gate, `relay list`'s heavy footnote) only when a transcript can't be parsed for real usage at all |
| `context_nudge_tokens` | 150000 | The cost/context signal for **executors**: heavy when the LAST request's live context (input + cache_read + cache_creation tokens — the real spend the next turn pays, not a transcript-size proxy) is at/above this many tokens. Drives `relay send`'s heaviness gate, `relay list`/board's heavy footnote/CTX columns, and `relay send --rotate` advice. A lead's own line is `lead_nudge_tokens` (below) — never this key |
| `lead_nudge_tokens` | 300000 | A **lead's** own heaviness/handoff-nudge line, on a 1M window — a lead's handoff costs more than an executor's rotation (a fresh successor-seed vs. a plain respawn) and its context grows slowly once diffs are reviewed by a fork, so it earns a higher line than `context_nudge_tokens`. Drives the lead's own handoff nudge (Stop hook), `relay status --statusline`'s weight segment, `relay list`'s LEADS CTX/heavy footnote, and the board's lead chip. When the lead's real context window is known (`relay doctor`'s probe, or inferred from a `[1m]`-suffixed model) to be 200k rather than 1M, the EFFECTIVE line is capped to `context_nudge_tokens` instead — a 200k-window lead can never reach 300k live context, so it's still nudged, just on the executor's line |
| `cache_ttl_minutes` | 60 | Claude Code's prompt-cache TTL — how long an executor's last request stays cached free. Drives the warm/cold readout in `relay list`'s TOKENS column, the board, and `relay send`'s advisory line — see [Cache state](#executor-context-window-200k-vs-1m) |
| `executor_default_context` | "1m" | Context window an executor launches with when nothing else decides it (no packet `CONTEXT:` line, no `[1m]` on `--model`, referenced reading under the heuristic). `"1m"` or `"200k"`. Shipped `1m`: the window is a **ceiling, not consumption** — you pay for tokens used, so a bounded packet costs the same either way, and 1M stops executors compacting early on real work. A packet can still pin `CONTEXT: 200k`; haiku (no 1M window) always runs 200K |
| `auto_close` | true | Park finished executors automatically — see [Auto-close](#auto-close-finished-executors-park-themselves) |
| `auto_close_idle_minutes` | 60 | Idle-after-report threshold for the auto-close timer path; 0 = timer off (the landed path still applies) |
| `executor_fallback_model` | unset | A concrete model id (or list, tried in order) that overloaded executors fall back to — e.g. `"claude-opus-4-8"`. A bare string is accepted and wrapped into a one-element list — Claude Code's `--settings` file requires `fallbackModel` to be a list, not a string, so passing a single id straight through failed every spawn's settings validation. A fallback equal to the executor's own launch model (compared with any `[1m]` suffix stripped) is dropped, with a spawn-time warning, since falling back to the model already running would just spin in place. Delivered via each executor's per-launch `--settings` file (`fallbackModel` — the `--fallback-model` flag is print-mode-only; the settings key works interactively), so if capacity fallback happens it goes where YOU chose, with the CLI's visible notice. Unset = no configured fallback (the documented default) |
| `executor_escalation` | true | Arm every spawned executor with the second-layer one-shot push (see [Auto-wake and notifications](#auto-wake-and-notifications)) |
| `autonomous_mode` | false | Posture a newly-armed lead holds. false = wait for you on every approval beat (safe default). true = new leads start in autonomous mode. `/relay:auto on\|off` flips it mid-session either way (see [Autonomous mode](#autonomous-mode)) |
| `stall_threshold_seconds` | 2700 | How long an executor can be `busy` with no transcript activity for before `stalled` — a long `busy` packet whose transcript is still being written stays `busy` (e.g. `busy 3h20m`) instead of misreading as stalled; kept independent of `poll_seconds` so the two don't flip at the same instant |
| `usage_limit_pattern` | built-in | Regex (case-insensitive, matched against the START of an executor's last assistant message) that marks it `paused (limit)` in `relay list`/`check` instead of an ordinary `stalled`. The built-in wording is a reasonable guess, not confirmed against a real Claude Code usage-limit message — override this only if the CLI's actual wording differs |
| `signoff_paths` | `[]` | Per-machine ADDITIONS to the auto-commit gate's condition 4 sign-off list (repo-relative path substrings, same blunt-substring semantics as the built-ins) — see [Autonomous mode](#autonomous-mode). Merged with the built-ins, never replacing them; shown by `relay doctor` |

`poll_seconds` must stay under the `Stop` hook's `timeout` in `hooks/hooks.json` (currently 1900s) — the harness kills the hook's background poller at that timeout regardless of `poll_seconds`, so raising one without the other silently breaks auto-wake (see [async-rewake-findings.md](docs/async-rewake-findings.md#addendum-silent-auto-wake-death-2026-07-10)).

Per-spawn override for `executor_skip_permissions`: pass `--skip-perms` or `--no-skip-perms` at `relay spawn` time.

### The executor agent

Every executor is launched as a Claude Code **agent** (`agents/executor.md`, passed inline as
`--agents … --agent relay-executor`, so it works whether relay is installed or loaded via
`--plugin-dir`). The standing GATES and REPORT FORMAT live in its system prompt — never compacted
away, re-applied on every resume/restart — so the packet only carries the task plus the per-packet
report path, self-diff command and closing line. Two things become *enforced* rather than asked:
the `Agent` tool is removed (an executor can never spawn sub-agents), and `git commit` / `git push`
are denied at the CLI (`--disallowedTools`, which holds even under `--dangerously-skip-permissions`
— verified live, `tests/test_e2e_agent.py`). Sessions spawned before the agent existed keep
getting the full GATES footer in their packets. The denies are prefix rules (`git commit…`,
`git push…`) — a guard against the ordinary mistake, not a sandbox; `git -C <dir> commit` would
slip past, and the staged-diff review is still where anything lands or doesn't. There is no lead agent on purpose: leads arm
mid-session with `/relay:mode`, which `--agent` (launch-time only) can't do.

### Executor context window (200K vs 1M)

Bare model aliases typically open a 200K window, and the `[1m]` suffix opens 1M (`sonnet[1m]`,
`opus[1m]`; Haiku 4.5 has no 1M flavour) — but the account decides, not the alias: some accounts
already resolve bare `sonnet`/`opus` to 1M, so relay learns the real window from `relay doctor`
rather than assuming it (see "How relay proves it" below). The window is fixed at spawn
(`--resume` keeps the conversation's model) — the lead decides it, or relay does mechanically on
its behalf; the executor never picks its own. Precedence: an explicit `[1m]` on `--model` → the
packet's `CONTEXT: 1m`/`200k` line → relay's heuristic (≥ ~600KB of referenced reading ≈ 150K
tokens → `[1m]`) → the `executor_default_context` config (**shipped `1m`** — a ceiling, not
consumption, so a bounded packet costs the same either way). A packet can pin `CONTEXT: 200k` to
opt one executor down; a heavy session is widened via `relay retire` + respawn. `haiku[1m]` is
refused — a packet/heuristic asking for 1M on haiku degrades to 200K with a note.

**How relay proves it.** Everything above decides which window an executor is *launched* with — a
`context: 1m/200k` field relay writes and then trusts, nothing before this stamped it against
reality. Three things now do:
- `relay doctor`'s "model aliases + context window" check reads the actual `contextWindow` off a
  live probe for `haiku`, `sonnet`, `sonnet[1m]`, `opus`, `opus[1m]` (skipping the opus pair below
  `executor_model_ceiling`). It PASSes iff every `[1m]` probe reports 1_000_000, `haiku` reports
  200_000, and no bare alias reports more than its `[1m]` form — it never requires bare
  `sonnet`/`opus` to be 200_000, since some accounts already resolve them to 1M. relay **learns**
  each model's real window from the probe rather than asserting one, caching it in
  `~/.relay-tasks/tier_windows.json` (adding an ℹ when a bare tier already equals its `[1m]` form).
- `relay list`'s CTX column renders `<live>/<window>` from that cache (falling back to the
  launch-time stamp until doctor has probed it), and appends `✓` once a session's own traffic makes
  a 1M window self-evident — a request whose input+cache tokens exceed 200K could not have run on
  one — or `!` if traffic contradicts the stamped window, with a footnote pointing at `relay doctor`.
- `tests/test_e2e_context.py` pins the live numbers against the real CLI, the same way
  `test_e2e_agent.py` pins the executor agent.

**Cache state.** Separate from window size, Claude Code caches an executor's last request's prefix
for `cache_ttl_minutes` (default 60) — inside that window the next send is nearly free; past it, the
whole prefix re-writes. `relay list`'s TOKENS column, the board, and `relay send`'s advisory line
show it as `warm` or `cold~<N>` (the estimated rewrite). It's a read, not a gate: a heavy AND cold
session is the strongest case for `relay send --rotate` over sending straight in.

### Executor effort

Effort is the second half of the model dial: which model (the rubric in `/relay:spawn`) and how
hard it thinks. Executors run at `executor_default_effort` (`high`, the CLI default) regardless of
your personal `effortLevel` — declaring `EFFORT: low` … `EFFORT: max` in the packet, or passing
`--effort` at spawn (flag wins), only raises or lowers a *single* executor above that policy. It is
always explicit: a new executor's `effort` is never unset, and a legacy session recorded before
this policy existed gets the config default stamped in on its next resume/restart, printed once.
Like the model, it's fixed per process — `relay resume|restart --effort` changes it, `send --rotate`
carries it to the successor, and a follow-up packet declaring a different `EFFORT:` gets an ℹ note
instead of a silent ignore. Pairing guidance: effort is a quality lever on top of the right model,
not a cost lever. Raise a single executor with `EFFORT:` / `--effort`: `xhigh` for an opus executor
whose whole territory is unknown-root-cause work, `max` only when correctness beats cost. Never
lower it to save money — thinking is a minority of an executor's spend; the tier is the lever.
Shown in `relay list`'s LAUNCH column as an always-present fourth segment (`none/1m/A/high`).

**Round-count nudge.** `relay send` prints an ℹ note once the outgoing packet is the 3rd (or later)
sent to that session — `this is packet 3 into a <tier> session — if the last two were fixes for the
same work and it still isn't done, stop sending fixes: relay send <sid> <packet> --upgrade moves it
one tier up with a seeded successor` — and posts the same sentence as a desktop banner (same
`notify_on_wake` / `RELAY_NO_NOTIFY` switches as the wake), because the ℹ line lives in the lead's
collapsed tool output and the human otherwise never sees it. It doesn't try to detect whether the
last two rounds were actually the same work — that's the lead's call. Advisory only; never blocks
the send. `--upgrade` is `--rotate` one tier up: retire, seeded successor, this packet first; the
spawn ceiling still applies.

### Executor MCP servers

(No config key — on purpose.) Executors launch with **zero** MCP servers (`--strict-mcp-config`) — no connector/plugin/user/project MCPs, so their tool rosters and instruction blocks never enter the executor's context (a real per-turn token saving, and one less side-effect surface). The packet itself declares what it needs — a line `MCP: linear` (comma-separate several; strict allowlist) or `MCP: inherit` — and `relay spawn` launches accordingly (precedence: `--mcp` flag > packet line > none). On `relay send`, a packet declaring a server the executor lacks makes relay relaunch the executor's **same conversation** (`--resume`) with the widened set before delivering — MCP servers load at process start and `--resume` doesn't restore `--mcp-config` (verified live: `tests/test_e2e_mcp.py`). `relay resume|restart --mcp SPEC` changes the set by hand. The resolved set is recorded on the session; an allowlist naming a server no config file defines refuses rather than launching an executor missing its tool.

**Environment variable overrides:**
- `RELAY_TERMINAL`: force "iterm" or "terminal" (beats `terminal_app` in config)
- `RELAY_NO_NOTIFY`: suppress all notification banners (useful for tests, CI)

## Troubleshooting

- **`relay list` is scoped to your project's live world.** Ghost and paused leads belonging to
  OTHER projects collapse into one dim line (`N ghost/paused leads in other projects hidden`)
  instead of a wall of rows that also re-named each of them in the `LIVE=ghost` and `↪ migrated
  from` footnotes. The reference project comes from `--lead <sid>`'s marker, or from the cwd when a
  lead there matches it. Live, unreachable, broken and same-project leads are never hidden;
  `--all-leads` shows everything, `relay prune` clears the dead ones for good.
- **First, `relay doctor`.** Proves the installed Claude Code still behaves the way relay's launch
  line assumes — strict MCP, the executor agent, commit-deny under skip-permissions, and each
  model tier's real context window (see [Executor context window](#executor-context-window-200k-vs-1m)
  for what that check proves) — plus plumbing (binary, agent file, hooks, state dir, config, MCP
  servers); `--offline` for plumbing only, `--quick` skips slow probes, run after every update.

- **`/relay:check --all`** tells you the real state (busy/reported/stalled/dead) — trust it over how
  a tab looks. `stalled` means go look at that tab. An executor stuck on a prompt it cannot answer
  (an OS permission dialog, say) keeps its process alive but stops writing its transcript, so it
  reads `stalled` once `stall_threshold_seconds` passes — and `relay retire` / `relay restart`
  accept it without `--force`, since there is nothing left to interrupt.
- **Tab died mid-build?** `relay resume <sid>` reopens the same conversation with context and staged
  work intact; `relay restart <sid>` re-runs the packet fresh.
- **Executor finished but the lead never woke?** First check `relay list` — a **`WAKE=STALE`** on the
  lead means it's bound to a pre-fix wake hook and will keep missing late reports. A **`WAKE=stuck`**
  means a dead watcher's lock is blocking wakes right now; no action needed, it self-heals on the
  lead's next turn (the lock auto-breaks) and any landed report surfaces then. For `STALE`, get it
  onto the fixed hook: `/plugin update relay@claude-relay` (if not already updated) → `/reload-plugins`
  → re-run `/relay:mode` to re-arm (which also re-stamps the version). Otherwise check
  `ls ~/.relay-tasks/lead/` — if empty, arming failed; re-run `/relay:mode`. A landed report surfaces
  on the lead's next idle either way, and `relay report <sid>` pulls it by hand. **After a lead
  handoff**, an inherited executor still owned by the retired lead won't wake you at all — run
  `relay list` and check the footnote for orphaned executors; `relay send`/`relay resume` adopt them
  automatically, or use `relay adopt <sid>` to re-point ownership without sending anything.
- **Tab closed or the session went dead, and you just want to send it more work?** `relay send <sid>
  <packet.md>` revives a closed/dead session by itself — no separate `resume` step needed: it kills
  any lingering process, closes the stale tab, resumes the conversation, and delivers the new packet
  in one shot.
- **After updating relay, verify — don't trust — that running leads picked it up.** A plugin update
  only caches the new version; `/reload-plugins` is *supposed* to re-point a live session's hooks to
  it, but has been observed not to in long-lived sessions. So: update → `/reload-plugins` → take one
  normal turn **without** re-arming → check `relay list`. Current hooks re-stamp the lead's `VER`
  column on every turn, so if it bumped by itself, you're current; if it didn't, **restart that
  session** — don't just re-run `/relay:mode` to "fix" it. A manual re-stamp only masks the check (it
  also blinds `relay list`'s own red **`stale hooks`** footnote — which flags exactly this
  automatically for any recently-active lead, no manual check needed unless you've re-stamped over
  it). If you've already re-stamped and need a check that survives it: on the next idle turn with a
  busy executor, look at that lead's `poll.lock` — JSON `{pid, pid_started, ts}` means current hooks; a bare integer
  means stale.
- **If your 🚦 segment disappears after a resume**, relay's own `$CLAUDE_CODE_SESSION_ID` most likely
  changed underneath the same tab (observed live: a background-job resume reported a different id
  than the one the lead armed under). A resume that comes back under a NEW id can't be revived by
  the ordinary same-id path, so relay's SessionStart/Stop hooks fall back to looking for a lead
  marker that claims the SAME tab AND the same project directory, and migrate it forward
  automatically — gate, wake, auto posture and the statusline segment all move with it, and
  `relay list` marks the row `↪ migrated from <old id>` so the jump stays visible. This should be
  silent and automatic; if the segment is
  *still* gone after your next turn, migrate by hand: `/relay:mode` re-arms this session as a fresh
  lead, `relay adopt <sid> --force` for each executor the old lead owned, then `relay close --self
  <old-session-id>` to retire the orphaned marker.
- **No wakes, no gate, and the tab looks fine?** Your marker may have been migrated or tombstoned
  out from under you (a hijack, or a silent tombstone during plugin-reload churn) without you
  noticing — this used to be invisible until you happened to check. Two signals now catch it: the
  statusline itself turns into a red **`lead ENDED — /relay:mode`** alarm (instead of quietly going
  blank), and `relay list`'s LEADS table renders that row's LIVE cell as **`ended?`** in red with a
  footnote naming the session id — a live tab whose own marker says it already ended is exactly the
  hijack/silent-tombstone shape, not an ordinary resumable pause (which never leaves its tab running
  to notice). Either signal means the same fix: re-run `/relay:mode` in that tab to re-arm.
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
