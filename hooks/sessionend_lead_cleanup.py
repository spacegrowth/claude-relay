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

# SessionEnd reasons split by what happens to the CONVERSATION, not by "did the session stop"
# (docs/lead-arming-durability.md §4). The old code lumped all four together and deleted the marker,
# which treated a resumable pause as a death — the resumed session came back silently unarmed.
#
#   clear/logout          → the conversation is genuinely gone. A revived lead would be armed with a
#                           model that has no idea it's a lead, which is worse than unarmed. HARD CLEAR.
#   exit/prompt_input_exit → RESUMABLE: `--resume` restores the same session_id and the full
#                           conversation (verified — that doc's §7). A pause, not a death. TOMBSTONE.
#
# Any other reason (e.g. "other", which is what headless `claude -p` produces) → touch nothing,
# same fail-safe-in-favour-of-staying-armed policy as before.
HARD_CLEAR_REASONS = {"clear", "logout"}
PAUSE_REASONS = {"exit", "prompt_input_exit"}


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

        if sid and reason in HARD_CLEAR_REASONS:
            lg.clear_lead(STATE_ROOT, sid)  # no-op if the subtree doesn't exist
        elif sid and reason in PAUSE_REASONS:
            # Resumable: keep the identity, drop the arming. SessionStart(source="resume") revives it.
            if lg.tombstone_lead(STATE_ROOT, sid):
                lg.append_ledger(STATE_ROOT, "lead_tombstoned", session_id=sid, reason=reason)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
