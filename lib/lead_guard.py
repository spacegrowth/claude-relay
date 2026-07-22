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
import re
import shutil
import subprocess
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
    "handoff_nudge": True,       # suggest handing off when the lead transcript gets heavy
    "handoff_nudge_mb": 5,       # transcript-size threshold (MB); proxy, not context occupancy
    "executor_default_model": "sonnet",  # model an executor launches with when --model is omitted —
                                  # relay's own policy, never the human's personal `/model` default
                                  # (see "executor model policy" section below: incident where a
                                  # null-model executor silently ran a full day on the user's
                                  # top-tier default)
    "executor_model_ceiling": "opus",    # spawn refuses a requested executor model ABOVE this tier
                                  # without --model-override "<reason>" (see "executor model policy")
    "stall_threshold_seconds": 2700,  # bin/relay's STALL_THRESHOLD_SECONDS override (wake-watch
                                  # design §6): kept independently of poll_seconds (1800) so a long
                                  # executor doesn't flip to `stalled` at the exact instant the
                                  # idle-lead poller's window also expires — see
                                  # docs/wake-watch-design.md §2.2's "two numeric coincidences".
    "autonomous_mode": False,     # the POSTURE a newly-armed lead holds (§6f, task #16 phase 1).
                                  # False = wait-for-human on every routine approval beat (the safe
                                  # default, and it must stay the default). True = new leads arm
                                  # already in auto, for someone who ALWAYS works this way. Either
                                  # way `relay auto on|off` flips it mid-session; the posture lives
                                  # in the lead's own marker, so it is per-session and resets to
                                  # this config value on every fresh arm rather than persisting
                                  # silently. Auto does NOT by itself cover committing executor work:
                                  # that has its own five-condition gate (#16 phase 2) — see
                                  # report_verify.clearance, `relay verify --for-autocommit`, and
                                  # skills/mode/SKILL.md's stop-list.
    "executor_escalation": True,  # arm every spawned executor with the escalation Stop hook
                                  # (wake-watch design §9): once its report lands and it goes idle,
                                  # push a nudge into the owning lead's tab, once. A net UNDER the
                                  # lead's own fast-path check, not a replacement — kill-switch
                                  # matches the auto_wake/notify_on_wake pattern above.
    "bash_gate_logging": True,   # task d1 (§10): logging-only Bash gate for armed leads — ledgers
                                  # `would_have_blocked` on an implementation-verb Bash command, NEVER
                                  # denies (dry-run-first, per §10's "Fable punchlist item 2": tune the
                                  # custody allowlist against real logs before this is ever allowed to
                                  # block). Default True because logging has no user-visible effect —
                                  # same "safe to default on" reasoning as auto_wake. Flip off to
                                  # silence the ledger without a release.
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


# ---- executor model policy ---------------------------------------------------------------------
# LIVE INCIDENT (2026-07-12, ~.relay-tasks/executor-model-leak-2026-07-12.md): an executor spawned
# without --model stored "model": null and launched plain `claude`, which silently inherited the
# HUMAN's personal `/model` default — a full day (11 packets) ran on their top-tier default before
# anyone noticed, because `relay list` renders null as `-`. executor_default_model/
# executor_model_ceiling (LEAD_DEFAULTS above) exist so an executor's model is always relay's own
# policy decision, never an accidental inheritance.

# Ascending: name-based, tier-agnostic (compares the tier WORD found in a model string, not a
# specific model id), so tomorrow's new top-tier release just needs a word added here rather than
# every existing model string enumerated.
TIER_ORDER = ["haiku", "sonnet", "opus", "fable"]


def model_tier(model):
    """The tier word from TIER_ORDER contained in `model` (case-insensitive substring), or None if
    `model` is empty or names no recognized tier."""
    if not model:
        return None
    s = str(model).lower()
    for tier in TIER_ORDER:
        if tier in s:
            return tier
    return None


def model_exceeds_ceiling(model, ceiling):
    """True if `model`'s tier is strictly above `ceiling`'s tier in TIER_ORDER. An unrecognized
    tier — for `model` OR `ceiling` — is treated as above-ceiling (refuse by default): a model name
    this list doesn't know about yet must not silently sail through just because it can't be
    ranked, and a misconfigured ceiling must fail toward requiring an override, not toward
    allowing everything."""
    model_t = model_tier(model)
    if model_t is None:
        return True
    ceiling_t = model_tier(ceiling)
    if ceiling_t is None:
        return True
    return TIER_ORDER.index(model_t) > TIER_ORDER.index(ceiling_t)


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


