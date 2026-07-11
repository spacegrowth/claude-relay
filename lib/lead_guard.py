"""
lead_guard — pure state/logic for relay's lead-mode routing gate, shared between bin/relay (the
CLI that sets up lead mode and the /relay:route escape hatch) and the PreToolUse/Stop/SessionEnd
hook scripts under hooks/.

Design notes:
- Every function takes `state_root` explicitly (rather than reading a module global) so it's
  unit-testable against a tmp dir, exactly like bin/relay's STATE_ROOT is patchable in tests.
  Hooks pass the real ~/.relay-tasks; tests pass tmp_path.
- The gate is STATELESS per-edit: there is no accumulator. `edit_line_count`/`exceeds_gate`
  evaluate a SINGLE Edit/Write/MultiEdit call on its own, so a large edit is blocked BEFORE it
  lands rather than after several edits have already happened. See the plan file for why the
  earlier cumulative-accumulator design was rejected.
- Everything here is defensive: malformed input degrades to a safe, fail-OPEN default (0 lines /
  not-a-lead / not-in-grace), never an exception. The hook's hard rule is "any error → allow", and
  keeping the shared logic non-throwing makes that rule easy to honor.
"""
import json
import os
import shutil
import time
from pathlib import Path

# Global routing-gate config. A ~/.relay-tasks/lead/config.json may override any of these keys;
# absent/corrupt file → these exact defaults (never required to exist).
LEAD_DEFAULTS = {
    "edit_line_threshold": 40,   # a single Edit/Write/MultiEdit at/over this many NEW lines is gated
    "block_on_new_file": True,   # creating a brand-new file (Write to a nonexistent path) is gated
    "grace_seconds": 120,        # how long /relay:route retain opens the edit window for
    "auto_wake": True,           # Stop-hook: wake the idle lead when an executor reports (App 1)
    "surface_commits": False,    # Stop-hook App 2: wake to surface commits the lead made this turn.
                                 # OFF by default — waking the lead about its OWN (often user-approved)
                                 # commits reads as a spurious "review needed". Opt in if you want it.
    "poll_seconds": 1800,        # how long the idle lead's background report-watcher waits (App 1)
    "poll_interval": 5,          # how often that watcher re-checks for a report
    "notify_on_wake": True,      # pop a macOS notification when the lead is woken to review
    "notify_via": "auto",        # notification transport. "auto" = iTerm OSC-to-tty first (native
                                 # click→the posting session), then terminal-notifier, then osascript.
                                 # iTerm forces a "Session …"-prefixed banner title on that OSC tier
                                 # (no escape parameter overrides it); "terminal-notifier" SKIPS the
                                 # OSC tier for a clean title/subtitle (falls back to osascript).
    "executor_skip_permissions": False,  # spawn executors with --dangerously-skip-permissions
    "terminal_app": "auto",      # "iterm" | "terminal" | "auto" ($TERM_PROGRAM decides; iTerm default)
    "tab_colors": True,          # iTerm only: color each lead's tab + its executors' tabs alike
    "executor_layout": "tab",    # "tab" | "pane" (pane: iTerm only, split into the lead's window)
}

# Distinguishable, colorblind-tolerant tab colors — brightened so they remain visible when dimmed
# (iTerm dims inactive tabs). Roughly halfway between vivid and previous muted set: clearly saturated
# (distinguishable at a glance), yet calm when active (not carnival). A lead hashes to one; its
# executors inherit it, so with several leads running you can tell which tabs belong together.
TAB_PALETTE = [
    (200, 140, 135),  # brighter coral
    (210, 172, 124),  # brighter amber
    (146, 185, 146),  # brighter green
    (136, 164, 198),  # brighter blue
    (172, 148, 192),  # brighter purple
    (132, 180, 180),  # brighter teal
]


def lead_color(session_id):
    """Stable per-lead RGB from TAB_PALETTE — the same lead always maps to the same color, across
    processes and restarts (sha256, not Python's per-process salted hash). Returns [r, g, b]."""
    import hashlib
    h = int(hashlib.sha256(str(session_id).encode()).hexdigest(), 16)
    return list(TAB_PALETTE[h % len(TAB_PALETTE)])


