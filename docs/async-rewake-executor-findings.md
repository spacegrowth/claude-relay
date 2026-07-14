# Does an executor's `asyncRewake` Stop hook survive it going idle? (spike findings)

**Verified empirically 2026-07-14.** Companion to `async-rewake-findings.md` (which proved the
mechanism on a **lead** session). This spike closes the one gap that doc didn't cover: an
**executor**-shaped session — write a report, go idle — behaves the same way. Answers `wake-watch-
design.md` §7 spike #2.

## The question

The wake-watch redesign relocates report-watching into the executor itself: after an executor
writes `NNN-report.md` and goes idle, an `asyncRewake` Stop hook runs in the background, waits a
grace period (~60s), then checks/escalates. The whole design rests on that background process
surviving the executor going idle and staying alive long enough to fire.

## Result: SURVIVES

Ground truth: marker-file timestamps (epoch, written by the hook itself) cross-checked against the
session's own transcript (independent clock, written by Claude Code). Both agree.

### Marker file (`marker.log`)

```
hook_fired_at=1784014697
hook_fired_at=1784014709
hook_survived_at=1784014767
hook_fired_at=1784014770
hook_survived_at=1784014779
hook_fired_at=1784014781
```

First cycle: `hook_survived_at` (1784014767) − `hook_fired_at` (1784014697) = **70s**, exactly the
sleep duration in the hook script — the background process ran uninterrupted for the full 70s, past
the harness's ~60s default timeout (the settings file set `"timeout": 120` explicitly; see
Constraints below for why that matters).

### Transcript cross-check (ground truth, not the harness's verdict)

The executor session's own `.jsonl` transcript (independent of the marker file) lines up
timestamp-for-timestamp:

| Epoch (UTC) | Marker event | Transcript event |
|---|---|---|
| 07:38:17 | `hook_fired_at=1784014697` | Assistant's first final text reply (session goes idle) — Stop fires |
| 07:39:27 | `hook_survived_at=1784014767` | `<task-notification>` "spike-exec-hook-wake" appears as a fresh **user** turn, followed by a new assistant reply acknowledging it |

The wake landed in the transcript at the exact second the marker file recorded the hook's exit-2,
70 seconds after it fired — two independently-clocked signals agreeing is what makes this a real
survival, not a measurement artifact (the `async-rewake-findings.md` "hard lesson": prior spikes
gave a false green and a false inconclusive from measurement bugs, not the mechanism itself).

The cycle then repeated twice more (`fired@709→survived@779`, `fired@781→...`) because this spike's
rig hook has no once-per-event gate (unlike production's `stop_lead_watch.py`) — every idle re-arms
it. That repetition is itself corroborating evidence: each `asyncRewake` wake produced a genuine
fresh turn, not a one-off fluke.

## Verdict

**SURVIVES.** `hook_fired_at` present, `hook_survived_at` present ~70s later matching the sleep
duration exactly, and the transcript independently confirms the session took a fresh turn at the
same second. The executor-side design's foundational assumption (§7 spike #2 in
`wake-watch-design.md`) holds — build can proceed on this front.

## Constraint confirmed (carries over from the lead-side finding)

Same as `async-rewake-findings.md`'s addendum: the hook's `"timeout"` in the `Stop` hook entry must
exceed the intended sleep/grace window, or the harness's own default (~60s) kills the background
process before it can do its job. This rig set `"timeout": 120` for a 70s sleep specifically to
confirm that setting it matters — do not omit it in the real executor-side hook.

## Reproduction

Rig built fresh in the session scratchpad (not checked into this repo — throwaway):
- `settings.json` — a `--settings` file registering a `Stop` hook, `"asyncRewake": true,
  "timeout": 120`.
- `hook.sh` — on fire, appends `hook_fired_at=$(date +%s)` to `marker.log`, sleeps 70s, appends
  `hook_survived_at=$(date +%s)`, prints a line, `exit 2`.
- `launch.sh` — opens a new iTerm tab via `osascript`, runs `claude --dangerously-skip-permissions
  --model haiku --session-id <uuid> --settings settings.json '<prompt>'` where `<prompt>` is "write
  the single word DONE to report.md, then stop" (an executor-shaped task: do the work, report, go
  idle).
- Baselined `marker.log` as absent/empty before launch; observed `pid.txt`, `report.md`,
  `marker.log` after; cross-checked against the transcript at
  `~/.claude/projects/<sanitized-scratchpad-path>/<session-uuid>.jsonl`.
- Cleanup: killed the executor's `claude` pid and the still-sleeping detached `hook.sh` pid, closed
  the iTerm tab, removed the scratchpad path's trust entry from `~/.claude.json`.

## Secondary question (not run)

`wake-watch-design.md` §7 spike #1 (does `UserPromptSubmit` fire on an `asyncRewake` wake, for the
lead's busy/idle stamping) was **not** checked in this rig — the transcript shows the wake landing
as a `user`-role `<task-notification>` turn (see table above), which is suggestive but not a direct
`UserPromptSubmit` hook observation. Per the packet's instruction, this was left unrun rather than
let it block the primary (gating) verdict above.
