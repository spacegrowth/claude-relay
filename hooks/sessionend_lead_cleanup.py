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
SessionEnd is logged to the ledger with its reason for future incident attribution — but ONLY on a
machine that already has a ~/.relay-tasks (BUG-hooks-3: `append_ledger` creates the state root on
the way, so logging unconditionally meant ending ANY Claude Code session, in ANY project, on a
machine where relay is merely installed, created ~/.relay-tasks/ forever — a zero-impact-promise
violation hooks.json's own description and README.md both state explicitly). A machine relay has
never armed a lead on has no incident to attribute, so nothing here creates the root.

INCIDENT (2026-09-05 20:16): a LIVE lead's marker was tombstoned with no reason recorded and no
ledger event at all — gate, wake and auto posture went dark unannounced. Fixed structurally in
`lead_guard.tombstone_lead` itself (reason stays optional — it does NOT refuse a falsy one — but
every successful tombstone now stores it as `ended_reason` and ledgers `lead_tombstoned` on its
own), not just here — see that function's docstring. This hook
also now ledgers `session_end_ignored` (with the raw payload keys) whenever the reason matches
neither policy below, so "nothing happened" is a statement, not an inference from silence.
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
    # THE INCIDENT (2026-09-05 22:18:50) — see sessionstart_lead_rearm.py's main() for the full
    # account. Every one of this plugin's hooks returns immediately on relay's own
    # RELAY_HEADLESS=1 marker (stamped on every headless `claude -p` relay itself launches),
    # before even reading the payload.
    if os.environ.get("RELAY_HEADLESS") == "1":
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
        import lead_guard as lg
        sid = payload.get("session_id")
        reason = payload.get("reason")

        # Log for observability — but only where relay already lives. Creating the state root from
        # a SessionEnd would touch every project on the machine (hooks.json's own description
        # promises the opposite), and a machine with no ~/.relay-tasks has no incident to attribute.
        if sid and os.path.isdir(STATE_ROOT):
            was_lead = lg.is_lead(STATE_ROOT, sid)
            lg.append_ledger(STATE_ROOT, "session_end", session_id=sid, reason=reason, was_lead=was_lead)

        if sid and reason in HARD_CLEAR_REASONS:
            lg.clear_lead(STATE_ROOT, sid)  # no-op if the subtree doesn't exist
        elif sid and reason in PAUSE_REASONS:
            # Resumable: keep the identity, drop the arming. SessionStart(source="resume") revives
            # it. tombstone_lead itself stores this reason (optional — it does not refuse a falsy
            # one) as ended_reason and ledgers lead_tombstoned on its own — see THE INCIDENT above;
            # this hook no longer has to remember either half.
            lg.tombstone_lead(STATE_ROOT, sid, reason=reason)
        elif sid:
            # THE INCIDENT (lead-found, 2026-09-05 20:16): a missing/unknown reason must NEVER
            # tombstone (unchanged — this branch does nothing to the marker, same fail-safe as
            # before), but it used to leave only the generic session_end line above to infer that
            # from. Say so explicitly, with the raw payload keys, so a genuinely novel SessionEnd
            # shape is investigable instead of indistinguishable from "nothing happened".
            lg.append_ledger(STATE_ROOT, "session_end_ignored", session_id=sid, reason=reason,
                             payload_keys=sorted(payload.keys()))
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