def pick_lead_color(state_root, session_id):
    """Collision-free lead color: walks TAB_PALETTE forward from lead_color's hash index to find an
    unused color. Re-arm stable: if this lead's marker already claims a CURRENT palette color,
    returns it unchanged. Stale (old-palette) colors fall through to re-pick from current palette.
    Stale colors don't block slots (self-heals as leads re-arm). All 6 current palette slots claimed
    by OTHER leads → falls back to lead_color (acceptable at >6 leads). Fully defensive: any error
    → lead_color fallback. Returns [r, g, b]."""
    try:
        import hashlib
        # Check if this lead already has a marker with a CURRENT-palette color (re-arm stability).
        existing = read_marker(state_root, session_id)
        if existing and isinstance(existing.get("color"), list):
            existing_color = existing.get("color")
            # Only preserve the color if it's still in the current palette; stale colors re-pick.
            if tuple(existing_color) in {tuple(c) for c in TAB_PALETTE}:
                return existing_color

        # Gather colors claimed by OTHER leads (skip this lead's marker).
        claimed = set()
        for lead in list_leads(state_root):
            if lead.get("session_id") == session_id:
                continue  # skip this lead's own marker if it exists
            color = lead.get("color")
            if isinstance(color, list) and len(color) == 3:
                claimed.add(tuple(color))

        # Start from this lead's hash index and walk forward looking for an unused color.
        h = int(hashlib.sha256(str(session_id).encode()).hexdigest(), 16)
        start_idx = h % len(TAB_PALETTE)
        for i in range(len(TAB_PALETTE)):
            idx = (start_idx + i) % len(TAB_PALETTE)
            color = TAB_PALETTE[idx]
            if tuple(color) not in claimed:
                return list(color)

        # All 6 colors in use by other leads → fall back to deterministic hash (acceptable at >6).
        return lead_color(session_id)
    except Exception:
        return lead_color(session_id)


def now():
    """Timestamp in the exact format bin/relay's ledger already uses, so shared-appended events
    are indistinguishable from natively-appended ones."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def find_terminal_notifier():
    """Absolute path to terminal-notifier, or None — PATH-robust. `shutil.which` alone gives FALSE
    negatives in Stop-hook / launchd shells whose PATH lacks Homebrew's bin dir, so also probe the
    standard brew locations. Callers must invoke it by THIS absolute path so it runs regardless of
    the caller's PATH."""
    p = shutil.which("terminal-notifier")
    if p:
        return p
    for cand in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier"):
        if os.access(cand, os.X_OK):
            return cand
    return None


# ---- path helpers -----------------------------------------------------------------------------

def lead_dir(state_root, session_id):
    return Path(state_root) / "lead" / str(session_id)


def marker_path(state_root, session_id):
    return lead_dir(state_root, session_id) / "marker.json"


def grace_path(state_root, session_id):
    return lead_dir(state_root, session_id) / "grace_until"


def config_path(state_root):
    return Path(state_root) / "lead" / "config.json"


# ---- config -----------------------------------------------------------------------------------

def load_config(state_root):
    """Defaults merged with any recognized keys from lead/config.json. Unknown keys ignored;
    missing/corrupt file → pure defaults. Never throws."""
    cfg = dict(LEAD_DEFAULTS)
    try:
        p = config_path(state_root)
        if p.exists():
            user = json.loads(p.read_text())
            if isinstance(user, dict):
                for k in LEAD_DEFAULTS:
                    if k in user:
                        cfg[k] = user[k]
    except Exception:
        pass
    return cfg


# ---- pure edit-sizing logic (unit-tested independent of any I/O) -------------------------------

def _count_lines(s):
    if not s:
        return 0
    return s.count("\n") + 1


def edit_line_count(tool_name, tool_input):
    """Lines of NEW content a single tool call introduces. Write → its content; Edit → new_string;
    MultiEdit → sum of each edit's new_string. Any unexpected shape degrades to 0 (fail-open: an
    unparseable edit is never blocked — under-counting is the safe direction)."""
    try:
        if tool_name == "Write":
            return _count_lines(tool_input.get("content", ""))
        if tool_name == "Edit":
            return _count_lines(tool_input.get("new_string", ""))
        if tool_name == "MultiEdit":
            total = 0
            for e in tool_input.get("edits", []) or []:
                if isinstance(e, dict):
                    total += _count_lines(e.get("new_string", ""))
            return total
    except Exception:
        return 0
    return 0


def is_new_file(tool_input):
    """True if this call targets a path that doesn't exist yet (a brand-new file). Checked BEFORE
    the write happens, which PreToolUse timing guarantees is still valid. In practice only Write
    creates files; Edit/MultiEdit on a nonexistent path fail anyway, so this is harmless there."""
    try:
        fp = tool_input.get("file_path")
        if not fp:
            return False
        return not os.path.exists(fp)
    except Exception:
        return False


