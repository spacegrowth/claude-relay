# Bug-hunt findings — area `hooks`

Scope: the six Claude Code hooks in `hooks/` plus the `hooks/hooks.json` manifest, driven as real
subprocesses with crafted stdin payloads and a tmp `HOME` (and, for the branches a subprocess hides
from `coverage`, the same scripts loaded in-process under the same conditions).

Branch `wt/bh-hooks`, from `main` @ b659a73. Measured 2026-09-05.

Test files: `tests/conftest_hooks.py` (the shared harness), `tests/test_hooks_pretool.py`,
`tests/test_hooks_stop.py`, `tests/test_hooks_stop_poller.py`, `tests/test_hooks_session.py`,
`tests/test_hooks_escalation.py`, `tests/test_hooks_manifest.py`.

| | |
|---|---|
| Tests added | 652 — 648 passing + 4 xfail-strict, across 6 files + a shared harness |
| BUG findings | 3 |
| SUSPECT-TEST findings | 0 |
| AMBIGUOUS findings | 2 |
| Most important | **BUG-hooks-1** — a corrupt lead marker turns the routing gate **on**, blocking every edit the lead makes, in direct contradiction of the hook's fail-open HARD RULE. |

Coverage, `hooks/*` (before → after):

| File | Before | After |
|---|---|---|
| `hooks/pretool_route_guard.py` | 0% | **92%** |
| `hooks/pretool_bash_gate.py` | 0% | **91%** |
| `hooks/sessionend_lead_cleanup.py` | 0% | **96%** |
| `hooks/sessionstart_lead_rearm.py` | 41% | **95%** |
| `hooks/stop_lead_watch.py` | 56% | **91%** |
| `hooks/executor_escalation.py` | 67% | **91%** |
| **total** | **44%** | **92%** |

Both numbers are from the same command over the whole unit suite
(`coverage run --source=bin,lib,hooks -m pytest tests/ --ignore-glob='tests/test_e2e_*'`), before
and after. Note the "before" for `stop_lead_watch.py` measures 56%, not the 31% the packet quoted —
the packet's figure appears to predate some existing coverage. Full suite: 1215 passed before,
1863 passed + 4 xfailed after.

The lines still uncovered in each file are the guards around `import lead_guard` (unreachable while
`lib/` is on the path), the `if __name__ == "__main__":` dispatch, and a handful of best-effort
`except: pass` blocks whose only failure mode is "a ledger line was not written".

## Confidence: mutation-checked

Per the shared rules' "a test that passes trivially ... is a smell", five assertions were checked by
briefly mutating the source and confirming the test goes red (every mutation reverted; `git status`
verified clean afterwards):

