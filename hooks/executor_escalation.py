#!/usr/bin/env python3
"""
Stop hook, asyncRewake: armed on an EXECUTOR session via `--settings` at spawn (NOT the plugin's
own hooks.json — a plain `claude` launch gets no hooks at all; see bin/relay's cmd_spawn /
scripts/iterm.py's build_claude_cmd). This is wake-watch design §4.1's executor-side escalation:
once this executor's report lands and it goes idle, watch (in the background) for the owning lead
to notice it, and escalate — nudge the idle lead, or notify the human directly — if it doesn't.

This is a NET UNDER the lead's own fast-path poller (hooks/stop_lead_watch.py), not a replacement:
a healthy idle lead surfaces the report at its own next Stop long before this hook's grace window
even elapses. Contract mirrors stop_lead_watch.py's discipline: fail-open (any error → exit 0),
gated to act ONLY on a genuine relay executor session with a report already on disk, one background
poller per (executor, packet) via a lock file, bounded total runtime (the harness kills the
background process at the settings file's declared `timeout` regardless — see
docs/async-rewake-executor-findings.md — so this exits cleanly well before that, and a later Stop
event re-arms it, picking the backoff schedule back up from the persisted ledger).

HARD RULE: any error → exit 0. A bug here must never brick the executor's normal Stop behavior.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")
RELAY_BIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "bin", "relay")

# Defaults for the three config-overridable timings (lead/config.json keys below) — same pattern
# as the lead's own poll_seconds/poll_interval (lib/lead_guard.py's LEAD_DEFAULTS). Tests shrink
# these via config rather than patching module constants, exactly like
# TestStopHookBackgroundPoll drives the real stop_lead_watch.py subprocess.
GRACE_SECONDS_DEFAULT = 60          # let the lead's own healthy fast-path win first (design §4.1)
POLL_INTERVAL_SECONDS_DEFAULT = 15  # how often the background loop re-checks while waiting
MAX_RUNTIME_SECONDS_DEFAULT = 1800  # exit cleanly well inside the settings file's declared
                                     # Stop-hook `timeout` (1900 by default — see
                                     # lead_guard.write_escalation_settings) rather than being
                                     # SIGKILLed; a later executor turn re-arms and resumes backoff
                                     # from the persisted ledger.

BACKOFF_SCHEDULE_SECONDS = [60, 300, 900]   # 60s -> 5m -> 15m, then steady at the last value —
                                     # fixed, not config-driven (packet's "minimal is fine" scope)
BUSY_WAIT_TIMEOUT_SECONDS = 900     # a patient `wait` (busy lead) escalates to the human after
                                     # waiting this long without the lead surfacing it itself —
                                     # also fixed for the same reason


def _next_backoff(attempts):
    idx = min(attempts, len(BACKOFF_SCHEDULE_SECONDS) - 1)
    return BACKOFF_SCHEDULE_SECONDS[idx]


def _notify_human(lg, cfg, sid, packet, owner_lead, reason):
    """Reuses stop_lead_watch.py's own `_notify` (same dir, imported lazily) — NOT a second
    notification path. `focus_sid` (the click target) is the owning lead if one is reachable
    (that's where the human should go review), else the executor itself."""
    try:
        hooks_dir = os.path.dirname(os.path.realpath(__file__))
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import stop_lead_watch as slw
        marker = lg.read_marker(STATE_ROOT, owner_lead) if owner_lead else {}
        exec_s = lg.read_session_json(STATE_ROOT, sid)
        project = marker.get("project") or exec_s.get("owner_project")
        message = f"executor '{sid}' packet {packet:03d} still unhandled — {reason}"
        focus_sid = owner_lead if marker else sid
        iterm_session = marker.get("iterm_session") or exec_s.get("iterm_session")
        slw._notify(cfg, message, project=project, executor=sid, lead_sid=focus_sid,
                    iterm_session=iterm_session)
    except Exception:
        pass


def _nudge_lead(owner_lead, sid, packet):
    """`relay nudge-lead` — the phase-1 primitive, invoked exactly as a human would from the CLI
    (not reimplemented here). Best-effort; a failed nudge isn't fatal — the next cycle just
    re-evaluates the lead's state (still idle/now busy/now stale) and tries again on schedule."""
    try:
        msg = f"executor '{sid}' reported (packet {packet:03d}) while you were idle — review it."
        subprocess.run([RELAY_BIN, "nudge-lead", owner_lead, msg], capture_output=True, timeout=10)
    except Exception:
        pass


_HUMAN_REASON = {
    "unowned": "it has no owning lead to notice it",
    "owner-missing": "its owning lead's session is gone (crashed, closed, or pruned)",
    "stale": "its owning lead appears wedged or crashed mid-turn",
}


def _run_escalation_loop(lg, cfg, sid, n, owner_lead):
    """The background poll: grace, then repeatedly re-evaluate lg.escalation_decision and act,
    persisting attempts/backoff in the executor's own escalation ledger (separate from the owning
    lead's surfaced_reports.json — design §4.4) until resolved, terminal, or the runtime budget
    runs out. grace/poll-interval/max-runtime are config-overridable (same pattern as the lead's
    own poll_seconds/poll_interval) so tests can shrink them instead of patching module state."""
    grace = max(0, int(cfg.get("executor_escalation_grace_seconds", GRACE_SECONDS_DEFAULT)))
    poll_interval = max(1, int(cfg.get("executor_escalation_poll_interval", POLL_INTERVAL_SECONDS_DEFAULT)))
    max_runtime = max(1, int(cfg.get("executor_escalation_max_runtime_seconds", MAX_RUNTIME_SECONDS_DEFAULT)))

    start = time.time()
    time.sleep(grace)

    busy_since = None
    while time.time() - start < max_runtime:
        lg.heartbeat_escalation_lock(STATE_ROOT, sid)

        # Terminal states: a closed/superseded executor is done being watched, regardless of
        # whether its report was ever explicitly surfaced.
        cur = lg.read_session_json(STATE_ROOT, sid)
        if cur.get("status") in ("closed", "superseded"):
            return

        decision = lg.escalation_decision(STATE_ROOT, sid, n, owner_lead)
        ledger = lg.load_escalation(STATE_ROOT, sid)
        key = str(n)
        record = ledger.get(key, {"attempts": 0})

        if decision == "resolved":
            record["resolved"] = True
            ledger[key] = record
            lg.save_escalation(STATE_ROOT, sid, ledger)
            return

        if decision == "wait":
            # Patient: a busy lead surfaces this at its own next Stop (design §4.3) — only escalate
            # if it's been busy without resolving for a good while.
            if busy_since is None:
                busy_since = time.time()
            elif time.time() - busy_since > BUSY_WAIT_TIMEOUT_SECONDS:
                _notify_human(lg, cfg, sid, n, owner_lead,
                              "the owning lead has been busy for a while and hasn't picked it up yet")
                record["attempts"] = record.get("attempts", 0) + 1
                record["last_action"] = "notify"
                record["next_eligible"] = time.time() + _next_backoff(record["attempts"])
                ledger[key] = record
                lg.save_escalation(STATE_ROOT, sid, ledger)
                busy_since = time.time()  # restart the wait-timeout window after escalating once
            time.sleep(poll_interval)
            continue

        busy_since = None  # no longer waiting on a busy lead

        now = time.time()
        if record.get("next_eligible", 0) > now:
            time.sleep(poll_interval)
            continue

        if decision == "nudge":
            _nudge_lead(owner_lead, sid, n)
            record["last_action"] = "nudge"
        else:  # "unowned" | "owner-missing" | "stale"
            _notify_human(lg, cfg, sid, n, owner_lead, _HUMAN_REASON[decision])
            record["last_action"] = "notify"

        record["attempts"] = record.get("attempts", 0) + 1
        record["next_eligible"] = now + _next_backoff(record["attempts"])
        ledger[key] = record
        lg.save_escalation(STATE_ROOT, sid, ledger)
        time.sleep(poll_interval)
    # Ran out of this invocation's runtime budget without resolving — a later executor turn's Stop
    # event re-arms the hook and resumes from the persisted ledger (attempts/next_eligible intact).


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        import lead_guard as lg
        sid = payload.get("session_id")
        if not sid:
            sys.exit(0)
        s = lg.read_session_json(STATE_ROOT, sid)
        if not s:
            sys.exit(0)  # not a relay executor session → silent, zero impact

        cfg = lg.load_config(STATE_ROOT)
        if not cfg.get("executor_escalation", True):
            sys.exit(0)  # kill-switch

        n = int(s.get("current_packet", 1))
        report_path = os.path.join(STATE_ROOT, sid, "packets", f"{n:03d}-report.md")
        if not os.path.exists(report_path):
            sys.exit(0)  # idle mid-work, nothing written yet → nothing to escalate

        owner_lead = s.get("owner_lead")

        if not lg.acquire_escalation_lock(STATE_ROOT, sid):
            sys.exit(0)  # a poller for this executor is already watching
        try:
            _run_escalation_loop(lg, cfg, sid, n, owner_lead)
        finally:
            lg.release_escalation_lock(STATE_ROOT, sid)
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # hard fail-open


if __name__ == "__main__":
    main()