# ---- Bash gate: custody-vs-implementation taxonomy (task d1, §10) ------------------------------
# LOGGING-ONLY (dry-run-first, §10's "Fable punchlist item 2"): this taxonomy decides what GETS
# LOGGED as would-have-blocked, never what gets blocked — there is no blocking code path yet.
# Deliberately ONE tunable structure (module-level, ordered, per-rule named) because the whole
# point of this phase is tuning it against real lead-day logs before it's ever allowed to refuse
# anything. Ordering matters: CUSTODY_RULES are checked first and win on overlap (§10: "start
# permissive on custody, strict on provisioning") — e.g. `npm run build` (implementation) vs
# `npm test`/`npm run test:*` (custody) both start with `npm`, so the custody test-invocation
# pattern must be checked before the implementation npm pattern would otherwise even come into play
# for a command it was never meant to match; kept as an explicit ordering rule regardless, since a
# future implementation rule could easily overlap a custody one by accident.
#
# CUSTODY_RULES: the lead's own assigned, mutating work (§10) — free-pass, never ledgered.
# Reads (cat/ls/grep/git status/git diff/git log/...) aren't listed here at all: they simply never
# match any IMPLEMENTATION_RULES pattern, so they free-pass by construction without needing an
# explicit rule.
CUSTODY_RULES = [
    {"name": "git-commit-push", "pattern": r"\bgit\s+(commit|push)\b"},
    {"name": "systemctl-restart-status", "pattern": r"\bsystemctl\s+(restart|status)\b"},
    {"name": "ssh-clickhouse-sql", "pattern": r"\bclickhouse-client\b"},
    {"name": "test-suite", "pattern":
        r"\b(pytest|py\.test|npm\s+(run\s+)?test\b|yarn\s+test\b|go\s+test\b|cargo\s+test\b|"
        r"make\s+test\b|tox\b)"},
]

# IMPLEMENTATION_RULES: box-provisioning verbs (§10) — the incident class this gate exists to catch.
IMPLEMENTATION_RULES = [
    {"name": "npm-install", "pattern": r"\bnpm\s+(install|ci)\b"},
    {"name": "npm-run-build", "pattern": r"\bnpm\s+run\s+build\b"},
    {"name": "package-install", "pattern": r"\b(yarn\s+(install|add)\b|pip3?\s+install\b)"},
    {"name": "compiler", "pattern":
        r"\b(tsc|gcc|g\+\+|clang(\+\+)?|go\s+build|cargo\s+build|make)\b"},
    {"name": "git-clone", "pattern": r"\bgit\s+clone\b"},
    {"name": "service-file-write", "pattern":
        r"(/etc/systemd/system/\S*\.service|systemctl\s+(enable|daemon-reload)\b)"},
    {"name": "sed-inplace", "pattern": r"\bsed\s+-i\b"},
    {"name": "heredoc", "pattern": r"(?<!<)<<(?!<)-?~?\s*['\"]?\w+"},
    {"name": "tee-mutation", "pattern": r"\btee\b"},
    {"name": "rsync", "pattern": r"\brsync\b"},
]


def classify_bash_command(cmd):
    """The d1 verb-taxonomy verdict for a single Bash command string: the matched rule's name if
    it's an IMPLEMENTATION verb (the caller should ledger would_have_blocked), or None if it's a
    CUSTODY verb OR anything unclassified (free-pass either way — this function never signals
    "block", only "log or don't"). CUSTODY_RULES are checked first so any pattern overlap resolves
    toward the free-pass, per §10's permissive-on-custody instruction. Never raises: an
    unparseable/non-string `cmd` degrades to None (unclassified → free-pass, the safe direction for
    a logging-only gate)."""
    try:
        s = str(cmd or "")
        for rule in CUSTODY_RULES:
            if re.search(rule["pattern"], s):
                return None
        for rule in IMPLEMENTATION_RULES:
            if re.search(rule["pattern"], s):
                return rule["name"]
        return None
    except Exception:
        return None


# ---- lead-mode state (marker + grace window) --------------------------------------------------

def is_lead(state_root, session_id):
    """The sole 'is this a lead session' test. Marker absent (or any error) → not lead → the hooks
    fast-exit-allow, which is the entire zero-impact path for non-lead/executor sessions.

    A TOMBSTONED marker counts as NOT a lead. A tombstone means the session exited cleanly but is
    resumable, so its identity is retained (see tombstone_lead / docs/lead-arming-durability.md) —
    but until a resume revives it, the gate and the wake must stay off. Returning True here for a
    tombstone would be strictly worse than the bug this replaced: an exited session would still be
    armed."""
    try:
        if not marker_path(state_root, session_id).exists():
            return False
        return not is_tombstoned(read_marker(state_root, session_id))
    except Exception:
        return False