| Mutation | Result |
|---|---|
| `exceeds_gate`: `lines >= threshold` → `lines > threshold` | 4 failures (boundary + configured-threshold) |
| `relay_announce_delivered`: return True with no claim (re-introduces the #23 incident) | `test_a_foreign_hooks_continuation_promotes_nothing` fails |
| `is_gate_exempt`: drop the state-root rule | 6 failures across the exemption tests |
| `sessionend`: treat every reason as a hard clear (re-introduces the 2026-07-10 incident) | 6 failures across the reason-policy tests |
| `new_reports_for`: drop `superseded` from the skip list | **NO failure** — see below |

That last one is the useful negative result, and it is recorded in the test itself rather than
quietly dropped: closed/superseded exclusion is enforced **twice**, independently
(`executor_reports:781` drops the session before `new_reports_for` ever sees it, and
`new_reports_for:1091-1098` drops it again), so a behaviour test cannot isolate either filter.
`test_closed_and_superseded_executors_never_nag` now says so in its docstring, and a second test
(`test_a_closed_executor_is_dropped_by_new_reports_for_too`) asserts against
`lg.new_reports_for` directly, with the positive precondition (`load_surfaced` empty, so the report
genuinely would be fresh) asserted alongside the negative result.

---

## BUG-hooks-1 — a corrupt lead marker turns the routing gate ON instead of failing open

**Severity:** wrong-result — an armed lead is blocked from every `Edit`/`Write`/`MultiEdit` with a
message that misattributes the cause, and the only recovery is deleting a file by hand.

**Contract**

`hooks/pretool_route_guard.py:8-9`:

> HARD RULE: any error, missing file, unparseable payload, or unexpected shape → exit 0 (allow).
> A broken hook must never brick normal Claude Code usage.

`lib/lead_guard.py:438-439` (`is_lead`'s own docstring):

> The sole 'is this a lead session' test. Marker absent (**or any error**) → not lead → the hooks
> fast-exit-allow, which is the entire zero-impact path for non-lead/executor sessions.

**Actual**

`lib/lead_guard.py:447-451`:

```python
try:
    if not marker_path(state_root, session_id).exists():
        return False
    return not is_tombstoned(read_marker(state_root, session_id))
except Exception:
    return False
```

`read_marker` (`lib/lead_guard.py:499-504`) swallows the JSON error and returns `{}`;
`is_tombstoned({})` is `False`; so `is_lead` returns **True**. The `try/except` never fires, because
nothing raises — the error was already absorbed one level down.

An unparseable marker therefore reads as a live, armed lead. Every hook that gates on `is_lead`
proceeds: `pretool_route_guard.py` denies the edit, `pretool_bash_gate.py` starts ledgering, and
`stop_lead_watch.py` runs its full wake path against a marker it cannot read.

This is reachable in practice — the marker is rewritten in place on every lead turn
(`touch_lead` → `marker_path(...).write_text(...)`, `lib/lead_guard.py:604`), with no atomic
rename, so a crash or a full disk mid-write leaves exactly this state.

Observed on the route guard: an armed lead whose marker is truncated to
`{"session_id": "lead-1", "proj` gets

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", ...}}
```

with the reason blaming the *size* of the edit.

**Repro test:** `tests/test_hooks_pretool.py::TestRouteGuardCorruptMarker::test_corrupt_marker_fails_open`
(xfail strict). Its sibling `test_corrupt_marker_current_behaviour_is_at_least_exit_zero` pins the
half of the HARD RULE that *does* hold today (exit 0, no crash), so a fix cannot regress it.

**Proposed fix** (do not apply — reported only). Make `is_lead` distinguish "no marker" from
"unreadable marker" instead of collapsing both into a truthy read:

```diff
--- a/lib/lead_guard.py
+++ b/lib/lead_guard.py
@@ def is_lead(state_root, session_id):
     try:
         if not marker_path(state_root, session_id).exists():
             return False
-        return not is_tombstoned(read_marker(state_root, session_id))
+        marker = read_marker(state_root, session_id)
+        if not marker:
+            return False   # present but unreadable/empty → not armed (the fail-open direction)
+        return not is_tombstoned(marker)
     except Exception:
         return False
```

Note the direction this fix chooses, because it is not free: a lead whose marker is corrupted goes
**silently unarmed** rather than **loudly stuck**, and `docs/lead-arming-durability.md` is entirely
about how bad silent unarming is. The mitigation belongs with it — `relay list` already renders
lead state, so an unreadable marker should surface there as a distinct broken/`?` row rather than
just vanishing. Worth a second opinion before implementing; the current behaviour is not defensible
either way, since it is the one outcome the HARD RULE names explicitly.

---

## BUG-hooks-2 — every hook command is unquoted, so a plugin root containing a space disables all of them

**Severity:** wrong-result, latent (depends on install path).

**Contract**

`hooks/hooks.json`'s own `description` states the behaviour unconditionally: the PreToolUse gate
"blocks large inline Edit/Write/MultiEdit", the Stop hook "wakes the idle lead". `README.md:313-317`
likewise: "Once armed, a hook **blocks large inline edits**".

The internal precedent for the correct form is in this same tree —
`hooks/stop_lead_watch.py:89-90` quotes the interpolated path when it builds a shell command:

```python
args += ["-group", f"relay-{lead_sid}",
         "-execute", f"'{RELAY_BIN}' focus {lead_sid}"]
```

**Actual**

`hooks/hooks.json:8` (and the other four entries):

```json
{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/pretool_route_guard.py"}
```

and `lib/lead_guard.py:2233`:

```python
"command": f"{hook_path} {exec_name}",
```

Neither quotes the interpolated path. Installed under a path containing a space — e.g.
`claude --plugin-dir "~/My Plugins/claude-relay"` — the command word-splits into an argv whose
first element does not exist, and the hook never launches.

The failure is silent in the worst way: a hook that fails to *launch* is indistinguishable from a
hook that fast-exited by design, which is the entire zero-impact path. There is no error, no
ledger line, and no `relay list` column that would show it. The lead simply has no gate and no
wake, while `relay list` still reports the marker as armed.

**Repro tests:** `tests/test_hooks_manifest.py::TestManifestCommandQuoting::test_manifest_commands_survive_a_plugin_root_with_a_space`
and `::test_the_escalation_settings_command_survives_a_plugin_root_with_a_space` (both xfail
strict). `::test_the_command_is_shell_safe_for_the_normal_install_path` pins that the bug is
currently latent, not live, for the paths relay actually ships under.

**Proposed fix** (do not apply). Quote the placeholder in all five manifest entries:

```diff
-{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/pretool_route_guard.py"}
+{"type": "command", "command": "'${CLAUDE_PLUGIN_ROOT}'/hooks/pretool_route_guard.py"}
```

and in `lib/lead_guard.py:2233`:

```diff
-        "command": f"{hook_path} {exec_name}",
+        "command": "%s %s" % (shlex.quote(hook_path), shlex.quote(exec_name)),
```

---

## BUG-hooks-3 — the SessionEnd hook writes to `~/.relay-tasks` for every session on the machine

**Severity:** wrong-result (unwanted side effect on unrelated projects) — plus unbounded growth of
a file the user never opted into.

**Contract**

`hooks/hooks.json` `description`:

> Silent on non-lead and executor sessions (**each hook fast-exits when the lead marker is absent**).

`README.md:320-322`:

> it only acts in `/relay:mode` sessions — **every other session on the machine is untouched** (the
> hook fast-exits, fail-open).

`hooks/pretool_route_guard.py:4-5` says it a third time:

> Everywhere else (non-lead sessions, executor sessions, **every other project on the machine**) it
> fast-exits and allows.

**Actual**

`hooks/sessionend_lead_cleanup.py:45-48` logs **before** any marker check:

```python
# Always log to ledger for observability
if sid:
    was_lead = lg.is_lead(STATE_ROOT, sid)
    lg.append_ledger(STATE_ROOT, "session_end", session_id=sid, reason=reason, was_lead=was_lead)
```

and `append_ledger` (`lib/lead_guard.py:744-757`, `root.mkdir` at :750) creates the root on the way:

```python
root = Path(state_root)
root.mkdir(parents=True, exist_ok=True)
```

So ending *any* Claude Code session, in *any* project, on a machine where relay is merely
installed, creates `~/.relay-tasks/` and appends a `session_end` line to `sessions.jsonl` — forever,
with no cap and no pruning. Measured across all six hooks with a stranger session id, this is the
only one that does it:

```
pretool_route_guard.py           creates_root=False
pretool_bash_gate.py             creates_root=False
stop_lead_watch.py               creates_root=False
sessionstart_lead_rearm.py       creates_root=False
sessionend_lead_cleanup.py       creates_root=True   ledger=['session_end']
executor_escalation.py           creates_root=False
```

**Ambiguity, stated plainly:** the write is deliberate. The same file's docstring (L12-13) says
"Every SessionEnd is logged to the ledger with its reason for future incident attribution", added
after the 2026-07-10 unarming incident. This is a genuine conflict between two in-repo contracts,
resolved here in favour of README + the manifest per the bug-hunt oracle order (README/docs rank
above module docstrings). A maintainer may legitimately decide the docstring wins — in which case
the fix is the doc change, not the code change.

**Repro test:** `tests/test_hooks_manifest.py::TestManifestDescriptionIsTrue::test_no_hook_creates_state_for_a_stranger`
(xfail strict on the `sessionend_lead_cleanup.py` parameter only; the other four hooks assert
positively and pass). `::test_the_stranger_state_that_is_created_is_only_the_ledger` bounds the
blast radius: no marker is written and nothing is armed.

**Proposed fix** (do not apply). Keep full attribution on machines that use relay, and honour the
zero-impact promise on machines that do not, by not *creating* the root from this path:

```diff
--- a/hooks/sessionend_lead_cleanup.py
+++ b/hooks/sessionend_lead_cleanup.py
@@
-        # Always log to ledger for observability
-        if sid:
+        # Log for observability — but only where relay already lives. Creating the state root from
+        # a SessionEnd would touch every project on the machine (hooks.json's own description
+        # promises the opposite), and a machine with no ~/.relay-tasks has no incident to attribute.
+        if sid and os.path.isdir(STATE_ROOT):
             was_lead = lg.is_lead(STATE_ROOT, sid)
```

(The alternative is to correct the manifest `description` and `README.md:320-322` instead.)

---

## AMBIGUOUS-hooks-1 — the Bash gate's `heredoc` rule matches arithmetic left-shift

`lib/lead_guard.py:408` classifies an implementation verb with:

```python
{"name": "heredoc", "pattern": r"(?<!<)<<(?!<)-?~?\s*['\"]?\w+"},
```

`echo $((2 << 3))` matches: `<<` followed by `\s*` then `\w+` (`3`). The command is a pure read and
gets ledgered as `would_have_blocked`, rule `heredoc`.

Classified AMBIGUOUS rather than BUG because the taxonomy is explicitly in a tuning phase and this
is precisely the tuning signal it is collecting — `hooks/pretool_bash_gate.py:5-7`: "LOGGING ONLY
(dry-run-first ... Blocking mode ships later, once the allowlist is tuned against real lead-day
logs". A false positive today costs one ledger line and nothing else. It would become a real bug
the day blocking mode ships, so it is worth carrying forward to that work.

No xfail test written: asserting either outcome would be enshrining a guess about intent. The
adjacent behaviour that *is* contractual — `<<<` here-strings must not match, because the pattern's
`(?<!<)`/`(?!<)` guards exist for exactly that — is pinned by
`tests/test_hooks_pretool.py::TestBashGateNeverDenies::test_herestring_is_not_a_heredoc`.

## AMBIGUOUS-hooks-2 — custody rules win on overlap even when the custody word is incidental

`classify_bash_command` (`lib/lead_guard.py:414-436`) checks `CUSTODY_RULES` first and returns
`None` on the first match, so any command containing a custody token free-passes regardless of what
else it does. `git clone https://github.com/pytest-dev/pytest` free-passes, because the `test-suite`
custody pattern matches `pytest` inside the URL — even though `git clone` is a listed implementation
verb.

Classified AMBIGUOUS because the ordering is documented and deliberate (`lib/lead_guard.py:381-387`:
"CUSTODY_RULES are checked first and win on overlap ... §10: 'start permissive on custody, strict on
provisioning'"), and the direction of the error is the safe one for a logging-only gate. Same note
as above: it becomes load-bearing the day blocking mode ships. The documented overlap case that IS
contractual is pinned by `::test_custody_wins_on_overlap` and
`::test_make_test_is_custody_but_bare_make_is_implementation`.

---

## Checked and found correct

Recorded so a later reader knows these were exercised rather than skipped.

**Fail-open (all six hooks, real subprocesses):** empty stdin, whitespace, non-JSON, truncated
JSON, JSON array / `null` / string / number / `{}`, NUL bytes; missing `session_id`, missing
`tool_input`, `tool_input` of the wrong type; `HOME` unset, `HOME` pointing at a regular file,
`~/.relay-tasks` absent, `~/.relay-tasks` existing as a file, a read-only state root; a corrupt
`config.json` (falls back to pure defaults, so no kill-switch reads as "off" out of a broken file);
a corrupt executor `session.json` (skipped without taking the wake down); a corrupt escalation
ledger. Every case exits 0. The Stop hook additionally never emits a stray exit 2, which would be a
spurious wake.

**Route guard:** the 39/40 line-threshold boundary and its config override; `MultiEdit` summing
across edits (ten four-line edits are gated); junk `edits` entries degrading to allow;
`Write` to an existing path sized rather than auto-blocked; new-file blocking and its
`block_on_new_file` switch; the deny JSON shape and exit code; the deny reason naming both escape
hatches, the measured size, and the honest Bash limit; silent allow writing nothing to stdout;
packet-file exemption for `~/.relay-tasks/**`, `*-packet.md` anywhere, `~`-relative paths, and a
symlink resolving into the state root, with lookalike names (`packet.md`, `001-packet.py`,
`x-packet.md.bak`) correctly *not* exempt; the retain grace window opening, expiring, staying
per-session, and closing the gate again when its stamp is corrupt; ledger records for blocks only;
unicode paths and a 200KB single-line edit counted as one line.

**Bash gate:** never denies, on twelve implementation verbs and seventeen custody/read verbs;
custody-wins-on-overlap; `make test` vs bare `make`; ledger record contents; `bash_gate_logging`
kill-switch; non-string/missing commands free-passing; unicode and very long commands.

**Stop hook:** silent for non-lead, executor and tombstoned sessions, and for a lead with nothing
new; report wake with the 🚦 marker, brief extraction (heading markers stripped, whitespace
collapsed, 200-char cap, empty-report fallback to the path); multiple reports in one wake;
ownership scoping (another lead's, unowned, closed and superseded executors never wake; `dead`
still does); per-packet keys; the whole #22/#23 two-phase stamp — announce marks pending not
surfaced, an undelivered wake retries, the retry cap fires and is ledgered, relay's own
continuation promotes while a foreign hook's does not, transcript-needle proof including the
offset rule that stops an *older* wake certifying a newer announce, one-shot claim consumption, and
an external `#17` channel stamp stopping the retry; App 2 commit surfacing (off by default, once
each when on, HEAD advancing regardless, non-git cwd, missing cwd); the one-time handoff nudge on
both the MB and the live-token signal, its kill-switch, its once-ever flag, and it riding an
existing wake; the posture-aware instruction (manual vs autonomous, and the five-condition commit
gate text); all three notification tiers plus `RELAY_NO_NOTIFY` and `notify_on_wake`, with a
positive control proving the kill-switch tests are not vacuous; the background poller — arming
only on our own in-flight executors, `stalled` counting as in-flight, waking on a report that lands
mid-poll, stopping when the lead steps down or the last executor leaves flight, timing out
silently, the single-poller lock (held, stale-reclaimed, and released on exit), and the `async`
claim kind; the heartbeat refreshing `last_active` and re-stamping the plugin version from the live
plugin root without dropping other marker fields.

**SessionEnd / SessionStart:** the full documented state machine on both sides, including every
"unknown reason preserves the arming" case that the 2026-07-10 incident produced; tombstone
losslessness; per-session scoping (one lead's exit never touches another's); idempotence of both
clear and tombstone; the ledger records; the round trip proving a resumed lead is gated *and*
wakeable again; `compact` never unarming; a stranger session never being armed; the re-arm
notification honouring both kill-switches, using its own subtitle rather than the misleading
default, and arming successfully even with no notifier on PATH at all.

**Executor escalation:** identity from `argv[1]` with the payload id as last-resort fallback (the
regression that kept the hook from ever firing in production); the four-way decision tree
(resolved / unowned / owner-missing / send) with the distinct human-facing reasons; the
once-per-packet gate and its #22 delivery-aware re-arm, including the legacy no-`confirmed` entry
reading as confirmed; `sent`-unconfirmed re-pushing while unsurfaced and stopping once surfaced;
`failed` being terminal; the queue delivery running ahead of the escalation kill-switch and being
skipped entirely when there is no queue file.

**Manifest:** every referenced script exists, is executable, carries `#!/usr/bin/env python3`, and
compiles; no orphan scripts and no double registration; matchers routing each event to the script
whose docstring names that event; the Stop entry's `asyncRewake`, `rewakeMessage` (marker matching
the hook's own stderr) and `rewakeSummary`; and the timeout consistency chain — declared, ≥
`poll_seconds`, ≥ the true worst case (`poll_seconds` + the 25s auto-close sweep + one
`poll_interval` overshoot = 1830s against the declared 1900s), read back through the exact
positional path `lead_guard._read_stop_hook_timeout` uses, and yielding `wake_hook_state == "ok"`
for a default lead.
