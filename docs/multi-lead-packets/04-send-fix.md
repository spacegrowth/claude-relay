# Packet 4 — reliable reuse / the `send` bug (§4)

`relay send` (reuse a live session for a follow-up packet) is frequently unavailable in practice,
so the lead is pushed to `spawn` fresh repeatedly — losing the context-reuse that `send` exists for.
Two root causes, both fixed here.

Design doc: `docs/multi-lead.md` (§4). You are building **relay itself** (the `claude-relay` repo).

## Root causes (confirmed in the code)

**(a) Stale status → false "busy" refusal.** `cmd_send`'s first guard is
`if s["status"] in ("busy", "stalled"): sys.exit(... refusing ...)`. But `s["status"]` is the
*stored* value — set to `busy` at spawn and only ever refreshed when `relay check`/`list` runs
`_check_one`. So right after an executor reports, its stored status is still `busy`, and `send`
refuses a session that's actually idle-and-reported.

**(b) Closed tab → forced cold spawn.** When `iterm.send` can't find the tab, `cmd_send` marks the
session `dead` and tells you to `relay spawn` — throwing away the context. But we now capture
`claude_session` at spawn, so a closed tab can be **resumed** instead.

## Build (`bin/relay` `cmd_send` + the report footer)

### A. Refresh liveness before the guard

At the top of `cmd_send`, **recompute the session's real status** using the same mechanism
`relay check` uses (`_check_one`, which already computes report-exists → `reported`, proc-alive →
`busy`, etc. and persists it), then re-read the session and make the guard decision on the *fresh*
status. Net effect: an idle, already-reported session becomes a valid send target;  a genuinely
busy one (process alive, no report yet) is still correctly refused (don't inject mid-turn).

### B. Resume-fallback when the tab is gone

Replace the current "`iterm.send` failed → mark dead → tell them to spawn" path with:
- If `iterm.send(...)` succeeds → as today (status busy, current_packet = n, etc.).
- If it fails **and** the session has a `claude_session` → **fall back to resume**: reopen the
  conversation and deliver the new packet in one shot via the existing relaunch path, e.g.
  `_relaunch(session_id, s, build_pointer_message(str(packet_path), packet_summary(body)),
  resume_id=s["claude_session"])`. This brings the context back AND delivers the packet — far better
  than a cold spawn. Set status busy / current_packet = n, log `packet_sent` (note it went via
  resume, e.g. an extra ledger field `via="resume"`).
- If it fails and there is **no** `claude_session` → keep today's behavior (mark dead, tell them to
  `relay spawn`).

### C. Convention: executors stay idle after reporting

In `TEMPLATE_FOOTER` (the report-format block appended to every packet), add one line to the GATES:
after writing the report, the executor should **remain idle — do not exit**; the lead may send a
follow-up packet and will `relay close` the session when done. (Claude sessions stay interactive by
default; this just makes the expectation explicit so tabs aren't closed prematurely, which is what
makes reuse work.)

## Gates

- Changes on disk, not committed (repo isn't under git).
- **Only touch:** `bin/relay`, `tests/`. (The footer lives in `bin/relay`.)
- **All 123 existing tests must still pass**, `claude plugin validate .` must pass.
- **Add tests** (mock `iterm.send` / `iterm.spawn`, `read_pid`, etc.):
  - a session whose *stored* status is `busy` but which has a report on disk → `cmd_send` refreshes,
    sees `reported`, and **proceeds** (the core bug-a fix).
  - a genuinely busy session (proc alive, no report) → still **refused**.
  - `iterm.send` fails + `claude_session` present → **resume-fallback** fires (`_relaunch` called with
    `resume_id == claude_session`), status ends busy, NOT marked dead.
  - `iterm.send` fails + no `claude_session` → marked dead (today's behavior).

## Report

What you changed in `cmd_send` (the refresh mechanism + the fallback), the footer line added, new
test names, and `pytest` + `validate` results. Note explicitly whether `_check_one` persists the
refreshed status or whether you had to adapt it.