def write_marker(state_root, session_id, model=None, iterm_session=None, project=None, cwd=None,
                 tab_label=None, color=None, plugin_version=None, stop_hook_timeout=None,
                 predecessor=None, started=None, backend=None, autonomous=False,
                 autonomous_source="config"):
    d = lead_dir(state_root, session_id)
    d.mkdir(parents=True, exist_ok=True)
    marker_path(state_root, session_id).write_text(json.dumps({
        "session_id": session_id,
        "project": project,          # human-readable project name (defaults to cwd basename at call site)
        "cwd": cwd,                  # where a restored lead should reopen
        "tab_label": tab_label,      # stable relay-controlled tab title → makes `relay focus <lead>` work
        "color": color,              # [r,g,b] tab color; this lead's executors inherit it at spawn
        "last_active": now(),        # heartbeat — refreshed on every write_marker call
        "started": started or now(), # preserved across re-arms by callers that read the existing marker first
        "model": model,
        "iterm_session": iterm_session,  # $TERM_SESSION_ID — recorded tab metadata (debugging)
        "backend": backend,          # which terminal app hosts this lead's OWN tab ("iterm" |
                                      # "terminal"), same field name/values as an executor's
                                      # session.json — term_backend() reads either. Re-stamped on
                                      # every arm (unlike predecessor/started) so it always reflects
                                      # the ambient backend `relay lead-start` actually ran under.
        # The plugin version this session is bound to, and the Stop-hook timeout that version
        # declares — captured at arm time (bin/relay shares ${CLAUDE_PLUGIN_ROOT} with the hooks, so
        # what it reads IS what will fire). wake_hook_state() reads these back to flag a lead whose
        # wake poller will be killed early (pre-fix hook), so a silently-stale session is VISIBLE in
        # `relay list` rather than only found by forensics after a missed wake.
        "plugin_version": plugin_version,
        "stop_hook_timeout": stop_hook_timeout,
        # A handoff successor's predecessor lead (session_id/tab_label/iterm_session), stamped by
        # cmd_handoff BEFORE the caller steps down — by successor-time the old marker is gone, so
        # this is the only record of how to close that now-unarmed zombie tab. `relay
        # close-predecessor` reads and clears it. None for any lead that didn't arrive via handoff.
        "predecessor": predecessor,
        # Autonomous posture (§6f / task #16 phase 1) — whether THIS lead proceeds by default on the
        # routine in-plan approval beats instead of waiting for the human. Deliberately written on
        # EVERY arm (never preserved like predecessor/started): the posture is opt-in-each-time, so a
        # fresh `lead-start` resets it to whatever `autonomous_mode` config says (default False).
        # `autonomous_source` records WHERE the current posture came from — "config" when an arm set
        # it, "command" once `relay auto on|off` overrode it mid-session — so `relay auto status` can
        # tell the human which it is rather than just the boolean.
        "autonomous": bool(autonomous),
        "autonomous_source": autonomous_source,
    }, indent=2))


def read_marker(state_root, session_id):
    try:
        p = marker_path(state_root, session_id)
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def autonomous_state(marker):
    """This lead's autonomous posture as `(on, source)` — `(bool, "config" | "command")`.

    Read from the marker alone, never from config: config only decides the posture an arm STARTS
    with (cmd_lead_start stamps it), after which the marker is the single source of truth. That is
    what makes the posture per-session and resettable — a lead armed before this feature existed
    (no key at all) reads as `(False, "config")`, i.e. the safe wait-for-human default."""
    if not isinstance(marker, dict):
        return (False, "config")
    src = marker.get("autonomous_source")
    return (bool(marker.get("autonomous")),
            src if src in ("config", "command") else "config")


def set_autonomous(state_root, session_id, on):
    """Flip a lead's autonomous posture (read-modify-write, preserving every other marker field) and
    stamp its source as "command". Returns True when the marker was updated, False when there is no
    marker to update — the caller decides whether that is an error (`relay auto` treats it as one:
    a posture with no lead to hold it would be silently meaningless)."""
    m = read_marker(state_root, session_id)
    if not isinstance(m, dict) or not m:
        return False
    m["autonomous"] = bool(on)
    m["autonomous_source"] = "command"
    m["last_active"] = now()
    marker_path(state_root, session_id).write_text(json.dumps(m, indent=2))
    return True


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


def _read_plugin_version(plugin_root):
    try:
        return json.loads((Path(plugin_root) / ".claude-plugin" / "plugin.json").read_text()).get("version")
    except Exception:
        return None


def _read_stop_hook_timeout(plugin_root):
    try:
        d = json.loads((Path(plugin_root) / "hooks" / "hooks.json").read_text())
        return d["hooks"]["Stop"][0]["hooks"][0].get("timeout")
    except Exception:
        return None