def is_gate_exempt(state_root, file_path):
    """Paths the routing gate must never block, because writing them IS the lead's own job:
    anything under the relay state root (packet files, and any other relay bookkeeping the lead
    maintains there), or a packet file by naming convention (*-packet.md) wherever the lead chose
    to draft it. Without this, `block_on_new_file` gates every new packet the lead writes — the
    core delegation workflow would trip its own gate on every spawn/send. Never throws; any error
    → not exempt (the gate's own fail-open contract still applies downstream)."""
    try:
        if not file_path:
            return False
        p = Path(file_path).expanduser()
        try:
            p.resolve().relative_to(Path(state_root).resolve())
            return True
        except ValueError:
            pass
        return p.name.endswith("-packet.md")
    except Exception:
        return False


def exceeds_gate(lines, new_file, config):
    """Whether a single edit trips the gate: too many new lines, or a new file when that's gated."""
    if lines >= config["edit_line_threshold"]:
        return True
    if new_file and config["block_on_new_file"]:
        return True
    return False


# ---- lead-mode state (marker + grace window) --------------------------------------------------

def is_lead(state_root, session_id):
    """The sole 'is this a lead session' test. Marker absent (or any error) → not lead → the hooks
    fast-exit-allow, which is the entire zero-impact path for non-lead/executor sessions."""
    try:
        return marker_path(state_root, session_id).exists()
    except Exception:
        return False


def write_marker(state_root, session_id, model=None, iterm_session=None, project=None, cwd=None,
                 tab_label=None, color=None, plugin_version=None, stop_hook_timeout=None):
    d = lead_dir(state_root, session_id)
    d.mkdir(parents=True, exist_ok=True)
    marker_path(state_root, session_id).write_text(json.dumps({
        "session_id": session_id,
        "project": project,          # human-readable project name (defaults to cwd basename at call site)
        "cwd": cwd,                  # where a restored lead should reopen
        "tab_label": tab_label,      # stable relay-controlled tab title → makes `relay focus <lead>` work
        "color": color,              # [r,g,b] tab color; this lead's executors inherit it at spawn
        "last_active": now(),        # heartbeat — refreshed on every write_marker call
        "started": now(),
        "model": model,
        "iterm_session": iterm_session,  # $TERM_SESSION_ID — recorded tab metadata (debugging)
        # The plugin version this session is bound to, and the Stop-hook timeout that version
        # declares — captured at arm time (bin/relay shares ${CLAUDE_PLUGIN_ROOT} with the hooks, so
        # what it reads IS what will fire). wake_hook_state() reads these back to flag a lead whose
        # wake poller will be killed early (pre-fix hook), so a silently-stale session is VISIBLE in
        # `relay list` rather than only found by forensics after a missed wake.
        "plugin_version": plugin_version,
        "stop_hook_timeout": stop_hook_timeout,
    }, indent=2))


def read_marker(state_root, session_id):
    try:
        p = marker_path(state_root, session_id)
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def wake_hook_state(marker, poll_seconds):
    """Whether this lead's background wake poller will survive its full poll window — 'ok', 'stale',
    or 'unknown' — from the Stop-hook timeout stamped in its marker at arm time.

      'ok'      — stamped timeout present and >= poll_seconds: the harness lets the poller run long
                  enough to catch a late report.
      'stale'   — stamped timeout is None (a 0.1.0-era hook with no timeout field → killed at the
                  harness default, the original missed-wake bug) or below poll_seconds (someone
                  raised poll_seconds past the hook timeout). Get onto the fixed hook: /reload-plugins
                  (re-points hooks — relay has no monitors, so no restart needed) then re-run
                  /relay:mode to re-arm and re-stamp. The stamp only refreshes on re-arm, so an
                  updated-but-not-re-armed lead can still read stale until it re-arms.
      'unknown' — marker predates version stamping (no key at all). Can't prove it's safe; surfaced
                  softly so an old pre-fix lead isn't hidden, without crying wolf over a fresh one.

    Pure and defensive: any bad input degrades to 'stale' (surface, don't hide)."""
    if "stop_hook_timeout" not in marker:
        return "unknown"
    t = marker.get("stop_hook_timeout")
    try:
        return "ok" if (t is not None and int(t) >= int(poll_seconds)) else "stale"
    except Exception:
        return "stale"


