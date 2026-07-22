#!/usr/bin/env python3
"""
PreToolUse hook (matcher Bash): in a /relay:mode LEAD session only, classify a Bash command against
the d1 custody-vs-implementation verb taxonomy (docs/post-0.3.27-backlog.md §10) and ledger a
`would_have_blocked` event for an implementation-verb match. LOGGING ONLY (dry-run-first, §10's
"Fable punchlist item 2") — this hook NEVER denies; it always allows, whether or not it logs.
Blocking mode ships later, once the allowlist is tuned against real lead-day logs. Everywhere else
(non-lead sessions, executor sessions, kill-switch off) it fast-exits and allows at zero cost.

Contract: read the hook payload from stdin; this hook NEVER prints a deny decision — it either
prints nothing (allow) or, best-effort, appends a ledger record. HARD RULE: any error, missing
file, unparseable payload, or unexpected shape → exit 0 (allow). A broken hook must never brick
normal Claude Code usage.
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
        # Not a lead session → the entire zero-impact path, same fast-exit as the edit gate.
        if not sid or not lg.is_lead(STATE_ROOT, sid):
            sys.exit(0)

        cfg = lg.load_config(STATE_ROOT)
        if not cfg.get("bash_gate_logging", True):
            sys.exit(0)  # kill-switch off → silent, no ledger writes

        tool_input = payload.get("tool_input", {}) or {}
        command = tool_input.get("command", "")

        rule_name = lg.classify_bash_command(command)
        if rule_name is None:
            sys.exit(0)  # custody verb, or unclassified → free-pass, nothing to log

        lg.append_ledger(STATE_ROOT, "would_have_blocked", session_id=sid, command=command,
                         rule=rule_name)
        sys.exit(0)  # logging-only: always allow, even on a matched implementation verb
    except Exception:
        sys.exit(0)  # hard fail-open


if __name__ == "__main__":
    main()
