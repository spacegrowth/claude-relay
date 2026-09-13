#!/usr/bin/env python3
"""
PreToolUse hook (matcher Bash): in a /relay:mode LEAD session only, two independent checks.

1. WRITE GATE (backlog row 59, `bash_write_gate` = "deny" | "log" | "off", default "log"): parse
   the command for file-WRITING shapes (redirects `> file` / `>>`, `tee`, `sed -i`, `cp`/`mv`
   destinations, `python - <<EOF` / `python -c` bodies that `open(<literal>, "w")`) via
   lib/bash_writes.py, keep only targets inside the session cwd that are tracked by git (or new
   files under a tracked dir) and not exempt (packets, `~/.relay-tasks/**`, `_staging/`), and apply
   the Edit gate's rule: new file → gated; known line count (a feeding heredoc's body) ≥
   `edit_line_threshold` → gated; unknown count on an existing file → allowed. The `/relay:route
   retain` grace window lets everything through. "deny" prints the same deny JSON shape
   pretool_route_guard.py prints and ledgers `blocked`; "log" ledgers `would_have_blocked` with
   `vector: "bash"` and allows; "off" skips the check. Unknown shapes always allow.

2. VERB LOG (task d1, `bash_gate_logging`): classify against the custody-vs-implementation verb
   taxonomy (docs/post-0.3.27-backlog.md §10) and ledger `would_have_blocked` for an
   implementation-verb match. LOGGING ONLY — never denies. Skipped when check 1 already logged
   the same command (one record per command; that record carries the verb `rule` too).

Everywhere else (non-lead sessions, executor sessions) it fast-exits and allows at zero cost.

Contract: read the hook payload from stdin; to block, print a permissionDecision:"deny" JSON on
stdout and exit 0; otherwise print nothing and exit 0. HARD RULE: any error, missing file,
unparseable payload, or unexpected shape → exit 0 (allow). A broken hook must never brick normal
Claude Code usage.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")

WRITE_GATE_MODES = ("deny", "log", "off")


def _write_gate_mode(cfg):
    mode = cfg.get("bash_write_gate", "log")
    return mode if mode in WRITE_GATE_MODES else "log"  # a typo never silently disables or denies


def _deny_reason(path, lines, new_file):
    parts = []
    if lines is not None:
        parts.append("%d lines" % lines)
    if new_file:
        parts.append("new file")
    size_desc = ", ".join(parts) or "unknown size"
    return (
        "Lead mode: this Bash command writes %s (%s), large enough that it should be delegated "
        "rather than done by the lead. Either /relay:spawn or /relay:send to route it to an "
        "executor, or if this is genuinely lead-appropriate work, /relay:route retain \"<reason>\" "
        "and retry within the grace window."
    ) % (path, size_desc)


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
        tool_input = payload.get("tool_input", {}) or {}
        command = tool_input.get("command", "")

        rule_name = None
        try:
            rule_name = lg.classify_bash_command(command)
        except Exception:
            rule_name = None

        wrote_record = False
        mode = _write_gate_mode(cfg)
        if mode != "off" and isinstance(command, str) and command \
                and not lg.in_grace(STATE_ROOT, sid):
            gated = []
            try:
                import bash_writes as bw
                cwd = payload.get("cwd") or os.getcwd()
                gated = bw.gated_targets(command, cwd, STATE_ROOT, cfg, lg.is_gate_exempt)
            except Exception:
                gated = []  # parse/resolve failure → allow
            if gated:
                path, lines, new_file = gated[0]
                if mode == "deny":
                    # The Edit gate's record shape plus `vector` (lines unknown → 0, its fail-open value).
                    lg.append_ledger(STATE_ROOT, "blocked", session_id=sid, file_path=path,
                                     lines=lines if lines is not None else 0, new_file=new_file,
                                     vector="bash")
                    print(json.dumps({
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": _deny_reason(path, lines, new_file),
                        }
                    }))
                    sys.exit(0)
                lg.append_ledger(STATE_ROOT, "would_have_blocked", session_id=sid, command=command,
                                 rule=rule_name or "bash-write", vector="bash", file_path=path,
                                 lines=lines, new_file=new_file)
                wrote_record = True

        if wrote_record or not cfg.get("bash_gate_logging", True):
            sys.exit(0)  # already logged this command, or verb-log kill-switch off

        if rule_name is None:
            sys.exit(0)  # custody verb, or unclassified → free-pass, nothing to log

        lg.append_ledger(STATE_ROOT, "would_have_blocked", session_id=sid, command=command,
                         rule=rule_name)
        sys.exit(0)  # verb log is logging-only: always allow
    except Exception:
        sys.exit(0)  # hard fail-open


if __name__ == "__main__":
    main()
