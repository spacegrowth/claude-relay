#!/usr/bin/env python3
"""
SessionEnd hook: if this session was a /relay:mode lead, conditionally remove its lead/<sid>/ state
subtree based on the SessionEnd reason. This prevents accidental lead unarming on plugin-reload churn.
Mirrors the session-bridge plugin's cleanup-on-SessionEnd pattern. Nothing to archive — routing
events (retained/blocked) already live durably in the shared ~/.relay-tasks/sessions.jsonl ledger.
Best-effort and silent. HARD RULE: never throw, never block session end.

INCIDENT (2026-07-10): during a plugin reload sequence, an armed lead's entire lead/<sid>/ dir
vanished without the session ending. This hook was unconditionally calling clear_lead on any
SessionEnd payload, regardless of reason. POLICY: only clear lead state on documented real-end
reasons; unknown/missing reasons preserve the marker (fail-safe in favor of staying armed). Every
SessionEnd is logged to the ledger with its reason for future incident attribution.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")

# Conservative set of SessionEnd reasons that indicate a REAL session end (Claude Code's documented
# reasons). Any other reason → preserve the lead marker (fail-safe).
REAL_END_REASONS = {"clear", "logout", "prompt_input_exit", "exit"}


def main():
    try:
        payload = json.load(sys.stdin)
        import lead_guard as lg
        sid = payload.get("session_id")
        reason = payload.get("reason")

        # Always log to ledger for observability
        if sid:
            was_lead = lg.is_lead(STATE_ROOT, sid)
            lg.append_ledger(STATE_ROOT, "session_end", session_id=sid, reason=reason, was_lead=was_lead)

        # Clear lead state ONLY on documented real-end reasons
        if sid and reason in REAL_END_REASONS:
            lg.clear_lead(STATE_ROOT, sid)  # no-op if the subtree doesn't exist
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
