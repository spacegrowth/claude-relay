# Bug-hunt findings — area `cli` (`bin/relay`'s untested subcommands)

Packet: `~/.relay-tasks/bh-cli/packets/001-packet.md`. Branch `wt/bh-cli`, off `main` @ `b659a73`.
Ground rules: `~/.relay-tasks/_staging/bughunt-common.md`.

**6 BUG findings, 3 AMBIGUOUS, 2 out-of-area observations, 0 SUSPECT-TESTs.**
Every BUG has a repro test marked `@pytest.mark.xfail(strict=True)`, so the suite stays green and
the bug stays visible; removing the marker without fixing the code fails the suite, and fixing the
code without removing the marker also fails it. Nothing in `bin/relay`, `lib/`, `hooks/`, `skills/`
or any existing test file was edited — proposed fixes below are descriptions, not applied changes.

Test files added: `tests/test_cli_spawn_send.py`, `tests/test_cli_queue.py`,
`tests/test_cli_lifecycle.py`, `tests/test_cli_close_retire_prune.py`, `tests/test_cli_lead.py`,
`tests/test_cli_lead_succession.py`, `tests/test_cli_tools.py` — 316 tests (304 pass, 12 xfail).

---

## BUG-cli-1 — one unparsable `session.json` takes down every multi-session command

**Severity:** crash (all of `relay list`, `check --all`, `prune`, `stats`, `board`, `whoami`).

**Contract.** `bin/relay:1937-1940`, `_busy_elapsed`'s docstring, states the standing rule for
hand-edited state:

> Never throws — one hand-edited or legacy session.json must not take down `relay list`/`check --all`.

`read_queue` (`bin/relay:447-459`) states the same rule for its own file and *implements* it:

> A missing/corrupt file reads as empty — a hand-edited queue.json must never take down
> `relay list`/`check`.

**Actual.** `read_session` (`bin/relay:404-409`) has no such guard:

```python
def read_session(session_id):
    p = session_dir(session_id) / "session.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())        # ← raises on a corrupt or half-written file
```

`all_session_ids()` (`bin/relay:417-421`) lists any directory that merely *has* a `session.json`,
so every command that iterates sessions calls `read_session` on the broken one and dies with an
unhandled `json.decoder.JSONDecodeError` traceback — not a message, not a partial result. Call
sites confirmed to crash: `cmd_prune` (`bin/relay:4024`), `_check_one` via `cmd_check`
(`bin/relay:2023`), `cmd_list`, `stats_data` (`bin/relay:2612`), `board_data`, and
`_resolve_whoami`'s uuid scan (`bin/relay:4631-4634`).

The realistic trigger is not a human with a text editor: `write_session` (`bin/relay:411-415`)
writes in place (`(d / "session.json").write_text(...)`), not via temp-file + `os.replace` the way
`write_queue` (`bin/relay:460-476`) and `auto_trust` (`bin/relay:669-696`) both do — so a crash or
a concurrent reader inside that `write_text` truncation window sees exactly this state. A single
executor dir then bricks the lead's whole view of the world.

**Repro test.** `tests/test_cli_tools.py::TestCorruptSessionJson::
test_one_broken_session_does_not_take_the_command_down` (6 parametrisations: list / check-all /
prune / stats / board / whoami). Companion, not xfailed:
`test_an_empty_session_json_is_the_same_hazard`, and `test_a_corrupt_queue_file_really_does_read_
as_empty` / `test_a_corrupt_config_falls_back_to_defaults` for the contrast.

**Proposed fix.** Make `read_session` fail the way its siblings do, and make `write_session`
atomic:

```python
def read_session(session_id):
    p = session_dir(session_id) / "session.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None          # unreadable == not a session, same convention as read_queue
    return d if isinstance(d, dict) else None
```

`None` is already the "no such session" value every caller handles (`cmd_send`, `cmd_resume`,
`cmd_keep`, … all `sys.exit(f"no such session: …")` on it), *except* `cmd_prune:4024` and
`stats_data:2758`, which do a bare `s.get(...)` and would need an explicit `if not s: continue`.
Separately, `write_session` should write to `session.json.tmp` and `os.replace` it, closing the
window that produces the half-written file in the first place.

