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
                                         — UNLESS an unexpired `relay handoff --here` rotation
                                         marker for this tmux pane names this pane's lead: then
                                         ARM THE NEW SESSION ID as its successor (in place)
    SessionStart(startup|compact)      → no-op

`source` values are spiked and verified on this build, not taken from docs — including `compact`,
which fires SessionStart but NEVER SessionEnd, so it can't unarm anything. This hook still runs on
every compaction, so it must stay cheap and explicitly no-op there rather than relying on the
absence of a tombstone.

HARD RULE: any error → exit 0 (fail open). A bug here must never block a session from starting.
"""
import json
import os
import re
import shutil
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


def _rotate_in_place(lg, sid):
    """`relay handoff --here` (tmux only): arm THIS new session id as the in-place successor of the
    lead whose /clear just happened in this same pane. Returns True iff it armed the successor (the
    caller then skips the hard clear); False when there is no rotation to honour, in which case the
    caller's existing hard-clear path runs unchanged.

    Honoured only when ALL hold: $TMUX_PANE is a pane id; `lead/rotation-pending/<pane>.json`
    exists, is unexpired (lead_guard.rotation_marker_valid) and names a predecessor; that
    predecessor's marker exists, is not this session, has not already been superseded, and was
    armed in THIS pane (`iterm_session == "tmux:<pane>"`) — i.e. it is the pane's last lead. A
    marker that is present but fails any check is stale and is deleted (ledger `rotate_here_stale`).

    On success: the successor marker carries the predecessor's project, tab_label, color, model,
    cwd, tty, lineage_started, backend=tmux and pane handle (posture fields reset from config
    exactly like a fresh arm); the predecessor's executors are re-parented through the SAME
    lead_guard.reparent_executors bin/relay's `_reparent_executors` uses (per-executor seen-report
    stamps carried); its whole surfaced/pending set, plan.json and handoff memo copy move over as
    `relay handoff` does; the predecessor is tombstoned (if SessionEnd didn't already) and stamped
    `superseded_by`/`migrated_to`; the rotation marker is consumed (deleted); and the hook's stdout
    carries `hookSpecificOutput.additionalContext` naming the memo. Raises on unexpected errors —
    main() turns that into the ordinary hard clear (fail open)."""
    pane = os.environ.get("TMUX_PANE", "")
    if not re.match(r"^%\d+$", pane):
        return False
    rm_path = lg.rotation_marker_path(STATE_ROOT, pane)
    if not rm_path.exists():
        return False
    rm = lg.read_rotation_marker(STATE_ROOT, pane)
    pred_sid = rm.get("predecessor")
    handle = "tmux:" + pane
    pred = lg.read_marker(STATE_ROOT, pred_sid) if pred_sid else {}
    why = None
    if not lg.rotation_marker_valid(rm):
        why = "expired or malformed"
    elif pred_sid == sid:
        why = "predecessor is this session"
    elif not pred:
        why = "predecessor marker missing"
    elif pred.get("superseded_by") or pred.get("migrated_to"):
        why = "predecessor already superseded"
    elif pred.get("iterm_session") != handle:
        why = "predecessor was not this pane's lead"
    if why:
        lg.delete_rotation_marker(STATE_ROOT, pane)
        lg.append_ledger(STATE_ROOT, "rotate_here_stale", session_id=sid, pane=pane,
                         predecessor=pred_sid, reason=why)
        return False

    cfg = lg.load_config(STATE_ROOT)
    project = rm.get("project") or pred.get("project")
    cwd = pred.get("cwd") or rm.get("cwd")
    lg.write_marker(STATE_ROOT, sid, model=rm.get("model") or pred.get("model"),
                    iterm_session=handle, project=project, cwd=cwd,
                    tab_label=rm.get("tab_label") or pred.get("tab_label"),
                    color=rm.get("color") if "color" in rm else pred.get("color"),
                    tty=pred.get("tty"), plugin_version=pred.get("plugin_version"),
                    stop_hook_timeout=pred.get("stop_hook_timeout"),
                    lineage_started=(rm.get("lineage_started") or pred.get("lineage_started")
                                     or pred.get("started")),
                    backend="tmux", autonomous=cfg.get("autonomous_mode", False),
                    autonomous_source="config")
    lg.update_marker(STATE_ROOT, sid, rotated_from=pred_sid, rotated_at=lg.now())

    moved = lg.reparent_executors(STATE_ROOT, pred_sid, sid, project)
    lg.carry_forward_surfaced(STATE_ROOT, pred_sid, sid)
    succ_dir = lg.lead_dir(STATE_ROOT, sid)
    pred_dir = lg.lead_dir(STATE_ROOT, pred_sid)
    try:
        if (pred_dir / "plan.json").is_file():
            shutil.copyfile(pred_dir / "plan.json", succ_dir / "plan.json")
    except Exception:
        pass
    try:
        lg.write_head(STATE_ROOT, sid, lg.git_head(cwd))
    except Exception:
        pass
    memo = rm.get("memo_copy")
    try:
        if memo and os.path.isfile(memo):
            shutil.copyfile(memo, succ_dir / "handoff.md")
            os.unlink(memo)
            memo = str(succ_dir / "handoff.md")
    except Exception:
        pass

    lg.tombstone_lead(STATE_ROOT, pred_sid, reason="rotated_in_place", notify=False)
    # SHORTCUT: the predecessor stays on disk as a superseded tombstone (audit trail; hidden from
    # list/board/project-resolve, excluded from tab matching via migrated_to). `relay prune` spares
    # paused leads, so these accumulate one per rotation; fine at a few per lead-day. Upgrade path:
    # teach prune to delete tombstones carrying `superseded_by` past its age cutoff.
    lg.update_marker(STATE_ROOT, pred_sid, superseded_by=sid, migrated_to=sid)
    lg.delete_rotation_marker(STATE_ROOT, pane)
    lg.append_ledger(STATE_ROOT, "lead_rotation_armed", session_id=sid, predecessor=pred_sid,
                     pane=pane, executors=moved)
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": (
            f"🚦 [relay] — you are the successor lead for project '{project or '?'}', rotated in "
            f"place in this same tmux pane (relay handoff --here). Lead mode is armed: gate and "
            f"auto-wake are active, and your predecessor's executors are yours. Read {memo} "
            f"before anything else, then run /relay:list and continue."),
    }}) + "\n")
    return True


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
            # `relay handoff --here`: a /clear relay itself typed to rotate this pane's lead in
            # place. Fail open — any error here falls through to the ordinary hard clear below.
            try:
                if _rotate_in_place(lg, sid):
                    sys.exit(0)
            except SystemExit:
                raise
            except Exception:
                try:
                    lg.delete_rotation_marker(STATE_ROOT, os.environ.get("TMUX_PANE", ""))
                except Exception:
                    pass
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