def touch_lead(state_root, session_id, plugin_root=None):
    """Heartbeat: refresh this lead's `last_active` to now(), preserving every other marker field
    (read-modify-write). Called once per lead turn so `relay list`'s last_active reflects real
    liveness — a stale one means the lead probably crashed.

    When `plugin_root` is given, ALSO re-stamps plugin_version/stop_hook_timeout by reading
    .claude-plugin/plugin.json and hooks/hooks.json from THAT root — the caller (the Stop hook)
    passes its OWN plugin root, so what gets read is whatever version is live right now, not
    whatever was live at arm time. This kills the stale-VER-until-re-arm gap: previously the stamp
    only refreshed when the lead re-ran /relay:mode, so a lead that stayed armed across a plugin
    update kept showing its old version/timeout in `relay list` until manually re-armed. Only
    overwrites a stamped field when the freshly-read value is present AND differs from the marker's
    current one, keeping this cheap in the steady state.

    Fully defensive: a missing/unreadable/non-dict marker is a silent no-op, and nothing here ever
    raises (the Stop hook's fail-open contract must hold even if the heartbeat can't be written)."""
    try:
        m = read_marker(state_root, session_id)
        if not isinstance(m, dict) or not m:
            return  # no marker to touch → nothing to do
        m["last_active"] = now()
        if plugin_root is not None:
            ver = _read_plugin_version(plugin_root)
            if ver is not None and ver != m.get("plugin_version"):
                m["plugin_version"] = ver
            timeout = _read_stop_hook_timeout(plugin_root)
            if timeout is not None and timeout != m.get("stop_hook_timeout"):
                m["stop_hook_timeout"] = timeout
        marker_path(state_root, session_id).write_text(json.dumps(m, indent=2))
    except Exception:
        pass


def update_marker(state_root, session_id, **fields):
    """Read-modify-write a FEW marker fields, preserving everything else — the safe counterpart to
    write_marker, which rewrites the whole marker and silently drops anything the caller forgot to
    re-pass (§1). Same defensive contract as touch_lead: a missing/unreadable marker is a silent
    no-op and nothing here ever raises. Returns True only if the write happened."""
    try:
        m = read_marker(state_root, session_id)
        if not isinstance(m, dict) or not m:
            return False
        m.update(fields)
        marker_path(state_root, session_id).write_text(json.dumps(m, indent=2))
        return True
    except Exception:
        return False


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
    """Remove the whole lead/<sid>/ subtree (step-down, or a SessionEnd whose reason means the
    conversation is genuinely gone — `clear`/`logout`). Best-effort; routing events already live
    durably in the shared sessions.jsonl ledger, so there's nothing here to preserve.

    NOT used for a resumable exit any more — see tombstone_lead."""
    try:
        shutil.rmtree(lead_dir(state_root, session_id))
    except Exception:
        pass


# ---- tombstones: arming that survives exit→resume (docs/lead-arming-durability.md) --------------
# A Claude Code session is RESUMABLE: `--resume` restores the same session_id AND the full
# conversation, and fires SessionStart with source="resume" (spiked and verified — see that doc's
# §7). Deleting the lead marker on a routine quit therefore treated a *pause* as a *death*, and the
# resumed session came back silently unarmed: gate off, wake structurally impossible.
#
# So a resumable exit TOMBSTONES the marker instead of deleting it — retaining everything (project,
# cwd, iterm_session, colour, predecessor, started) so the revive is lossless — while `is_lead`
# reports False for the duration.

def is_tombstoned(marker):
    """True if this marker is a tombstone: the session exited cleanly but is resumable, so its
    identity is retained while it counts as NOT armed. Never raises."""
    try:
        return bool((marker or {}).get("ended"))
    except Exception:
        return False


def tombstone_lead(state_root, session_id, now_ts=None):
    """Mark a lead ended-but-resumable instead of deleting it. Retains every other field so
    revive_lead() is lossless. Returns True if a marker was actually tombstoned (no marker, or an
    already-tombstoned one, returns False so callers can stay quiet). Never raises."""
    try:
        m = read_marker(state_root, session_id)
        if not m or is_tombstoned(m):
            return False
        m["ended"] = True
        m["ended_at"] = now() if now_ts is None else time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(now_ts))
        marker_path(state_root, session_id).write_text(json.dumps(m, indent=2))
        return True
    except Exception:
        return False


