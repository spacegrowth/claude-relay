#!/usr/bin/env python3
"""
UserPromptSubmit hook: in a /relay:mode LEAD session only, stamp `state: busy` + `state_since` on
turn start — the busy half of the lead's busy/idle turn-state (wake-watch design §4.2), symmetric
to how executors already have one. Paired with the Stop hook's `state: idle` stamp at turn-end
(hooks/stop_lead_watch.py). Consumed by lead_guard.lead_turn_state(), which a future executor-side
watcher uses to decide whether it's safe to `relay nudge-lead` this session.

Contract: read the hook payload from stdin; always exit 0 (this hook never blocks or alters the
turn). HARD RULE: any error → exit 0 (fail open) — a bug in state-stamping must never brick a
lead's turn.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        import lead_guard as lg
        sid = payload.get("session_id")
        if not sid or not lg.is_lead(STATE_ROOT, sid):
            sys.exit(0)  # not a lead session → silent, zero impact
        lg.stamp_lead_state(STATE_ROOT, sid, "busy")
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
