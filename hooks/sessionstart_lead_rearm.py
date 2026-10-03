#!/usr/bin/env python3
"""
SessionStart hook: re-arm a lead whose session was paused and resumed.

THE PROBLEM (docs/lead-arming-durability.md): Claude Code sessions are resumable — `--resume`
restores the same session_id AND the full conversation — but a routine quit (`prompt_input_exit`)
used to DELETE the lead marker. The resumed session came back silently unarmed: routing gate off,
wake structurally impossible (every hook fast-exits on `is_lead`), ownership broken for anything
spawned afterwards. Worse, the model still believed it was the lead, because its context said so.
Nothing reconciled the two, and nothing said a word.

THE FIX, in two halves: SessionEnd tombstones instead of deleting on a resumable exit
(hooks/sessionend_lead_cleanup.py), and this hook revives the tombstone on the way back in. Together
they form a closed state machine keyed on the two events' own fields:

    SessionEnd(clear|logout)          → hard clear      (conversation genuinely gone)
    SessionEnd(exit|prompt_input_exit) → tombstone       (a pause, not a death)
    SessionStart(resume)               → REVIVE          (this hook)
    SessionStart(clear)                → hard clear      (context wiped; do not resurrect)
    SessionStart(startup|compact)      → no-op

`source` values are spiked and verified on this build, not taken from docs — including `compact`,
which fires SessionStart but NEVER SessionEnd, so it can't unarm anything. This hook still runs on
every compaction, so it must stay cheap and explicitly no-op there rather than relying on the
absence of a tombstone.

HARD RULE: any error → exit 0 (fail open). A bug here must never block a session from starting.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")

REVIVE_SOURCES = {"resume"}
HARD_CLEAR_SOURCES = {"clear"}


def _notify_rearm(lg, sid, marker):
    """Desktop banner on re-arm — reuses stop_lead_watch's existing two-tier `_notify` (iTerm
    OSC 777 → osascript) rather than inventing a second notification path.

    This is the ONLY channel that reaches the human here: a SessionStart hook's stdout goes to the
    model as session context, and its stderr goes nowhere at all. Honours the same `notify_on_wake`
    config and `RELAY_NO_NOTIFY` kill-switch as every other relay notification. Never raises."""
    try:
        hooks_dir = os.path.dirname(os.path.realpath(__file__))
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import stop_lead_watch as slw
        project = marker.get("project") or "?"
        slw._notify(
            lg.load_config(STATE_ROOT),
            f"lead mode restored for '{project}' — gate and auto-wake are active again",
            project=project, lead_sid=sid, iterm_session=marker.get("iterm_session"),
            subtitle="lead re-armed on resume",
        )
    except Exception:
        pass


def main():
    # THE INCIDENT (2026-09-05 22:18:50): a headless `claude -p` relay itself launches (a model-
    # alias probe, `relay doctor`, spawn's model-cache seed) runs from the LEAD's own shell and
    # inherits its $TERM_SESSION_ID/cwd — its own SessionStart hook then matched the live lead's
    # tab and migrated the marker onto the throwaway probe, tombstoning the real lead. Relay stamps
    # RELAY_HEADLESS=1 on every such launch (bin/relay's `_headless_env`); every one of this
    # plugin's hooks returns immediately on it, before even reading the payload.
    if os.environ.get("RELAY_HEADLESS") == "1":
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        import lead_guard as lg
        sid = payload.get("session_id")
        source = payload.get("source")
        if not sid:
            sys.exit(0)

        if source in HARD_CLEAR_SOURCES:
            # /clear wipes the conversation: the model returns with no lead context, so a marker or
            # tombstone left behind would be actively wrong. Drop it.
            if lg.read_marker(STATE_ROOT, sid):
                lg.clear_lead(STATE_ROOT, sid)
                lg.append_ledger(STATE_ROOT, "lead_cleared_on_start", session_id=sid, source=source)
            sys.exit(0)

        if source in REVIVE_SOURCES:
            # Lossless: revive_lead only drops the tombstone flags and refreshes last_active, so the
            # project name, cwd, iterm_session, colour and predecessor all come back untouched — a
            # resumed lead is indistinguishable from one that never exited.
            revived = lg.revive_lead(STATE_ROOT, sid)
            id_change_suffix = ""
            if revived:
                lg.append_ledger(STATE_ROOT, "lead_rearmed", session_id=sid, source=source)
            elif not lg.read_marker(STATE_ROOT, sid) and payload.get("cwd"):
                # relay has NO marker for this id at all — not even a tombstone. THE INCIDENT (memory:
                # relay-lead-id-changes-on-resume.md): this resume's own $CLAUDE_CODE_SESSION_ID came
                # back different from the one its lead armed under, while the iTerm TAB is unchanged.
                # Look for whichever lead marker still claims THIS tab (same identity lead-start itself
                # records — lg.env_tab_id(): $TERM_SESSION_ID, or the "tmux:%N" pane handle under
                # tmux — compared by exact string equality, no
                # subprocess needed) AND the same project directory (fix-list 002: a shared tab alone
                # isn't proof — a brand-new unrelated session started later in that tab must not
                # inherit an old lead; a payload with no cwd can't prove same-project either way, so
                # the `and payload.get("cwd")` above skips the lookup entirely) and migrate it forward
                # rather than leaving it silently orphaned. `safe_migrate_by_tab` refuses when that
                # old marker still looks like a LIVE lead (lead_guard.lead_still_live) — THE
                # INCIDENT above (2026-09-05 22:18:50) is this exact lookup matching a still-running
                # lead's tab from one of relay's own headless probes, not a genuine resume.
                poll_seconds = lg.load_config(STATE_ROOT).get(
                    "poll_seconds", lg.LEAD_DEFAULTS["poll_seconds"])
                old_sid = lg.safe_migrate_by_tab(STATE_ROOT, lg.env_tab_id(),
                                                 payload.get("cwd"), sid, poll_seconds)
                if old_sid:
                    revived = True
                    lg.append_ledger(STATE_ROOT, "lead_rearmed", session_id=sid, source=source,
                                     migrated_from=old_sid)
                    id_change_suffix = f" (session id changed: {old_sid[:8]} → {sid[:8]})"
            if revived:
                marker = lg.read_marker(STATE_ROOT, sid)
                # Loudness is the point: the original defect was that unarming happened in silence.
                # This MUST be stdout — a SessionStart hook's stdout is surfaced as session context;
                # its stderr goes nowhere the user will see. (Learned the hard way: the first cut
                # wrote to stderr, the re-arm worked perfectly and reported itself to no one.)
                sys.stdout.write(
                    f"🚦 [relay] — lead mode restored for this resumed session "
                    f"(project '{marker.get('project') or '?'}'). Gate and auto-wake are active "
                    f"again.{id_change_suffix}\n"
                )
                # ...but stdout only reaches the MODEL (it becomes session context). Nothing a
                # SessionStart hook writes lands on the user's screen. So also fire the desktop
                # notification — the one channel that reaches a human, works with no statusline
                # configured, and doesn't risk garbling the live TUI the way writing to the tty
                # would. Best-effort; a notification failure must never affect arming.
                _notify_rearm(lg, sid, marker)
        # startup / compact / anything else → nothing to do.
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