def revive_lead(state_root, session_id):
    """Re-arm a tombstoned lead (SessionStart source="resume"): drop the tombstone flags and refresh
    last_active. Everything else — project name included — is restored untouched, so a resumed lead
    is indistinguishable from one that never exited. Returns True ONLY if a tombstone was actually
    revived, so a plain fresh start stays a silent no-op. Never raises."""
    try:
        m = read_marker(state_root, session_id)
        if not m or not is_tombstoned(m):
            return False
        m.pop("ended", None)
        m.pop("ended_at", None)
        m["last_active"] = now()
        marker_path(state_root, session_id).write_text(json.dumps(m, indent=2))
        return True
    except Exception:
        return False


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
    """Record report keys (\"execsid:packet\") PROVEN to have reached this lead, so each report wakes
    it exactly once. Callers are the delivery-proven ones only: the #17 channels (check/diff/close/
    retire — the lead demonstrably handled the report) and promote_pending below. An announce that
    merely FIRED is not proof — see mark_pending (#22)."""
    try:
        cur = load_surfaced(state_root, lead_sid)
        cur.update(keys)
        d = lead_dir(state_root, lead_sid)
        d.mkdir(parents=True, exist_ok=True)
        _surfaced_path(state_root, lead_sid).write_text(json.dumps(sorted(cur)))
    except Exception:
        pass
    drop_pending(state_root, lead_sid, keys)  # proven by another channel → stop retrying it


# ---- #22: announced-but-unproven wakes (§13's lost-wake bug) -----------------------------------
# The bug: the Stop hook stamped `surfaced` the moment it ANNOUNCED, then relied on its exit-2
# reaching the lead. A firing that can't be delivered (the lead is mid-turn, so the harness drops a
# stale hook's exit-2) still kept the stamp, and the report was never announced again — silently
# swallowed. Mirror image of #17: that stamped too little (duplicate wakes), this stamps too early
# (lost wakes), which is strictly worse.
#
# The fix is a two-phase stamp. An announce records the keys as PENDING, which does NOT suppress a
# later announce — so an undelivered wake naturally retries on the lead's next Stop. Delivery is
# PROVEN by the harness itself: when our exit-2 actually continues the session, Claude Code re-runs
# the Stop hook with `stop_hook_active: true`, and that re-run promotes pending → surfaced. If the
# wake was dropped instead, no such re-run happens, the key stays pending, and the next Stop
# re-announces it.
WAKE_RETRY_CAP = 3  # give up (and stamp) after this many unproven announces — a lead whose harness
                    # never sets stop_hook_active must not be re-announced at forever.


def _pending_path(state_root, lead_sid):
    return lead_dir(state_root, lead_sid) / "pending_wakes.json"


def load_pending(state_root, lead_sid):
    """{key: {"announces": n}} for wakes announced but not yet proven delivered."""
    try:
        p = _pending_path(state_root, lead_sid)
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_pending(state_root, lead_sid, pending):
    try:
        d = lead_dir(state_root, lead_sid)
        d.mkdir(parents=True, exist_ok=True)
        p = _pending_path(state_root, lead_sid)
        if pending:
            p.write_text(json.dumps(pending, indent=2, sort_keys=True))
        elif p.exists():
            p.unlink()
    except Exception:
        pass


def mark_pending(state_root, lead_sid, keys):
    """Record an UNPROVEN announce. Returns the keys that hit WAKE_RETRY_CAP and were therefore
    stamped surfaced outright (announced enough times that continuing to retry is spam, not
    recovery) — the caller may want to say so in the ledger."""
    pending = load_pending(state_root, lead_sid)
    capped = []
    for k in keys:
        n = pending.get(k, {}).get("announces", 0) + 1
        if n >= WAKE_RETRY_CAP:
            capped.append(k)
            pending.pop(k, None)
        else:
            pending[k] = {"announces": n}
    _save_pending(state_root, lead_sid, pending)
    if capped:
        try:                                  # NOT via mark_surfaced: that would recurse into
            cur = load_surfaced(state_root, lead_sid)   # drop_pending, which we just did by hand
            cur.update(capped)
            _surfaced_path(state_root, lead_sid).write_text(json.dumps(sorted(cur)))
        except Exception:
            pass
    return capped


def drop_pending(state_root, lead_sid, keys):
    """Forget pending entries for `keys` (they were proven some other way)."""
    pending = load_pending(state_root, lead_sid)
    if not pending:
        return
    for k in keys:
        pending.pop(k, None)
    _save_pending(state_root, lead_sid, pending)


def promote_pending(state_root, lead_sid):
    """Delivery is PROVEN — promote every pending key to surfaced and return them. Called when the
    harness re-runs the Stop hook with stop_hook_active set, which only happens because our own
    exit-2 continued the session: the wake reached the lead."""
    pending = load_pending(state_root, lead_sid)
    if not pending:
        return []
    keys = sorted(pending)
    try:
        cur = load_surfaced(state_root, lead_sid)
        cur.update(keys)
        d = lead_dir(state_root, lead_sid)
        d.mkdir(parents=True, exist_ok=True)
        _surfaced_path(state_root, lead_sid).write_text(json.dumps(sorted(cur)))
    except Exception:
        return []
    _save_pending(state_root, lead_sid, {})
    return keys