def touch_lead(state_root, session_id):
    """Heartbeat: refresh this lead's `last_active` to now(), preserving every other marker field
    (read-modify-write). Called once per lead turn so `relay list`'s last_active reflects real
    liveness — a stale one means the lead probably crashed. Fully defensive: a missing/unreadable/
    non-dict marker is a silent no-op, and nothing here ever raises (the Stop hook's fail-open
    contract must hold even if the heartbeat can't be written)."""
    try:
        m = read_marker(state_root, session_id)
        if not isinstance(m, dict) or not m:
            return  # no marker to touch → nothing to do
        m["last_active"] = now()
        marker_path(state_root, session_id).write_text(json.dumps(m, indent=2))
    except Exception:
        pass


def list_leads(state_root):
    """Every lead marker under <state_root>/lead/*/marker.json, oldest-first by `started`. Each
    item is the marker dict exactly as stored. Fully defensive: config.json and any non-marker
    entry are skipped, an unreadable/malformed marker is skipped, and no input ever raises — this
    is the always-visible LEADS surface, so a single bad marker must never blank the whole list."""
    out = []
    try:
        lead_root = Path(state_root) / "lead"
        if not lead_root.exists():
            return out
        for d in lead_root.iterdir():
            if not d.is_dir():
                continue  # skips lead/config.json and any stray files
            mp = d / "marker.json"
            if not mp.exists():
                continue
            try:
                m = json.loads(mp.read_text())
                if isinstance(m, dict):
                    out.append(m)
            except Exception:
                continue  # a malformed marker is skipped, never fatal
    except Exception:
        return out
    # Sort oldest-first; a marker missing `started` sorts as "" (first) rather than crashing.
    out.sort(key=lambda m: m.get("started") or "")
    return out


def set_grace(state_root, session_id, seconds, now_ts=None):
    """Open an edit grace window (retain escape hatch). Stored as an absolute unix ts so the hook
    just compares against time.time()."""
    if now_ts is None:
        now_ts = time.time()
    d = lead_dir(state_root, session_id)
    d.mkdir(parents=True, exist_ok=True)
    grace_path(state_root, session_id).write_text(str(now_ts + seconds))


def in_grace(state_root, session_id, now_ts=None):
    if now_ts is None:
        now_ts = time.time()
    try:
        gp = grace_path(state_root, session_id)
        if not gp.exists():
            return False
        return now_ts < float(gp.read_text().strip())
    except Exception:
        return False


def clear_lead(state_root, session_id):
    """Remove the whole lead/<sid>/ subtree (step-down or SessionEnd). Best-effort; routing events
    already live durably in the shared sessions.jsonl ledger, so there's nothing here to preserve."""
    try:
        shutil.rmtree(lead_dir(state_root, session_id))
    except Exception:
        pass


# ---- ledger (reuses the EXISTING ~/.relay-tasks/sessions.jsonl, same record shape as bin/relay) -

