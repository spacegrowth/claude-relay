#!/usr/bin/env python3
"""
Stop hook, single-shot PUSH: armed on an EXECUTOR session via `--settings` at spawn (NOT the
plugin's own hooks.json — a plain `claude` launch gets no hooks at all; see bin/relay's cmd_spawn /
scripts/iterm.py's build_claude_cmd). This is wake-watch design §9's replacement for the old
watcher (§4): once this executor's report lands and it goes idle, type a message into the owning
lead's tab ONCE — no grace window, no poll loop, no backoff, no busy check. §9.5b proved injecting
mid-turn is harmless (text queues in the lead's input box and is processed intact at turn-end), so
the rule is simply "always send."

Net UNDER the lead's own fast-path check (hooks/stop_lead_watch.py), not a replacement: a healthy
idle lead surfaces the report at its own next Stop before this hook ever fires. Both paths are kept
deliberately (§9.6 #2) and deduped via the owning lead's surfaced_reports.json (§9.6a: the resolved
path logs `escalation_resolved` instead of exiting silently, so the dedup working and a dead hook
don't look identical).

Learns its own IDENTITY through `relay whoami --json` rather than reaching into
~/.relay-tasks/<name>/session.json by hand — one more subprocess (this hook already shells out to
`relay nudge-lead`), but now every other relay operation and this hook go through the same command
contract. Still learns its NAME from argv[1] (baked in at spawn by
lead_guard.build_escalation_settings) — deriving it from Claude Code's own payload is exactly the
bug that kept this hook from EVER firing in production (see the regression test pinning this).

HARD RULE: any error → exit 0. A bug here must never brick the executor's normal Stop behavior.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")
RELAY_BIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "bin", "relay")


def _whoami(sid):
    """`relay whoami --json <sid>` — this executor's own identity, its current packet + report
    state, and its owning lead's reachability, all in one read instead of the hook reconstructing
    them from `lg.read_session_json` + manual `owner_lead` / report-path joins. Returns the parsed
    dict, or None on ANY failure (unresolvable token, `relay` missing, malformed output, non-
    executor role) — the caller treats None exactly like the old "not a relay executor session"
    case: silent, zero impact. Never raises."""
    try:
        r = subprocess.run([RELAY_BIN, "whoami", "--json", sid], capture_output=True, text=True,
                           timeout=10)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        return data if data.get("role") == "executor" else None
    except Exception:
        return None


def _notify_human(lg, cfg, sid, packet, owner_lead, reason):
    """Reuses stop_lead_watch.py's own `_notify` (same dir, imported lazily) — NOT a second
    notification path. The ONE fallback (§9.2): no reachable owning lead to push to."""
    try:
        hooks_dir = os.path.dirname(os.path.realpath(__file__))
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import stop_lead_watch as slw
        marker = lg.read_marker(STATE_ROOT, owner_lead) if owner_lead else {}
        exec_s = lg.read_session_json(STATE_ROOT, sid)
        project = marker.get("project") or exec_s.get("owner_project")
        message = f"executor '{sid}' packet {packet:03d} reported — {reason}"
        focus_sid = owner_lead if marker else sid
        iterm_session = marker.get("iterm_session") or exec_s.get("iterm_session")
        slw._notify(cfg, message, project=project, executor=sid, lead_sid=focus_sid,
                    iterm_session=iterm_session)
    except Exception:
        pass


def _push_to_lead(owner_lead, sid, packet):
    """`relay nudge-lead` — the phase-1 primitive, invoked exactly as a human would from the CLI
    (not reimplemented here). Sent UNCONDITIONALLY (§9.5b — no busy check: a busy lead just queues
    it). Returns True only if the send actually succeeded (checked `returncode`) — a failed push
    must not be recorded as delivered (D4: the once-per-packet ledger used to lie about this).
    Best-effort otherwise; with no retry left in this design, a failed push falls through to the
    once-per-packet mark anyway — the lead's own fast path (or a human via `relay list`) is the net
    under a push that didn't land."""
    try:
        msg = f"executor '{sid}' reported (packet {packet:03d}) while you were idle — review it."
        r = subprocess.run([RELAY_BIN, "nudge-lead", owner_lead, msg], capture_output=True,
                           timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _deliver_queued(sid):
    """Hand any `--when-idle` queued packet to `relay _deliver-queued` (task #18). Invoked as the
    CLI, exactly like this hook's other two subprocess calls, so the queue has ONE delivery
    implementation rather than a hook-local copy. Skips the subprocess entirely when there's no
    queue file — the overwhelmingly common Stop. Never raises."""
    try:
        if not os.path.exists(os.path.join(STATE_ROOT, sid, "queue.json")):
            return
        subprocess.run([RELAY_BIN, "_deliver-queued", sid], capture_output=True, timeout=30)
    except Exception:
        pass


def _mark(lg, sid, packet, status):
    """Record this packet's terminal state so a later Stop (repeated executor idles) gates out
    before re-checking anything — the whole of what `escalation.json` needs to be now that there is
    nothing to retry or back off from. `status` is "resolved" | "notified" | "sent" | "failed" —
    "failed" still occupies the once-per-packet slot (no retry, per design), it just tells the
    truth instead of claiming delivery that didn't happen (D4)."""
    ledger = lg.load_escalation(STATE_ROOT, sid)
    ledger[str(packet)] = {"status": status}
    lg.save_escalation(STATE_ROOT, sid, ledger)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        import lead_guard as lg
        # The hook is TOLD its relay name as argv[1] (baked in by lead_guard.build_escalation_settings
        # at spawn). It cannot derive it: Claude Code's payload carries the CLAUDE session id, while
        # relay files an executor's state under its relay NAME, and nothing in the payload maps one
        # to the other. Deriving it from payload["session_id"] is exactly the bug that kept this
        # hook from EVER firing in production — the lookup missed, so it concluded "not a relay
        # executor" and exited silently every time. Payload id is kept only as a last-resort
        # fallback for a settings file written before names were passed.
        sid = sys.argv[1] if len(sys.argv) > 1 else payload.get("session_id")
        if not sid:
            sys.exit(0)

        who = _whoami(sid)
        if not who:
            sys.exit(0)  # not a relay executor session → silent, zero impact

        # --when-idle queue (#18): this Stop IS the idle transition a queued packet waits for, so
        # deliver here — the PRIMARY trigger, and the reason #18 needs no poller of its own (a
        # lead's `relay check` is only the net). Deliberately ahead of the escalation kill-switch
        # and the once-per-packet gate below: queue delivery is a separate feature from the wake
        # push and must not inherit its gating. Cheap file check first so the common no-queue Stop
        # doesn't pay for a subprocess. `relay` decides whether the session is actually idle enough
        # (deliver_queued re-checks); a failure here is swallowed, per this hook's HARD RULE.
        _deliver_queued(sid)

        cfg = lg.load_config(STATE_ROOT)
        if not cfg.get("executor_escalation", True):
            sys.exit(0)  # kill-switch

        n = int(who.get("current_packet", 1))
        if not who.get("report_exists"):
            sys.exit(0)  # idle mid-work, nothing written yet → nothing to push

        if str(n) in lg.load_escalation(STATE_ROOT, sid):
            sys.exit(0)  # already handled this packet — once-per-packet gate

        owner_lead = who.get("owner_lead")
        decision = lg.escalation_decision(STATE_ROOT, sid, n, owner_lead)

        if decision == "resolved":
            # The lead's own fast-path already surfaced this — the dedup working, not a dead hook.
            lg.append_ledger(STATE_ROOT, "escalation_resolved", session_id=sid, packet=n,
                              owner_lead=owner_lead)
            _mark(lg, sid, n, "resolved")
            sys.exit(0)

        if decision in ("unowned", "owner-missing"):
            reason = ("it has no owning lead to notice it" if decision == "unowned" else
                      "its owning lead's session is gone (crashed, closed, or pruned)")
            _notify_human(lg, cfg, sid, n, owner_lead, reason)
            _mark(lg, sid, n, "notified")
            sys.exit(0)

        # decision == "send"
        ok = _push_to_lead(owner_lead, sid, n)
        _mark(lg, sid, n, "sent" if ok else "failed")
        if not ok:
            lg.append_ledger(STATE_ROOT, "escalation_push_failed", session_id=sid, packet=n,
                              owner_lead=owner_lead)
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # hard fail-open


if __name__ == "__main__":
    main()