# ---- Stop-hook: handoff nudge (transcript-size proxy) ------------------------------------------

def transcript_mb(path):
    """Size of the lead's transcript JSONL in MB (float), 0.0 on any error/missing/None path — a
    usable PROXY for session weight, NOT context-window occupancy (compaction shrinks context but
    the file keeps growing, which is exactly why this is a one-time nudge, not automation)."""
    try:
        if not path:
            return 0.0
        return os.path.getsize(path) / (1024 * 1024)
    except Exception:
        return 0.0


def _handoff_nudged_path(state_root, lead_sid):
    return lead_dir(state_root, lead_sid) / "handoff_nudged"


def handoff_nudged(state_root, lead_sid):
    """Whether this lead has already been nudged to hand off — a bare flag file (not JSON, unlike
    surfaced_reports.json) since it's a single onetime bit, mirrored in spirit from that pattern."""
    try:
        return _handoff_nudged_path(state_root, lead_sid).exists()
    except Exception:
        return False


def mark_handoff_nudged(state_root, lead_sid):
    try:
        d = lead_dir(state_root, lead_sid)
        d.mkdir(parents=True, exist_ok=True)
        _handoff_nudged_path(state_root, lead_sid).touch()
    except Exception:
        pass


def read_session_json(state_root, session_id):
    """An executor's own session.json as a dict, or {} if missing/unreadable — the ONE place a hook
    script reads it (bin/relay's own read_session lives in bin/relay, which has no .py extension
    and isn't a normal import target for a hook). Used by the escalation hook to confirm a sid is a
    genuine relay executor and read its current_packet/owner_lead. Never raises."""
    try:
        p = Path(state_root) / str(session_id) / "session.json"
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


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
    """True if any executor is still `busy` OR `stalled` (working, or long-running-but-alive, with
    no report yet) — i.e. there's something worth the idle lead waiting on. Reported ones are
    handled instantly, not by waiting.

    `stalled` counts as in-flight (wake-watch design §6): a long-but-alive executor is the MOST
    likely to report while the lead idles, so excluding it (as the pre-fix code did) was backwards
    — it dropped the executor out of the watched set at exactly the moment its report becomes most
    imminent.

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
                if s.get("status") not in ("busy", "stalled"):
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


def _pid_start_time(pid):
    """The process's launch timestamp (`ps lstart`), or None on any failure. SINGLE SOURCE OF TRUTH
    for pid-reuse detection — bin/relay's pid_start_time is a thin delegate to this (used for
    executor liveness there, and for the poll-lock heartbeat here): recorded at
    acquire/spawn time and compared later so a recycled pid — the OS reusing this exact number for
    an unrelated process — doesn't read as 'the original holder is alive'."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def _lock_path(state_root, lead_sid):
    return lead_dir(state_root, lead_sid) / "poll.lock"


# A legacy (pre-heartbeat) lock is a bare int with no ts/pid_started — it can only be judged stale
# by file mtime. Uses the DEFAULT poll_seconds (not whatever the current config says), since this
# path only exists for a brief mixed-version window and doesn't need to track live config.
_LEGACY_LOCK_TTL = LEAD_DEFAULTS["poll_seconds"] + 120  # + slack


def _poll_lock_status(lock_path, poll_interval):
    """The ONE staleness definition for the poll.lock, shared by acquire_poll_lock (which breaks a
    stale lock and reclaims it) and poll_lock_state (which only reports it, for `relay list`).
    Returns "absent" | "live" | "stale". Never raises — any bad input is treated as "stale" so it's
    reclaimable rather than a permanent block.

    Stale when: content unreadable/garbage; pid not alive; pid alive but its recorded start time no
    longer matches the pid's CURRENT start time (pid reuse — the holder is an impostor); or the
    heartbeat ts is older than max(3 * poll_interval, 30) seconds (the holder stopped ticking,
    whoever it is — this alone is sufficient, independent of pid liveness/reuse). A legacy
    (pre-heartbeat) bare-int lock has none of that; it's judged stale purely by file mtime against
    _LEGACY_LOCK_TTL."""
    try:
        lp = Path(lock_path)
        if not lp.exists():
            return "absent"
        raw = lp.read_text().strip()
    except Exception:
        return "stale"
    if not raw:
        return "stale"

    data = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "pid" in parsed:
            data = parsed
    except Exception:
        data = None

    try:
        if data is not None:
            pid = data.get("pid")
            if not _pid_alive(pid):
                return "stale"
            recorded = data.get("pid_started")
            if recorded and _pid_start_time(pid) != recorded:
                return "stale"  # pid recycled — the live process isn't the original holder
            ts = data.get("ts")
            if ts is None:
                return "stale"
            if (time.time() - float(ts)) > max(3 * poll_interval, 30):
                return "stale"  # heartbeat too old — holder stopped ticking
            return "live"
        else:
            # Legacy bare-int lock (pre-heartbeat), or garbage that isn't valid JSON either way.
            pid = int(raw)  # raises ValueError → caught below → "stale"
            if not _pid_alive(pid):
                return "stale"
            mtime = lp.stat().st_mtime
            if (time.time() - mtime) > _LEGACY_LOCK_TTL:
                return "stale"
            return "live"
    except Exception:
        return "stale"


