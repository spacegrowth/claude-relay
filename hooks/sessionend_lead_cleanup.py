#!/usr/bin/env python3
"""
SessionEnd hook: if this session was a /relay:mode lead, remove its lead/<sid>/ state subtree.
Mirrors the session-bridge plugin's cleanup-on-SessionEnd pattern. Nothing to archive — routing
events (retained/blocked) already live durably in the shared ~/.relay-tasks/sessions.jsonl ledger.
Best-effort and silent for every non-lead session. HARD RULE: never throw, never block session end.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")


def main():
    try:
        payload = json.load(sys.stdin)
        import lead_guard as lg
        sid = payload.get("session_id")
        if sid:
            lg.clear_lead(STATE_ROOT, sid)  # no-op if the subtree doesn't exist
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