---

## BUG-cli-2 — `relay check` / `relay list` / `relay board` auto-park **another lead's** executors

**Severity:** wrong-result (one lead's routine status check silently closes a different lead's
in-flight session and closes its terminal tab).

**Contract.** `README.md:594-595`, the Auto-close section:

> The sweep runs on `relay check`, `relay list`, and every lead turn-end (the Stop hook), **scoped
> to the lead's own executors**;

`auto_close_sweep`'s own docstring (`bin/relay:2150-2158`) says the same thing about the parameter
that implements it:

> `lead_sid` limits it to that lead's own executors (the hook path — a lead only parks what it
> owns).

**Actual.** Only the Stop-hook path passes it. Of the four call sites:

```
bin/relay:2763   auto_close_sweep("lead-stop", sids=sids, lead_sid=lead)   ← scoped
bin/relay:2795   auto_close_sweep("check",     sids=[sid for sid in ids if sid])   ← NOT scoped
bin/relay:3242   auto_close_sweep("list",      sids=[r["session_id"] for r in rows])  ← NOT scoped
bin/relay:2434   auto_close_sweep("board",     sids=[r["session_id"] for r in rows])  ← NOT scoped
```

`relay check --all` passes every session id on the machine, and `relay list` / `relay board`
without `--lead` do the same, so lead A running its own "where are things" beat evaluates and parks
lead B's executors — `cmd_close` teardown included, which SIGTERMs the process and closes the tab.
The surfaced-set precondition inside `auto_close_decision` limits *which* sessions qualify (B must
already have seen the report) but does not limit *who does the parking*: it is checked against the
session's own `owner_lead`, not against the caller.

Verified live in the test: with `e-of-B` owned by `lead-B` and surfaced to `lead-B`, running
`relay check --all` as `lead-A` leaves `e-of-B` `closed`.

**Repro test.** `tests/test_cli_lifecycle.py::TestAutoCloseSweep::
test_check_all_does_not_park_another_leads_executor`. The scoped path is separately proven to work:
`test_lead_scoped_sweep_ignores_another_leads_executor` and
`TestAutoCloseSweepCommand::test_it_leaves_another_leads_executor_alone` both pass.

**Proposed fix.** Pass the caller's lead id at the three unscoped sites, exactly as the Stop hook
does — the value is already available as `os.environ.get("CLAUDE_CODE_SESSION_ID")`:

```python
# cmd_check, bin/relay:2795
caller = os.environ.get("CLAUDE_CODE_SESSION_ID")
acted = auto_close_sweep("check", sids=[sid for sid in ids if sid],
                         lead_sid=caller if caller and lead_guard.is_lead(STATE_ROOT, caller) else None)
```

…and the same for `cmd_list:3242` and `cmd_board:2434`. Note `lead_sid=None` must keep meaning
"unscoped" for a non-lead caller only if that is genuinely wanted; if not, a non-lead caller should
sweep nothing. That choice is the lead's to make — flagging it rather than deciding it.

---

## BUG-cli-3 — `close-predecessor` addresses the outgoing lead's tab through the CALLER's backend

**Severity:** wrong-result (Defect A repeated: a cross-backend handoff leaves the zombie tab open
forever, and the marker field that pointed at it is cleared anyway, so the offer never returns).

**Contract.** `_lead_tab_target`'s docstring (`bin/relay:4539-4560`) states the invariant for every
lead-tab operation:

> Two identities, **both from the marker rather than from the caller's ambient state**: the BACKEND
> that hosts the tab … and the HANDLE … Leaving the handle out makes an operation label-aware, and
> lead labels are NOT unique.

`cmd_nudge_lead`'s docstring (`bin/relay:4566-4580`) names the live incident from doing otherwise:

> Sends via the marker's OWN recorded backend (`term_backend`), never the caller's ambient guess —
> Defect A, live-reproduced: an executor running under Terminal.app selects the `terminal` backend
> for ITSELF, and used to hand that same wrong backend to an iTerm-hosted lead …