def poll_lock_state(state_root, lead_sid, poll_interval=5):
    """Public read-only view of a lead's poll.lock health for `relay list` — "absent" | "live" |
    "stale". Never breaks or touches the lock; just reports the same verdict acquire_poll_lock would
    reach. Never raises."""
    try:
        return _poll_lock_status(_lock_path(state_root, lead_sid), poll_interval)
    except Exception:
        return "stale"


def _acquire_lock(lock_path, poll_interval=5):
    """Path-generic lock acquire — the shared mechanics behind acquire_poll_lock (a lead's
    poll.lock, the only current caller since the executor-side escalation lock was retired in
    wake-watch design §9 — the push is single-shot, with nothing left to serialize). A stale lock
    (dead pid, recycled pid, or a heartbeat that stopped ticking — see _poll_lock_status) is broken
    and reclaimed. Returns True if this process took the lock."""
    try:
        lp = Path(lock_path)
        if _poll_lock_status(lp, poll_interval) == "live":
            return False  # a live poller already holds it
        lp.parent.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        lp.write_text(json.dumps({
            "pid": pid,
            "pid_started": _pid_start_time(pid),
            "ts": time.time(),
        }))
        return True
    except Exception:
        return False


def _heartbeat_lock(lock_path):
    """Path-generic heartbeat refresh — see _acquire_lock. ONLY rewrites when the lock's pid is
    THIS process; never stomps another holder's lock. Never raises."""
    try:
        lp = Path(lock_path)
        if not lp.exists():
            return
        data = json.loads(lp.read_text().strip())
        if not isinstance(data, dict) or data.get("pid") != os.getpid():
            return  # not ours (or legacy/garbage) → don't touch it
        data["ts"] = time.time()
        lp.write_text(json.dumps(data))
    except Exception:
        pass


def _release_lock(lock_path):
    """Path-generic release — see _acquire_lock. ONLY releases when the lock is still ours
    (pid == os.getpid()). Handles both JSON (current) and legacy bare-int lock content. Never
    raises."""
    try:
        lp = Path(lock_path)
        if not lp.exists():
            return
        raw = lp.read_text().strip()
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, dict):
            pid = data.get("pid")
        else:
            # A bare legacy int lock is itself valid JSON (json.loads("123") == 123, no exception),
            # so it lands here rather than the except above — fall back to plain int parsing.
            try:
                pid = int(raw)
            except Exception:
                pid = None
        if pid == os.getpid():
            lp.unlink()
    except Exception:
        pass


def acquire_poll_lock(state_root, lead_sid, poll_interval=5):
    """Ensure only ONE background report-watcher runs per lead at a time — every idle cycle would
    otherwise spawn another long-lived poller. Returns True if this process took the lock."""
    return _acquire_lock(_lock_path(state_root, lead_sid), poll_interval)


def heartbeat_poll_lock(state_root, lead_sid):
    """Refresh this lock's heartbeat ts, once per poll tick — proof of life so a hard-killed poller
    (plugin reload, sleep, crash, logout) can't leave a stuck lock indefinitely."""
    _heartbeat_lock(_lock_path(state_root, lead_sid))


def release_poll_lock(state_root, lead_sid):
    """Release the lock, but ONLY if it's still ours — same ownership rule as acquire."""
    _release_lock(_lock_path(state_root, lead_sid))


# ---- executor-side escalation lock (wake-watch design §4.1) — same mechanics, own lock file -----
# ---- executor-side escalation ledger + decision tree (wake-watch design §9) ---------------------
# A SEPARATE ledger from the lead's surfaced_reports.json, by design: the executor's own "I
# pushed/notified" bookkeeping must never be written into the lead's own surfacing ledger, or the
# executor pinging the human would silently consume the lead's own announcement — leaving the lead
# silent when the human returns. Keyed by packet number (the file itself already scopes to one
# executor via its path); with the push single-shot (§9.2 — no retry, no backoff), each record is
# now just a one-bit "already handled" flag: {"status": "resolved" | "notified" | "sent"}.

