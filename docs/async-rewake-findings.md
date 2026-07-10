# Waking an idle lead session with `asyncRewake` (spike findings)

**Verified empirically 2026-07-05** on this machine's Claude Code version. Reproduction rig:
`spike.py` (kept in the session scratch dir; rebuildable from this doc).

## Status — BUILT & verified (2026-07-06)

Both applications are implemented and wired into the plugin:
- `hooks/stop_lead_watch.py` — the Stop + `asyncRewake` hook (App 1 background report-watch + App 2
  commit surfacing), gated once-per-event, **announce-and-wait** (never auto-acts).
- `hooks/hooks.json` — Stop hook registered with `asyncRewake` + `rewakeMessage`.
- `lib/lead_guard.py` — report surfacing, git-HEAD/commit tracking, single-poller lock, config.
- `bin/relay lead-start` — records the cwd git HEAD baseline; `skills/mode/SKILL.md` documents the
  announce-and-wait rule.

Verification:
- **65 unit tests** incl. `test_wakes_on_late_arriving_report` (executor reports *after* the lead is
  idle → background poller detects it and exits 2) and the lock/once-per-event gating.
- **Live E2E** (real `claude --plugin-dir relay`, transcript-confirmed): the plugin's Stop hook
  fired on idle, detected a real executor's report, **woke the idle lead**, which announced it in
  announce-and-wait form (*"An executor has finished work: ✅ executor 'daily-sectors' reported…"*).
  This exercised the fast path (report already present at idle). The late-report background-poll
  path is proven by composition: plugin Stop hook fires (this E2E) → poll loop (unit test) → async
  exit-2 wakes an idle session (the original spike).
- Harness lesson: the first E2E gave a **false green**, the second a **false INCONCLUSIVE** — both
  were measurement bugs (stale-marker matching; baselining the transcript *after* the wake; watching
  for a `poll.lock` the fast path never creates). The transcript was the ground truth, not the
  harness's verdict.

## The question

relay's lead is a Claude Code session — turn-based, not a daemon. After it delegates a packet, how
does it learn an executor finished (wrote its `NNN-report.md`) **without** the user asking, without
blocking a turn in a poll loop, and without a scheduled `/loop` re-fire? I.e. does Claude Code have
a native way to *wake an idle session* when something changes on disk?

## Result: YES — an `asyncRewake` Stop hook wakes an idle session

Confirmed with two independent, corroborating signals in a controlled test (a real interactive
`claude` session, isolated temp dir, disk-observed):

- The session launched, replied, and **went idle** (returned to the prompt, process alive).
- A **Stop hook** with `asyncRewake: true` fired, ran a watcher **in the background** (the session
  went idle immediately — it did *not* block), the watcher slept 10s, then **exited 2**.
- Within a few seconds of that exit-2: the session's transcript **grew** (it took a fresh turn on
  its own) **and** the model executed the action in the wake message. Both signals agreed, so this
  isn't a model-compliance fluke — the idle session genuinely woke.

## The mechanism (exact)

- **Event:** `Stop` (fires each time the session finishes a turn / goes idle). `asyncRewake` is
  also valid on `PostToolUse` (that's how the official `security-guidance` plugin uses it, gated by
  `if` conditions on `Bash(git commit:*)` etc.). `Stop` is the natural trigger for "check something
  every time the lead goes idle."
- **Config fields** on the hook entry:
  - `"asyncRewake": true` — run the command in the background; wake the session on exit code 2.
  - `"rewakeMessage": "..."` — prepended to the woken context (this is where you tell the lead what
    to do, e.g. "executor X reported — run `/relay:check X` and review").
  - `"rewakeSummary": "..."` — a short label for the wake.
- **Exit-code contract:** exit `0` → **silent**, session stays idle. Exit `2` → **wake**: the
  session takes a new turn and receives the hook's stdout/stderr + `rewakeMessage` as a system
  reminder.
- **Loading:** pass the hook config to the spawned session via **`claude --settings <file.json>`**
  (a real CLI flag). Confirmed to load hooks with no project-settings-approval friction. A
  project-level `.claude/settings.json` was **not** reliably loaded in the spike — use `--settings`.