`cmd_handoff` already honours it for the very same tab, one function earlier — its `[ex-Lead]`
retitle resolves `pred_bk = backend.by_name(caller_marker.get("backend")) or iterm`
(`bin/relay:4366`).

**Actual.** `cmd_close_predecessor` calls the module-level `iterm` (this invocation's *selected*
backend) unconditionally, four times:

```
bin/relay:4399   pred_tty = iterm.tty_by_id(...)
bin/relay:4400   pred_pid = iterm.pid_on_tty(pred_tty)
bin/relay:4407   closed   = iterm.close(pred.get("tab_label") or "", pred.get("iterm_session"), None)
bin/relay:4410   elif not iterm.is_alive(pred.get("tab_label") or "", pred.get("iterm_session")):
```

And the `predecessor` dict `cmd_handoff` records (`bin/relay:4262-4264`) carries only
`session_id` / `tab_label` / `iterm_session` — there is no `backend` field to key off even if the
call site wanted one. So a predecessor lead that ran under Terminal.app cannot be closed from a
successor running under iTerm (and vice versa): `close` misses, `is_alive` misses, the command
prints "tab not auto-closed", then `marker.pop("predecessor", None)` (`bin/relay:4414`) deletes the only
record of that tab's identity, permanently stranding it — the exact failure mode §4 was written to
end.

Verified in the test: with the predecessor recorded as `backend: "terminal"` and this invocation
selecting iTerm, only the `iterm` module's `close` is ever called.

**Repro test.** `tests/test_cli_lead_succession.py::TestClosePredecessor::
test_it_addresses_the_predecessors_own_backend`. The nudge-lead equivalent, which DOES honour the
contract, passes: `TestNudgeLead::test_it_uses_the_markers_recorded_backend`.

**Proposed fix.** Two halves.

1. `cmd_handoff` records the predecessor's backend (it is already reading `caller_marker`):
   ```python
   predecessor = {"session_id": caller, "tab_label": caller_marker.get("tab_label"),
                  "iterm_session": caller_marker.get("iterm_session"),
                  "backend": caller_marker.get("backend")}
   ```
2. `cmd_close_predecessor` resolves the backend from that field with the same fallback chain the
   rest of the file uses, then addresses every one of the four calls through it:
   ```python
   pred_bk = (backend.by_name(pred.get("backend"))
              or _probe_backend_for_tab(pred.get("tab_label"), pred.get("iterm_session"))
              or iterm)
   ```
   A pre-fix marker with no `backend` field then goes through `_probe_backend_for_tab` rather than
   the ambient guess — the same degradation `_lead_tab_target` already specifies.

---

## BUG-cli-4 — `relay resume`'s second-live-copy guard is pid-only, so a pid-less session resumes twice

**Severity:** wrong-result (two live `claude` processes on one conversation id, which the code
itself calls "stomp on each other").

**Contract.** `cmd_resume_lead` (`bin/relay:1391-1410`) states the invariant, implements the
stronger check, and explicitly claims to be copying the executor's:

> Refuse to open a SECOND live copy of the same conversation (**mirrors the executor guard**).
> Aliveness: a pid recorded by a previous restore, **else the marker's relay-controlled tab title**.

…exiting with `"two live copies of one conversation stomp on each other"` (`bin/relay:1409`).
`cmd_spawn` warns, at `bin/relay:1209`, that the pid is sometimes simply not obtainable, and says
what covers the gap:

> could not read PID, **aliveness/stall detection will rely on tab title only**

`_check_one` (`bin/relay:2058-2065`) implements that fallback for status.

**Actual.** The executor guard is pid-only, in both `cmd_resume` (`bin/relay:1491`) and
`cmd_restart` (`bin/relay:1359`):

```python
if session_pid_alive(s) and not args.force:
    sys.exit(f"session '{sid}' still looks alive (pid {s.get('pid')}) — close it first or pass --force")
```