def _escalation_path(state_root, exec_sid):
    return Path(state_root) / str(exec_sid) / "escalation.json"


def load_escalation(state_root, exec_sid):
    """This executor's escalation-state ledger (dict keyed by str(packet)), or {} if missing/
    unreadable. Never raises."""
    try:
        p = _escalation_path(state_root, exec_sid)
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def save_escalation(state_root, exec_sid, ledger):
    """Write the full escalation ledger dict back (the hook reads, mutates one packet's record,
    then calls this with the whole dict). Best-effort; never raises."""
    try:
        p = _escalation_path(state_root, exec_sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ledger, indent=2))
    except Exception:
        pass


def escalation_decision(state_root, exec_sid, packet, owner_lead):
    """wake-watch design §9's push decision tree for the executor-side escalation hook, given the
    on-disk state it reads (the owning lead's marker + its surfaced_reports.json) — one of:

      "resolved"      — the owning lead already surfaced this report (its key is in that lead's
                        surfaced_reports.json) — nothing left to do.
      "unowned"       — no owner_lead recorded at all → no lead to push to; notify the human
                        directly.
      "owner-missing" — owner_lead is set but its marker is gone (crashed/closed/pruned) → notify
                        the human directly (do NOT assume a marker exists just because owner_lead
                        is non-null).
      "send"          — push it: type into the owning lead's tab, unconditionally. §9.5b proved
                        injecting mid-turn is harmless (it queues and is processed intact at
                        turn-end), so there is no busy check left in this tree — it collapsed from
                        the pre-push 6-branch version (resolved/unowned/owner-missing/nudge/wait/
                        stale) once the busy-guard was shown to protect nothing.

    Reuses read_marker/load_surfaced — no reimplementation. Never raises; any bad input degrades to
    "owner-missing" (the safe direction is surfacing to a human, never silent inaction)."""
    try:
        if not owner_lead:
            return "unowned"
        marker = read_marker(state_root, owner_lead)
        if not marker:
            return "owner-missing"
        key = f"{exec_sid}:{packet}"
        if key in load_surfaced(state_root, owner_lead):
            return "resolved"
        return "send"
    except Exception:
        return "owner-missing"


# ---- executor escalation settings file (wake-watch design's "Key integration fact") --------------
# Executors are launched by build_claude_cmd as plain `claude [--session-id][--model] <prompt>` —
# no --settings, no plugin-dir — so they get NO hooks today (unlike leads, who get theirs from the
# plugin's own hooks.json). To arm the escalation Stop hook on an executor, bin/relay's cmd_spawn
# must generate a settings file and pass it via `claude --settings <file>` (threaded through
# scripts/iterm.py's build_claude_cmd/spawn).

def build_escalation_settings(plugin_root, exec_name, timeout=30):
    """The `--settings` JSON content that arms an EXECUTOR with hooks/executor_escalation.py as a
    PLAIN synchronous Stop hook (wake-watch design §9.4) — no `asyncRewake`. The push is a
    single-shot: read some on-disk state, maybe type into a tab, exit — a few hundred ms of work,
    not a long-running background watcher, so there's nothing left to host asynchronously. `timeout`
    is just a safety margin above that, not a budget for grace/backoff sleeping (there is none).

    `exec_name` is passed to the hook AS AN ARGUMENT because the hook cannot otherwise learn which
    executor it is: Claude Code's payload carries the CLAUDE session id, while relay files an
    executor's state under its relay NAME (`~/.relay-tasks/<name>/`). Nothing in the payload maps
    one to the other, so without this the hook looks up a directory that doesn't exist, concludes
    "not a relay executor", and exits — silently, every time. Found live: the push never fired in
    production until the name was passed explicitly."""
    hook_path = str(Path(plugin_root) / "hooks" / "executor_escalation.py")
    return {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{hook_path} {exec_name}",
                            "timeout": timeout,
                        }
                    ]
                }
            ]
        }
    }


def write_escalation_settings(state_root, plugin_root, exec_name, timeout=30):
    """Write this executor's own `--settings` file into its state dir. PER-EXECUTOR (not shared),
    because the file carries that executor's relay name as a hook argument — see
    build_escalation_settings for why the hook can't derive it. Regenerated on each call so it
    always points at the CURRENTLY live plugin_root/version. Returns the path (str), or None on any
    failure — a write failure must fall back to spawning WITHOUT escalation armed rather than
    failing the whole spawn."""
    try:
        d = Path(state_root) / str(exec_name)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "settings.json"
        p.write_text(json.dumps(
            build_escalation_settings(plugin_root, exec_name, timeout=timeout), indent=2))
        return str(p)
    except Exception:
        return None
