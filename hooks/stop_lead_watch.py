#!/usr/bin/env python3
"""
Stop hook, asyncRewake: in a /relay:mode LEAD session only, wake the idle lead when there's new
activity it would otherwise miss, and ANNOUNCE-AND-WAIT (never auto-act). Two things it surfaces:

  App 1 — an executor finished (its NNN-report.md appeared). The executor usually finishes AFTER
          the lead has gone idle, so this hook runs its check as a BACKGROUND POLL (async): when
          the lead stops with a busy executor still in flight, it watches the report paths and
          exits 2 the moment one lands. (A one-shot check at stop time would miss a later report
          and never fire again, since an idle session emits no further Stop events.)
  App 2 — the lead made NEW commit(s) this turn (covers the Bash/`git commit` vector the PreToolUse
          edit-gate can't see). These already exist at stop time → checked synchronously, instantly.

Contract (proven by the asyncRewake spike — see docs/async-rewake-findings.md): runs in the
background; exit 0 → silent, lead stays idle; exit 2 → the idle lead WAKES with this script's
stderr + the hook's rewakeMessage. Gated to fire ONCE per event (surfaced markers, advancing the
last-seen git HEAD, and a single-poller lock). HARD RULE: any error → exit 0 (fail open, never
brick normal usage).
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "scripts"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")
RELAY_BIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "bin", "relay")


def _notify(cfg, message, project=None, executor=None, lead_sid=None, iterm_session=None):
    """Desktop notification, three tiers, first one that applies wins:

    1. iTerm native (OSC 777, written straight to the lead's own tty) — zero external deps, and
       clicking it focuses the POSTING session natively (confirmed live — see
       docs/async-rewake-findings.md). Used whenever the lead's marker recorded an iterm_session
       AND iterm.tty_by_id can still resolve it to a live tty; RETURNs regardless of whether the
       write itself succeeds (notify_via_tty is best-effort/never-raises — that's the point of a
       tier system, not something to retry with a fallback).
    2. terminal-notifier — still valuable even with tier 1 available: `-group` coalesces repeated
       pings per lead (replace rather than stack), and `-execute` runs `relay focus <lead>` so it
       works even if the lead's tty is gone (session moved, iTerm restarted).
    3. osascript's built-in `display notification` — same info, NOT clickable, no coalescing.

    Title names the project, subtitle/body names the executor. Configurable via notify_on_wake;
    failures swallowed throughout."""
    if not cfg.get("notify_on_wake", True):
        return
    if os.environ.get("RELAY_NO_NOTIFY"):
        return  # kill-switch: the test suite sets this so its subprocess hook runs don't fire REAL
                #             desktop banners (neither notifier path has a dry-run). Also usable in CI.
    import lead_guard as lg
    title = f"relay · {project}" if project else "relay — review needed"
    subtitle = f"{executor} reported" if executor else "review needed"
    if iterm_session:
        try:
            import iterm
            tty = iterm.tty_by_id(iterm_session)
            if tty:
                iterm.notify_via_tty(tty, title, subtitle + " — " + message[:180])
                return
        except Exception:
            pass  # fall through to tier 2 — tty_by_id shells out to osascript, which can misbehave
    tn = lg.find_terminal_notifier()  # PATH-robust — a bare `which` fails in the hook's minimal PATH
    try:
        if tn:
            args = [tn, "-title", title, "-subtitle", subtitle, "-message", message[:200], "-sound", "Glass"]
            if lead_sid:
                args += ["-group", f"relay-{lead_sid}",
                         "-execute", f"'{RELAY_BIN}' focus {lead_sid}"]  # click → jump to the lead tab
            subprocess.run(args, capture_output=True, timeout=5)
        else:
            # FALLBACK: no terminal-notifier → macOS's built-in banner via osascript. Same
            # information (project in the title, executor in the message) but degraded: NOT
            # clickable (no way to jump to the lead's tab) and no per-lead coalescing — those two
            # are terminal-notifier-only. The on-screen 🚦 wake is unaffected either way.
            def q(s):
                return (s or "").replace("\\", "\\\\").replace('"', '\\"')
            script = (f'display notification "{q(subtitle + " — " + message[:180])}" '
                      f'with title "{q(title)}" sound name "Glass"')
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


def _announce_and_wake(lg, cfg, sid, lines, surfaced_keys, notify_msg):
    if surfaced_keys:
        lg.mark_surfaced(STATE_ROOT, sid, surfaced_keys)
    # Identify the source so the notification says WHICH project/executor and can click to the lead.
    marker = lg.read_marker(STATE_ROOT, sid)
    project = marker.get("project")
    executor = surfaced_keys[0].split(":")[0] if surfaced_keys else None  # first executor that reported
    _notify(cfg, notify_msg, project=project, executor=executor, lead_sid=sid,
            iterm_session=marker.get("iterm_session"))
    # Emoji-forward banner: the model echoes this into its announcement, so 📥 is a visible,
    # consistent "you have a relay update" marker in the lead's on-screen text.
    sys.stderr.write(
        "🚦 [relay] — review needed: new activity while you were idle:\n"
        + "\n".join(lines)
        + "\n\nOpen your reply with the marker '🚦 [relay] — review needed:', surface these to the "
          "user, and WAIT for their direction. Do NOT auto-review, auto-commit, or otherwise act "
          "on them yourself until the user asks. If a report needs reviewing, tell the user it's "
          "ready and ask whether to review it.\n"
    )
    sys.exit(2)  # wake the idle lead


def _report_brief(path, maxlen=200):
    """The first meaningful line of an executor's report, so the wake shows WHAT happened — not just
    a file path. Heading markers stripped, whitespace collapsed. Best-effort; empty on any error."""
    try:
        with open(path) as f:
            for raw in f:
                line = " ".join(raw.strip().lstrip("#").strip().split())
                if line:
                    return line[:maxlen]
    except Exception:
        pass
    return ""


def _report_lines(lg, sid):
    """(display lines, surfaced keys) for executor reports this lead hasn't been told about. Each
    line carries a BRIEF of the report (its first line) so you know what happened at a glance."""
    lines, keys = [], []
    for key, exsid, packet, path in lg.new_reports_for(STATE_ROOT, sid):
        brief = _report_brief(path)
        head = f"  ✅ executor '{exsid}' reported (packet {packet:03d})"
        lines.append(f"{head} — {brief}\n       report: {path}" if brief
                     else f"{head} — report at {path}")
        keys.append(key)
    return lines, keys


def _notify_summary(lines):
    """A one-line, emoji-stripped summary for the macOS notification banner."""
    for ln in lines:
        t = ln.strip().lstrip("✅📝 ").strip()
        if t:
            return t
    return "new relay activity — review when ready"


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
        # Heartbeat: every lead turn refreshes last_active so `relay list` reflects real liveness.
        try:
            lg.touch_lead(STATE_ROOT, sid)
        except Exception:
            pass

        cfg = lg.load_config(STATE_ROOT)

        # Synchronous announce of what's ALREADY visible at stop time — SKIPPED on the post-wake
        # re-run (stop_hook_active) to avoid a tight re-announce loop. Crucially we then fall THROUGH
        # to arm the background watcher below even when stop_hook_active, so a report that lands while
        # the lead sits idle awaiting your answer is still caught (this used to early-exit and miss
        # it — a #2-reported-then-#3-reported-while-you-decide bug). mark_surfaced dedups either way.
        if not payload.get("stop_hook_active"):
            lines, surfaced_keys = [], []
            cwd = payload.get("cwd") or os.getcwd()
            prev_head = lg.read_head(STATE_ROOT, sid)
            cur_head = lg.git_head(cwd)
            if cfg.get("surface_commits", False):  # App 2 — default OFF (see LEAD_DEFAULTS)
                commits = lg.new_commits(cwd, prev_head)
                if commits:
                    lines.append(f"  \U0001f4dd you made {len(commits)} commit(s) this turn in {cwd}:")
                    lines += [f"       {c}" for c in commits]
            if cur_head and cur_head != prev_head:
                lg.write_head(STATE_ROOT, sid, cur_head)  # surface each commit once
            if cfg.get("auto_wake", True):  # App 1 fast path — a report already exists at stop time
                rlines, rkeys = _report_lines(lg, sid)
                lines += rlines
                surfaced_keys += rkeys
            if lines:
                _announce_and_wake(lg, cfg, sid, lines, surfaced_keys, _notify_summary(lines))  # exits 2

        # Nothing instant. If an executor is still busy, become a BACKGROUND poller that waits for
        # its report and wakes when it lands. One poller per lead (lock); a later Stop while it runs
        # just exits 0.
        if not (cfg.get("auto_wake", True) and lg.has_inflight_executors(STATE_ROOT, sid)):
            sys.exit(0)
        if not lg.acquire_poll_lock(STATE_ROOT, sid):
            sys.exit(0)  # a poller is already watching
        try:
            deadline = time.time() + max(1, int(cfg.get("poll_seconds", 1800)))
            interval = max(1, int(cfg.get("poll_interval", 5)))
            while time.time() < deadline:
                time.sleep(interval)
                if not lg.is_lead(STATE_ROOT, sid):
                    sys.exit(0)  # lead stepped down / session ended while we waited
                rlines, rkeys = _report_lines(lg, sid)
                if rlines:
                    _announce_and_wake(lg, cfg, sid, rlines, rkeys, _notify_summary(rlines))  # exits 2
                if not lg.has_inflight_executors(STATE_ROOT, sid):
                    sys.exit(0)  # nothing left of OURS in flight → stop waiting
            sys.exit(0)  # timed out; a later lead turn will re-arm
        finally:
            lg.release_poll_lock(STATE_ROOT, sid)
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # hard fail-open


if __name__ == "__main__":
    main()