`session_pid_alive` (`bin/relay:714-727`) returns `False` the moment `s["pid"]` is `None`. So an
executor whose pidfile never came back — the case `cmd_spawn` prints a warning for, and the case
`_check_one` handles by consulting the tab — resumes with no refusal at all, and `_relaunch` starts
a second `claude --resume <same uuid>` alongside the first. The tab is never consulted, even though
the handle is recorded in `session.json` and `term_backend(s).is_alive(...)` is one line away.

`relay restart` shares the pid-only guard but is the milder case: it mints a *fresh* conversation
id, so it duplicates a tab rather than a conversation. The finding is filed against `resume`.

**Repro test.** `tests/test_cli_lifecycle.py::TestResumeExecutor::
test_resume_refuses_when_only_the_tab_says_it_is_alive`. The lead-side equivalent, which honours
the contract, passes: `TestResumeLead::test_a_live_lead_tab_refuses_the_restore`.

**Proposed fix.** Give the executor guard the same two-tier aliveness the lead guard has:

```python
alive = session_pid_alive(s)
if not alive and s.get("tab_label"):
    alive = term_backend(s).is_alive(s["tab_label"], s.get("iterm_session"), s.get("pid"))
if alive and not args.force:
    sys.exit(f"session '{sid}' still looks alive … pass --force")
```

Note the ordering matters and the tab probe must come *second*: `_check_one`'s comment
(`bin/relay:2036-2043`) is emphatic that liveness is process-primary, because Claude Code mutates
its own tab title while working. Consulting the tab only when there is no pid at all preserves
that.

---

## BUG-cli-5 — `relay stats --lead` and `relay whoami <token>` bypass the universal name resolver

**Severity:** `stats` — wrong-result, and silently so (an empty table, not an error).
`whoami` — wrong-message (a refusal for a name the README says works).

**Contract.** `README.md:221-225`, immediately under the command table that lists both
`relay stats [--lead SID]` (line 182) and `relay whoami [<token>]` (line 216):

> Anywhere a command above takes a session id, you can pass the executor's name (its id, set at
> `spawn --name`), a lead's project name, or a unique prefix of either's id — no more pasting lead
> UUIDs. `relay focus webapp` and `relay stop docs-site` just work.

`main()`'s `RESOLVE_FIELDS` comment (`bin/relay:5060-5068`) names the complete set of exemptions:

> Two deliberate exclusions, per resolve_sid's own docstring: `lead-start`'s session_id CREATES the
> lead namespace … and `handoff`'s positional is a path, not a sid — neither appears below.

Neither `stats` nor `whoami` is one of those two.

**Actual.** `RESOLVE_FIELDS` (`bin/relay:5069-5081`) has no `"stats"` key and no `"whoami"` key,
though it does route the identically-shaped `"list": ["lead"]` and `"board": ["lead"]`.

- `relay stats --lead webapp` → `stats_data(lead_sid="webapp")` compares the literal string
  `"webapp"` against each session's `owner_lead` UUID (`bin/relay:2615`), matches nothing, and
  prints an empty table. No error, no hint — the lead reads "no packets recorded" for a project
  that has a full history.
- `relay whoami webapp` → `_resolve_whoami` (`bin/relay:4617-4636`) tries marker / session /
  claude_session and exits `whoami: could not resolve 'webapp' to a known executor or lead`.
  `whoami`'s own docstring restates the three token shapes it accepts, so it is arguably
  self-consistent; the README sentence above is what it contradicts.

**Repro tests.** `tests/test_cli_tools.py::TestStats::test_lead_scoping_accepts_a_project_name`
and `tests/test_cli_lead_succession.py::TestWhoami::test_a_lead_project_name_resolves`. The
already-working equivalents pass: `TestStats::test_lead_scoping_keeps_unowned_executors` (raw sid),
`TestFocus::test_a_lead_can_be_focused_by_project_name`, and
`TestBoard::test_lead_scoping_keeps_unowned_executors`.

**Proposed fix.** Add the two entries — this is the one place the docstring says a new sid surface
gets wired in:

```python
RESOLVE_FIELDS = {
    ...
    "stats": ["lead"],
    "whoami": ["token"],
}
```

`resolve_sid` returns the token unchanged when nothing matches (clause (e) of its docstring), so
`whoami`'s claude_session-uuid path is unaffected: a uuid resolves against nothing and falls
through to `_resolve_whoami` exactly as today. Worth also checking `msg-failed`'s `session_id`
(`bin/relay:4974-4980`), which is likewise absent — it is a hook-only command, so probably deliberate,
but nothing says so.

---

## BUG-cli-6 — the `mcp-unparsable` lint finding can never fire

**Severity:** wrong-result (a malformed `MCP:` line lints clean, then refuses the spawn later).
**Locus:** `lib/lead_guard.py` — out of this packet's edit scope, filed here because it is
`relay lint`'s user-visible behaviour and `relay lint` is in this area.

**Contract.** `lib/lead_guard.py:1936-1938` declares the finding and its message states the rule:

```python
if mcp_line and mcp_spec is None:
    out.append(("warn", "mcp-unparsable", f"MCP: line present but its value isn't none/inherit/a,b: "
                f"'{mcp_line.group(1)}'"))
```

**Actual.** `normalize_mcp_spec` (`lib/lead_guard.py:2081-2095`) returns `None` for *nothing*: any
non-empty string that is not a recognised keyword falls through to `normalize_mcp_spec(s.split(","))`
and comes back as a sorted allowlist. So `packet_mcp_spec` is `None` only when there is no `MCP:`
line at all — in which case `mcp_line` is `None` too, and the branch is unreachable.

`MCP: yes please if you can` therefore lints as `["can", "yes please if you"]` and reports nothing.
Worse, the only check that would catch those junk names — `mcp-unknown-server` — needs
`known_servers`, which `cmd_lint` only computes when `--worktree` is passed (`bin/relay:601-618`),
and the README documents plain `relay lint <packet.md>` as the normal invocation. The lead gets a
clean bill of health and then a hard spawn refusal (`spawn: MCP (packet MCP: line): …`) at launch.

**Repro test.** `tests/test_cli_tools.py::TestLint::test_an_unparsable_mcp_line_is_warned_about`.

**Proposed fix.** Reject values that cannot be server names before falling through to the
comma-split — e.g. in `normalize_mcp_spec`, return `None` for a raw value whose parts contain
whitespace or are otherwise not plausible identifiers, and have every *caller* that treats `None`
as "not declared" keep doing so (`packet_mcp_spec` would need a separate sentinel, since `None`
there already means "no line"). A smaller, safer alternative that stays inside the linter: have
`lint_packet` re-check `mcp_line.group(1)` itself and warn when any comma-part fails a
`^[A-Za-z0-9_.-]+$` test. The second is the one I would take — it changes no resolution behaviour,
only the advisory output.

---

## AMBIGUOUS-cli-1 — deleting the highest-numbered packet re-issues its number

`next_packet_number` (`bin/relay:423-427`) is `max(existing) + 1`. Delete `003-packet.md` while
`003-report.md` remains, and the next `relay send` writes a fresh `003-packet.md` pointing the
executor at the *existing* `003-report.md` — silently overwriting a previous packet's report on the
executor's first write. An interior hole is safely never backfilled (tested, passing:
`TestSendPacketNumbering::test_an_interior_hole_is_not_backfilled`), so only the top of the range
is exposed.

Classified AMBIGUOUS, not BUG: nothing in the README, the skills or the docstrings promises that
numbers are never re-issued, and deleting packet files by hand is not a documented workflow. If the
lead considers packet numbers monotone-forever, the fix is to take the max over
`*-packet.md` ∪ `*-report.md` ∪ `*-diff.html` rather than packets alone.

## AMBIGUOUS-cli-2 — `restart` / `resume` do not consider the *stored* `busy` status