## Constraints and gotchas (all learned the hard way in the spike)

1. **The session process must still be alive** (idle at the prompt). Closing the tab kills it;
   nothing wakes a dead session. Fine for relay — the lead's tab stays open.
2. **A hook only fires from a lifecycle event, not "on its own."** A config'd hook cannot fire just
   because a file appeared. The `Stop` event is the arming trigger — it fires every time the lead
   goes idle, which is exactly when we want to run the background check.
3. **GATE IT TO FIRE (wake) EXACTLY ONCE per real event.** A Stop hook that exits 2 on every idle
   is an infinite wake loop / constant nag — this is the *same noise failure we already hit and
   removed* (see the deleted `stop_lead_nudge.py`). The hook must exit `0` silently unless there is
   genuinely new state, and must not re-wake for state it has already surfaced. Use a per-session
   seen-marker/flag, and combine with the payload's `stop_hook_active` guard as a loop backstop.
4. **`--settings` is the injection mechanism, not project settings.** `spawn` the lead (or arm the
   current session) with `--settings <generated-hook-file>`.
5. **`timeout(1)` is not on macOS** (it's `gtimeout`) — don't use it in hook scripts here.

## Application 1 — executor completion (the original problem)

Arm the lead with a `Stop` hook, `asyncRewake: true`, whose background watcher checks the in-flight
executors' report paths (`~/.relay-tasks/<sid>/packets/NNN-report.md`):

- No **new** report since last surfaced → **exit 0** (silent; lead stays idle).
- A new report → **exit 2** with `rewakeMessage`: *"executor `<sid>` reported — run `/relay:check
  <sid>` and review the staged diff."*

Result: the idle lead **wakes itself** on completion, reviews, commits — no `/loop`, no blocking
`relay wait`, no manual "are they done?". The report file relay already writes *is* the trigger;
this just makes the lead notice it. The per-report seen-marker (constraint 3) is the whole trick to
getting this quiet.

## Application 2 — routing enforcement (does the lead actually delegate?)

This is the more interesting one, and it closes the two gaps the current PreToolUse gate can't:

- The `PreToolUse` gate is **preventive but narrow** — it blocks large `Edit`/`Write`/`MultiEdit`
  *before* they land, but structurally **cannot see `Bash`** (`git merge`, `git commit`, `sed -i`,
  heredocs) — which is exactly the shape of the incident that started all this.
- An `asyncRewake` **Stop-hook review is detective but broad** — it runs *after* the turn and can
  inspect **outcomes** (`git diff`/`git status`/repo state), so it catches Bash-driven changes and
  death-by-many-small-edits that the tool-call matcher misses. It wakes the lead only when it finds
  undelegated substantial work: *"you changed N files inline this turn (incl. via Bash) — route
  this or justify with `/relay:route retain`."*

This is precisely how `security-guidance` works (async git-diff review on Stop, wakes only on
findings), and it fixes both weaknesses of the removed `stop_lead_nudge.py`: it's **silent unless
there's a real finding** (exit 0 on clean turns, per constraint 3), and it **covers Bash**.

**The hard part is unchanged and honest:** the review must distinguish *undelegated substantial
work* from *the lead legitimately committing an executor's staged diff* (committing IS lead work).
A naive "did git state change?" check would false-positive on every legit lead commit. Options:
(a) a heuristic that ignores changes the lead is *committing* vs *authoring* (hard to tell apart
from git alone); (b) an LLM-based reviewer like security-guidance uses — accurate but spends tokens
on each idle; (c) tie it to the existing routing ledger (`retained`/`blocked` events) so the review
only flags work with no corresponding route declaration. Option (c) is the cheapest and reuses what
relay already records.

**Preventive + detective are complementary, not either/or:** keep `PreToolUse` for pre-damage
blocking on the Edit/Write vector; add the `asyncRewake` Stop review to catch the Bash vector and
nudge (via self-wake) after the fact. Neither alone is complete; together they cover both vectors.

## Reproduction

`spike.py` builds an isolated rig: a temp dir, a `--settings` hook file with a `Stop`+`asyncRewake`
watcher that sleeps then exits 2, launches a real `claude --dangerously-skip-permissions --model
haiku --settings <file>` in an iTerm tab, and detects the wake via temp-dir marker files
(`run_started`, `watcher.log`, `WOKE`) plus transcript growth. Cleans up the tab and the trust
entry afterward.

## Addendum: silent auto-wake death (2026-07-10)

**Incident.** A live lead session's poller armed at 08:30 (`poll.lock`, pid 32943). The report it
was waiting on landed at 08:46 — well inside the poller's own 1800s deadline. But the pid was
already dead, the lock was never released, and no `surfaced_reports.json` was written. That
signature — `finally:` blocks never running — points to an *external* hard kill of the background
process, not a normal exit or an internal timeout inside the script. No wake, no banner.

**Diagnosis (prime suspect, not confirmed fact).** The `Stop` hook entry set `"asyncRewake": true`
but no `"timeout"`. Absent an explicit timeout, the harness applies its own default (~60s) to the
hook process — which would kill the background poller long before a report landing at the 16-minute
mark could ever surface. This fits all observed evidence (dead pid, no cleanup, no wake), but it is
a diagnosis from symptoms, not something confirmed against a harness log — the original spike (see
Reproduction above) only ever exercised a 10s background sleep, so the 30-minute path was never
actually run end-to-end before this incident.

**Fix.** `hooks/hooks.json` — the `Stop` hook entry now sets `"timeout": 1900`, i.e. `poll_seconds`
(1800, see [Config](../README.md#config)) plus a 100s margin, so the hook process is allowed to
outlive the poller's own deadline.

**Standing constraint.** The `hooks.json` `Stop`-hook `timeout` must always exceed
`lead/config.json`'s `poll_seconds`. Anyone raising `poll_seconds` past 1900 must raise the hook
timeout to match, or the poller will be killed out from under itself again with the same silent
symptom (dead pid, stale lock, no `surfaced_reports.json`, no wake).

**Acceptance test.** This fix cannot be verified by unit test — there is no automated surface for
harness-enforced hook timeouts. The real test is live: a report landing more than 60s into a
genuinely idle lead session should now still produce a wake. Its absence recurring (the
dead-pid/stale-lock signature from this incident) over the next few live runs is the actual proof.

## Addendum: SessionEnd hook safety — clearing markers only on real ends (2026-07-10)

**Incident.** During a `/plugin uninstall → remove/add → install → /reload-plugins` sequence, an
armed lead's entire `lead/<sid>/` subtree vanished — marker, surfaced_reports, all of it — without
the session ever ending. The session itself stayed alive with the same conversation running. The
only code that deletes that subtree is `sessionend_lead_cleanup.py`'s `clear_lead()`. Attribution to
reload-churn (plugin reload firing spurious SessionEnd events) is the best-fit hypothesis but
unproven; the hook had no observability. Result: the lead was silently unarmed (gate off, wakes off)
until a human noticed a missing wake.

**Fix.** `hooks/sessionend_lead_cleanup.py` now:
1. **Always logs** every SessionEnd to the ledger with its reason, session_id, and was_lead flag —
   permanent observability for future incident attribution.
2. **Clears lead state ONLY on documented real-end reasons:** `{"clear", "logout",
   "prompt_input_exit", "exit"}` (Claude Code's documented SessionEnd reasons). Any
   unknown/missing reason → **preserves the marker** (fail-safe in favor of staying armed).
3. **Documents the incident and policy** in a comment explaining why this defensive posture exists.

**Stale markers.** A marker that outlives its session (e.g. from a missed SessionEnd) shows a stale
`LAST ACTIVE` age in `relay list` and is cleaned up by `/relay:stop` (step-down with reason
"clear") or `relay prune --days N --dry-run` (discovery tool). Stale markers do not block new
packet sends or leads taking over — they're just orphaned state.
