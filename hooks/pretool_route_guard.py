#!/usr/bin/env python3
"""
PreToolUse hook (matcher Edit|Write|MultiEdit): in a /relay:mode LEAD session only, block a single
inline edit that's large enough it should have been delegated — BEFORE it lands. Everywhere else
(non-lead sessions, executor sessions, every other project on the machine) it fast-exits and allows.

Contract: read the hook payload from stdin; to block, print a permissionDecision:"deny" JSON on
stdout and exit 0; to allow, just exit 0. HARD RULE: any error, missing file, unparseable payload,
or unexpected shape → exit 0 (allow). A broken hook must never brick normal Claude Code usage.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")


def main():
    try:
        import lead_guard as lg
    except Exception:
        sys.exit(0)  # can't load logic → allow, never block

    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        sid = payload.get("session_id")
        # Not a lead session → the entire zero-impact path. This is what keeps non-lead sessions,
        # executor sessions, and every unrelated project untouched.
        if not sid or not lg.is_lead(STATE_ROOT, sid):
            sys.exit(0)

        # /relay:route retain opened a grace window → let edits through until it expires.
        if lg.in_grace(STATE_ROOT, sid):
            sys.exit(0)

        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {}) or {}

        # Packet files (and anything else under ~/.relay-tasks) are the lead's OWN deliverable —
        # writing them is delegation working as intended, never something to delegate.
        if lg.is_gate_exempt(STATE_ROOT, tool_input.get("file_path")):
            sys.exit(0)

        cfg = lg.load_config(STATE_ROOT)
        lines = lg.edit_line_count(tool_name, tool_input)
        new_file = lg.is_new_file(tool_input)

        if not lg.exceeds_gate(lines, new_file, cfg):
            sys.exit(0)  # small review-class fix → silent allow, never nag

        # Over the gate: record the block (involuntary, hook-side — the genuinely checkable signal)
        # and deny with guidance.
        file_path = tool_input.get("file_path", "?")
        lg.append_ledger(STATE_ROOT, "blocked", session_id=sid, file_path=file_path,
                         lines=lines, new_file=new_file)

        size_desc = "%d lines%s" % (lines, ", new file" if new_file else "")
        reason = (
            "Lead mode: this inline edit is large enough (%s) that it should be delegated rather "
            "than done by the lead. Either /relay:spawn or /relay:send to route it to an executor, "
            "or if this is genuinely lead-appropriate work, /relay:route retain \"<reason>\" and "
            "retry within the grace window. "
            "(Note: file changes made via Bash — git merge/commit, sed, heredocs — are NOT gated "
            "by this; that discipline is still yours to keep.)"
        ) % size_desc

        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }))
        sys.exit(0)
    except Exception:
        sys.exit(0)  # hard fail-open


if __name__ == "__main__":
    main()