Both guards ask only "is the process alive" (see BUG-cli-4). A session recorded `busy` whose
process has genuinely died is therefore restartable with no `--force`, which is almost certainly
the intent (that is the whole recovery path the restart skill describes). The packet's contract
list phrased it as "both refuse on a busy session", which the code does not do and — reading
restart/SKILL.md's "If the session still looks alive, relay refuses" — should not do. No test
written; recording it so the next reader does not re-derive it.

## AMBIGUOUS-cli-3 — queue ids restart at 1 and reuse the body filename

`enqueue_packet` (`bin/relay:477-497`) takes `max(id of current items) + 1`, so once the queue
drains the next item is `#1` again and its body overwrites `queue/001-queued.md`. Harmless today —
the previous `#1` was already delivered and its real packet lives under `packets/` — but it means
`queue_id` in the ledger (`packet_queued` / `queue_delivered` / `queue_cancelled`) is not unique
per session over time, which any later analysis of those events would have to know. No documented
contract either way. Tested behaviourally as FIFO-and-once
(`TestWhenIdleQueue::test_delivery_is_fifo_and_exactly_one_per_idle_transition`), not as id
uniqueness.

---

## Out-of-area observations (no test written; for whoever owns these files)

- **`lead_guard.is_lead` returns True for a corrupt marker.** `lib/lead_guard.py:437-451` checks
  `marker_path(...).exists()` and then `not is_tombstoned(read_marker(...))`; `read_marker` swallows
  the parse error and returns `{}`, `is_tombstoned({})` is False, so an unparsable marker reads as
  *armed*. Its docstring says "Marker absent (**or any error**) → not lead". Belongs to the `lib`
  bughunt area; `tests/test_cli_tools.py::TestCorruptSessionJson::test_a_corrupt_marker_reads_as_no_lead`
  asserts only the `read_marker` half, deliberately.
- **`write_session` is not atomic** — see BUG-cli-1's fix note. Same file, same area, but it is the
  *cause* rather than the symptom, so it is recorded there.

## Packet-premise corrections (for the lead, not bugs in relay)

1. **Coverage.** The packet states `bin/relay` is at "39%, 1673 uncovered lines". Measured on this
   branch at `b659a73` with the packet's own command, the baseline is **88% (2665 statements, 320
   uncovered)**. Per-`cmd_*` at baseline, only four were under the packet's 75% bar:
   `cmd_doctor` 1.5%, `cmd_auto_close_sweep` 8.3%, `cmd_deliver_queued` 20%,
   `cmd_close_predecessor` 73.3%. Everything else was already 77-100%. The work was retargeted
   accordingly: those four first, then bug-hunting rather than coverage-filling. After this
   packet: **93% overall (198 uncovered)**, and every `cmd_*` above is at 83.3-100%.
2. **`relay.main([...argv...])` does not work.** `main()` (`bin/relay:4787`) takes no parameters and
   reads `sys.argv`; the tests drive it through a `run_main()` helper that patches `sys.argv`.
3. **There is no packet `MODEL:` line.** The packet's spawn contract list names "packet `MODEL:`
   line vs `--model`"; `lib/lead_guard.py` has `CONTEXT_RE`, `EFFORT_RE` and `PACKET_MCP_RE` but no
   model equivalent, and nothing in README/skills documents one. Model precedence is
   `--model` > config `executor_default_model` > `"sonnet"`, which is what the tests assert.
4. **`--effort` does not land in the per-executor `settings.json`.** That file
   (`lead_guard.write_escalation_settings`) carries the escalation Stop hook and `fallbackModel`
   only; effort is a launch-argv flag (`iterm.spawn(..., effort=…)`). Tested on the argv side.
5. **`relay lint` has no GOAL-first-line rule.** The packet asks what lint says about
   "GOAL must be the first line"; that rule exists only in `skills/mode/SKILL.md:217` as lead-
   authoring doctrine, and `lint_packet` implements no check for it. Not filed as a bug (no
   contract claims lint enforces it) — noting it in case the lead wants the rule added.

## Existing tests

No `SUSPECT-TEST` findings. Every test in `tests/test_relay.py` that overlaps this area was read
before writing the new ones; none asserts behaviour that contradicts a documented contract.