def append_ledger(state_root, event, **fields):
    """Append one {ts, event, ...} record to the shared sessions.jsonl. Byte-identical shape to
    bin/relay's own append_ledger so route/blocked events sit alongside spawn/send/etc. Best-effort;
    a failed ledger write must never turn into a blocked or errored tool call."""
    try:
        root = Path(state_root)
        root.mkdir(parents=True, exist_ok=True)
        rec = {"ts": now(), "event": event, **fields}
        with open(root / "sessions.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ---- Stop-hook auto-wake: executor reports (App 1) + lead commits (App 2) -----------------------

def executor_reports(state_root):
    """Every executor session that currently has a written report for its current packet, as
    (session_id, packet, report_path). Reads the top-level session dirs' session.json directly
    (the lead's own state lives under lead/<sid>/ with no top-level session.json, so it's naturally
    excluded). Never throws."""
    out = []
    try:
        root = Path(state_root)
        if not root.exists():
            return out
        for d in root.iterdir():
            sj = d / "session.json"
            if not sj.exists():
                continue
            try:
                s = json.loads(sj.read_text())
                n = int(s.get("current_packet", 1))
                rp = d / "packets" / f"{n:03d}-report.md"
                if rp.exists() and s.get("status") not in ("closed", "superseded"):
                    out.append((s["session_id"], n, str(rp)))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _surfaced_path(state_root, lead_sid):
    return lead_dir(state_root, lead_sid) / "surfaced_reports.json"


def load_surfaced(state_root, lead_sid):
    try:
        p = _surfaced_path(state_root, lead_sid)
        return set(json.loads(p.read_text())) if p.exists() else set()
    except Exception:
        return set()


def mark_surfaced(state_root, lead_sid, keys):
    """Record report keys (\"execsid:packet\") already announced to this lead, so each report wakes
    the lead exactly once."""
    try:
        cur = load_surfaced(state_root, lead_sid)
        cur.update(keys)
        d = lead_dir(state_root, lead_sid)
        d.mkdir(parents=True, exist_ok=True)
        _surfaced_path(state_root, lead_sid).write_text(json.dumps(sorted(cur)))
    except Exception:
        pass


def _executor_owner(state_root, exec_sid):
    """`owner_lead` recorded in this executor's session.json (None if unowned/missing/unreadable)."""
    try:
        s = json.loads((Path(state_root) / str(exec_sid) / "session.json").read_text())
        return s.get("owner_lead")
    except Exception:
        return None


def new_reports_for(state_root, lead_sid):
    """Executor reports this lead hasn't been told about yet — as (key, session_id, packet, path).

    Ownership-scoped: ONLY reports from executors this lead owns (the executor's `owner_lead ==
    lead_sid`) surface. Another lead's executors and UNOWNED ones (bare/legacy spawns with no
    owner_lead) never wake this lead — otherwise every stale unowned report on the machine would
    spam every new lead. Unowned executors are still visible passively in `relay list`, just not via
    the wake."""
    surfaced = load_surfaced(state_root, lead_sid)
    fresh = []
    for sid, packet, path in executor_reports(state_root):
        if _executor_owner(state_root, sid) != lead_sid:
            continue  # not owned by THIS lead (another lead's, or unowned) → never wakes it
        key = f"{sid}:{packet}"
        if key not in surfaced:
            fresh.append((key, sid, packet, path))
    return fresh


def _head_path(state_root, lead_sid):
    return lead_dir(state_root, lead_sid) / "last_head"


def read_head(state_root, lead_sid):
    try:
        p = _head_path(state_root, lead_sid)
        return p.read_text().strip() if p.exists() else ""
    except Exception:
        return ""


def write_head(state_root, lead_sid, head):
    try:
        d = lead_dir(state_root, lead_sid)
        d.mkdir(parents=True, exist_ok=True)
        _head_path(state_root, lead_sid).write_text((head or "").strip())
    except Exception:
        pass


def git_head(cwd):
    """Current commit sha of the repo at `cwd`, or "" if not a git repo / any error."""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def new_commits(cwd, since_head):
    """One-line summaries of commits in `cwd` after `since_head` up to HEAD. Empty on any error or
    when there's nothing new. Bounded to the 20 most recent so a huge gap can't flood the wake."""
    if not since_head:
        return []
    try:
        import subprocess
        r = subprocess.run(
            ["git", "-C", str(cwd), "rev-list", "--max-count=20", "--oneline",
             f"{since_head}..HEAD"],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        return [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    except Exception:
        return []


def has_inflight_executors(state_root, owner_lead=None):
    """True if any executor is still `busy` (working, no report yet) — i.e. there's something worth
    the idle lead waiting on. Reported ones are handled instantly, not by waiting.

    When `owner_lead` is given, ONLY executors this lead owns (`executor's owner_lead == owner_lead`)
    count — so a lead never idles waiting on another lead's executor OR an unowned (bare/legacy)
    one. The default `owner_lead=None` is the global, pre-ownership behavior (counts all)."""
    try:
        root = Path(state_root)
        if not root.exists():
            return False
        for d in root.iterdir():
            sj = d / "session.json"
            if not sj.exists():
                continue
            try:
                s = json.loads(sj.read_text())
                if s.get("status") != "busy":
                    continue
                if owner_lead is not None and s.get("owner_lead") != owner_lead:
                    continue  # busy, but not THIS lead's (another lead's, or unowned) → not ours
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _lock_path(state_root, lead_sid):
    return lead_dir(state_root, lead_sid) / "poll.lock"


def acquire_poll_lock(state_root, lead_sid):
    """Ensure only ONE background report-watcher runs per lead at a time — every idle cycle would
    otherwise spawn another long-lived poller. Returns True if this process took the lock. A lock
    held by a dead pid is considered stale and reclaimed."""
    try:
        lp = _lock_path(state_root, lead_sid)
        if lp.exists():
            try:
                if _pid_alive(lp.read_text().strip()):
                    return False  # a live poller already holds it
            except Exception:
                pass  # unreadable/garbage → treat as stale
        d = lead_dir(state_root, lead_sid)
        d.mkdir(parents=True, exist_ok=True)
        lp.write_text(str(os.getpid()))
        return True
    except Exception:
        return False


def release_poll_lock(state_root, lead_sid):
    try:
        lp = _lock_path(state_root, lead_sid)
        if lp.exists() and lp.read_text().strip() == str(os.getpid()):
            lp.unlink()
    except Exception:
        pass
