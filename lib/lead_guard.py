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
import shlex
import shutil
import subprocess
import sys
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
                                 # click→the posting session), falling back to osascript's built-in
                                 # banner. iTerm forces a "Session …"-prefixed banner title on that
                                 # OSC tier (no escape parameter overrides it); "osascript" SKIPS the
                                 # OSC tier for a clean title/subtitle. A legacy "terminal-notifier"
                                 # value (pre-drop) is treated as "osascript" — no error, no retitle.
    "executor_skip_permissions": False,  # spawn executors with --dangerously-skip-permissions
    "terminal_app": "auto",      # "iterm" | "terminal" | "auto" ($TERM_PROGRAM decides; iTerm default)
    "tab_colors": True,          # iTerm only: color each lead's tab + its executors' tabs alike
    "tidy_tabs": True,           # iTerm only (backlog row 64): after every event that changes which
                                  # tabs exist or who owns them — spawn, `send --rotate/--upgrade`,
                                  # handoff, close-predecessor, resume, restart — re-order the tab
                                  # bar into [Lead] [Exec 1] [Exec 2] … [Lead 2] [Exec 2.1] … and
                                  # re-apply each lead's color to its own group. Purely cosmetic and
                                  # entirely best-effort: it needs the optional `iterm2` package and
                                  # iTerm's Python API enabled (the same one-time toggle adjacent-tab
                                  # placement needs), and degrades to one dim line without them, so
                                  # False here is only for someone who arranges their own tab bar.
                                  # `relay tidy` runs regardless — this key governs the AUTOMATIC
                                  # tidy only.
    "executor_layout": "tab",    # "tab" | "pane" (pane: iTerm only, split into the lead's window)
    "handoff_nudge": True,       # suggest handing off when the lead transcript gets heavy
    "handoff_nudge_mb": 5,       # SESSION-AGE signal (MB on disk, a compaction proxy) for BOTH
                                 # leads and executors: MB never shrinks, so a big number means
                                 # several compactions in — "the session's memory of the plan is a
                                 # summary of summaries" — even when live context is currently small.
                                 # Secondary to context_nudge_tokens (below) for both: fires the
                                 # LEAD's handoff nudge/statusline segment alongside tokens, and is
                                 # the EXECUTOR fallback reading when a transcript can't be parsed
                                 # for real usage at all.
    "context_warn_tokens": 120000,  # backlog row 57: an EARLIER, one-shot desktop-banner heads-up
                                 # for EXECUTORS, well below context_nudge_tokens' rotate line, so
                                 # a rotate can be PLANNED rather than discovered in a `relay list`
                                 # footnote. At/above `context_nudge_tokens` it simply never fires.
    "context_nudge_tokens": 150000,  # the COST/CONTEXT signal for EXECUTORS ONLY (bin/relay's
                                 # cmd_send gate, `relay list`/board's heavy footnote and CTX
                                 # columns, `relay send --rotate` advice): heavy when the LAST
                                 # request's live context (input + cache_read + cache_create —
                                 # transcript_usage's `last_prompt`) is at/above this many tokens.
                                 # This is the number that actually drives cost/compaction;
                                 # handoff_nudge_mb is the fallback reading only when usage can't be
                                 # read at all. A LEAD's own nudge reads `lead_nudge_tokens` instead
                                 # (below) — a lead's handoff costs more than an executor's rotation
                                 # (a fresh successor-seed vs. a plain respawn) and its context grows
                                 # slowly once diffs are reviewed by a fork, so it earns a higher line.
    "lead_nudge_tokens": 300000,  # the LEAD's own heaviness/handoff-nudge line, on a 1M window: the
                                 # Stop hook's handoff nudge, `relay status --statusline`'s weight
                                 # segment, `relay list`'s LEADS CTX/heavy footnote, and the board's
                                 # lead chip all read this (never context_nudge_tokens, which is
                                 # executors-only — see above). See `lead_nudge_threshold` for the
                                 # window-aware cap: a lead whose REAL context window (tier_windows.json
                                 # / `relay doctor`'s probe) is known to be 200_000 can never actually
                                 # reach 300k live context, so its EFFECTIVE line is capped to
                                 # `min(lead_nudge_tokens, context_nudge_tokens)` — never told it has
                                 # room the window doesn't have. An unprobed/unknown window leaves this
                                 # uncapped (most leads run the 1M window — executor_default_context
                                 # ships "1m" — and guessing 200k for an unprobed one would nudge it
                                 # far too early).
    "cache_ttl_minutes": 60,     # Claude Code's prompt-cache TTL for a main conversation (each executor
                                 # is one): 1 hour when signed in with a Claude plan within its included
                                 # usage; 5 minutes on an API key / cloud provider / usage credits unless
                                 # promptCacheTtl (CLAUDE_CODE_PROMPT_CACHE_TTL) is set to 1h. Set this to
                                 # match; see lead_guard.cache_state. Verified 2026-09-05 against the
                                 # Claude Code prompt-caching doc + a transcript's ephemeral_1h writes.
    "signoff_paths": [],          # per-machine ADDITIONS to report_verify.SIGNOFF_PATH_MARKERS
                                  # (auto-commit clearance condition 4): repo-relative path
                                  # substrings, same semantics as the built-ins, MERGED in by
                                  # `relay verify --for-autocommit` — never replacing them, so the
                                  # built-in markers can't be configured away. A hit against one of
                                  # these names its source as "configured in signoff_paths" rather
                                  # than a built-in's own text. Default [] = built-ins only.
    "executor_default_model": "sonnet",  # model an executor launches with when --model is omitted —
                                  # relay's own policy, never the human's personal `/model` default
                                  # (see "executor model policy" section below: incident where a
                                  # null-model executor silently ran a full day on the user's
                                  # top-tier default)
    "executor_default_effort": "high",  # thinking-effort an executor launches with when neither
                                  # --effort nor a packet EFFORT: line pins one — relay's OWN policy
                                  # (the CLI default), never the human's personal `effortLevel` from
                                  # ~/.claude/settings.json. Closes the same class of leak
                                  # executor_default_model closed for the model (LIVE INCIDENT
                                  # below): found 2026-09-05 — a machine with effortLevel "medium" in
                                  # its personal settings silently ran every unpinned executor at
                                  # medium, while the docs claimed "unset = the CLI default (high)".
                                  # Validated against EFFORT_LEVELS at spawn, same as an --effort
                                  # flag; an invalid value is refused there, not silently ignored.
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
    "executor_default_context": "1m",  # the context window an executor launches with when nothing
                                  # else decides it (no packet CONTEXT: line, no [1m] on --model, and
                                  # its referenced reading is under the heuristic threshold): "1m" or
                                  # "200k". Shipped "1m" — the window is a ceiling, not consumption
                                  # (you pay for tokens used, so a bounded packet costs the same at
                                  # either), and it stops executors compacting early on real work. A
                                  # packet can still pin `CONTEXT: 200k`; haiku (no 1M window) always
                                  # runs 200k. See lead_guard "executor context window".
    "auto_close": True,           # park finished executors automatically (see "auto-close policy"
                                  # below): an idle, REPORTED executor whose report the owning lead
                                  # has already seen is closed once its work has landed (its
                                  # claimed files are clean in the worktree — the lead committed or
                                  # discarded them) or once it has sat idle past
                                  # auto_close_idle_minutes. Closing is parking, not loss: `relay
                                  # send` to a closed session resumes the same conversation.
                                  # Heavy sessions are retired (seed written) instead of closed.
    "auto_close_idle_minutes": 60,  # idle-after-report threshold for the timer path; 0 = timer off
                                    # (the landed path still applies while auto_close is true)
    "executor_fallback_model": None,  # when set (a concrete model id, or a list tried in order),
                                  # every executor's per-launch --settings file carries
                                  # {"fallbackModel": [...]} so an OVERLOADED primary falls back to
                                  # a model YOU chose, with the CLI's visible notice — instead of
                                  # wherever. A bare string is accepted and wrapped into a
                                  # one-element list (see normalize_fallback_models) — Claude Code
                                  # validates this settings key as a LIST, not a string, so a bare
                                  # id here used to fail EVERY spawn's settings validation. An entry
                                  # equal to the executor's own launch model (compared with any
                                  # `[1m]` suffix stripped) is silently dropped — falling back to
                                  # the model already running would just spin in place, not
                                  # actually recover. Settings-file route on purpose: the
                                  # --fallback-model FLAG is print-mode-only, the settings key works
                                  # interactively (docs: model-config "Fallback model chains").
                                  # Default None = no configured fallback (the documented default).
    "executor_escalation": True,  # arm every spawned executor with the escalation Stop hook
                                  # (wake-watch design §9): once its report lands and it goes idle,
                                  # push a nudge into the owning lead's tab, once. A net UNDER the
                                  # lead's own fast-path check, not a replacement — kill-switch
                                  # matches the auto_wake/notify_on_wake pattern above.
    "transport_v2": "off",       # transport-v2 phase 1 (#28). "off" (default) = nothing changes, the
                                  # Stop-hook wake stack is the only channel. "prototype" = the
                                  # auto-appended GATES gain ONE extra final step telling the executor
                                  # to also SendMessage a `[relay-v2] report …` pointer to its owning
                                  # lead after the self-diff. Prototype only: the old wake stack is
                                  # still the real channel, and a failed send is ledgered, never fatal.
    "bash_gate_logging": True,   # task d1 (§10): logging-only Bash gate for armed leads — ledgers
                                  # `would_have_blocked` on an implementation-verb Bash command, NEVER
                                  # denies (dry-run-first, per §10's "Fable punchlist item 2": tune the
                                  # custody allowlist against real logs before this is ever allowed to
                                  # block). Default True because logging has no user-visible effect —
                                  # same "safe to default on" reasoning as auto_wake. Flip off to
                                  # silence the ledger without a release.
    "bash_write_gate": "log",    # backlog row 59: the lead's Bash WRITE vector (redirects, tee, sed -i,
                                  # cp/mv, python open(..., "w")) against tracked files in the cwd,
                                  # sized by the Edit gate's own edit_line_threshold/block_on_new_file
                                  # rule. "deny" blocks like the Edit gate, "log" ledgers
                                  # `would_have_blocked` (vector "bash") and allows, "off" skips it.
                                  # Parsing lives in lib/bash_writes.py; unknown shapes always allow.
    "usage_limit_pattern": (      # bin/relay's `relay list`/`check` pause detection (lead-found gap
                                  # (d), packet 0a153c7): a regex, matched case-insensitively against
                                  # the START of an executor's last assistant text, that marks it
                                  # `paused (limit)` instead of an ordinary `stalled`. This IS the
                                  # built-in default string bin/relay falls back to on a missing/
                                  # unparsable override — landed here (rather than left as a direct
                                  # config-file read) once this packet no longer forbids touching
                                  # this file; see bin/relay's `_usage_limit_pattern`. The real-world
                                  # wording Claude Code emits for this message is UNVERIFIED — no
                                  # contract/fixture/prior art was found for it — so this is a
                                  # reasonable guess, not a confirmed one; override it here if the
                                  # CLI's actual wording differs.
                                  r"^(?:you'?ve hit your (?:session|usage) limit"
                                  r"|(?:claude )?usage limit reached)"),
    "board_live": False,          # the lead's on/off switch for `relay board` LIVE mode: when true,
                                  # a plain `relay board` (no --live flag) writes the live-styled
                                  # page (meta-refresh + a board.json sidecar), AND every
                                  # state-changing command (list/check/send/spawn) plus the lead's
                                  # own Stop hook keep rewriting board.html/board.json in place —
                                  # no server process, just a file that stays at most one turn
                                  # stale. `relay board --live` also turns this behavior on for the
                                  # CURRENT board.html without touching config (see bin/relay's
                                  # _is_board_live_active: the board.json sidecar's mere presence on
                                  # disk is itself proof a live board is active, so the auto-rewrite
                                  # keeps going after just one `--live` run).
    "board_refresh_seconds": 10,  # the live board's <meta http-equiv="refresh"> interval (N) — also
                                  # the unit the "stale" threshold is measured in (3×N, bin/relay's
                                  # board_render.render): a page not rewritten within 3 refresh
                                  # cycles is presumed abandoned (lead stopped nudging state) and its
                                  # "updated HH:MM:SS" header turns red.
    "ctx_warn_wake": True,        # backlog row 87: whether an owned executor past `context_warn_tokens`
                                  # (but not yet `context_nudge_tokens`) also earns a 🟠 line on the
                                  # LEAD's own Stop-hook wake (hooks/stop_lead_watch.py), once per
                                  # executor sid, alongside row 57's desktop banner (which this does
                                  # NOT silence — a separate kill-switch on purpose: `notify_on_wake`
                                  # governs desktop banners generally, this governs only whether the
                                  # heads-up also rides the lead's own conversation). False = the
                                  # executor is still warned by the banner/footnote/board chip, just
                                  # never inside the wake text itself.
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


def pick_lead_color(state_root, session_id, exclude_leads=()):
    """Collision-free lead color: walks TAB_PALETTE forward from lead_color's hash index to find an
    unused color. Re-arm stable: if this lead's marker already claims a CURRENT palette color,
    returns it unchanged. Stale (old-palette) colors fall through to re-pick from current palette.
    Stale colors don't block slots (self-heals as leads re-arm). All 6 current palette slots claimed
    by OTHER leads → falls back to lead_color (acceptable at >6 leads). Fully defensive: any error
    → lead_color fallback. Returns [r, g, b].

    `exclude_leads` — session ids whose claimed color must NOT count as taken. The handoff case
    (cmd_handoff): the outgoing lead's color is being TRANSFERRED to its successor, not shared with
    it, so treating the caller's own color as claimed would push the successor onto a different
    color and repaint the whole group at every handoff."""
    try:
        import hashlib
        # Check if this lead already has a marker with a CURRENT-palette color (re-arm stability).
        existing = read_marker(state_root, session_id)
        if existing and isinstance(existing.get("color"), list):
            existing_color = existing.get("color")
            # Only preserve the color if it's still in the current palette; stale colors re-pick.
            if tuple(existing_color) in {tuple(c) for c in TAB_PALETTE}:
                return existing_color

        # Gather colors claimed by OTHER leads (skip this lead's marker, and any lead the caller
        # named as excluded — see `exclude_leads`).
        skip = {session_id} | {s for s in (exclude_leads or ())}
        claimed = set()
        for lead in list_leads(state_root):
            if lead.get("session_id") in skip:
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


def notify_banner(cfg, title, subtitle, message, lead_sid=None, iterm_session=None, group=None,
                  tty=None, state_root=None):
    """The two-tier desktop notification chain EVERY relay banner should use — extracted from
    hooks/stop_lead_watch.py's original `_notify` (lead-found gap: bin/relay's own `desktop_nudge`,
    the round-3-packet nudge, skipped straight to osascript, disagreeing with the documented chain
    — README "Auto-wake and notifications" — for no reason beyond having been written separately).
    Both now call this ONE function, so a lead's tab-native OSC banner (zero deps, native
    click-to-focus) fires for every relay notification, not only Stop-hook wakes.

    1. iTerm native (OSC 777, written straight to the lead's own tty) — zero external deps, and
       clicking it focuses the POSTING session natively. `tty` (from the caller's marker — the tty
       recorded ONCE at arm/re-arm time, backlog row 91) is used directly when given, so a normal
       banner never needs a live AppleScript call at all; only when `tty` is absent does this fall
       back to resolving `iterm_session` via `iterm.tty_by_id` (the pre-row-91 behaviour, and still
       what a caller with no marker handy — or an old marker armed before this field existed — gets).
       Returns regardless of whether the write itself succeeds (best-effort/never-raises — the point
       of a tier system, not a retry) — UNLESS the tty never resolved / the lookup raised / the write
       itself failed, in which case this falls through to tier 2 (see `banner_fallback` below).
    2. osascript's built-in `display notification` — same info, NOT clickable, no coalescing.
       Always available on macOS, so this is the unconditional fallback (no PATH probe needed).

    Every fall-through to tier 2 that happened because tier 1 was EXPECTED to work (a `tty` or
    `iterm_session` was actually given) appends one `banner_fallback` ledger event — `state_root`,
    `reason` one of "no-tty" (neither a given tty nor a resolvable one), "tty-lookup-failed"
    (`iterm.tty_by_id` raised) or "write-failed" (a tty resolved but `notify_via_tty` returned
    False) — so the rate `relay doctor` reports is real, not a guess. `state_root` is optional and
    additive: a caller that doesn't pass it (or passes no `lead_sid`) just loses that visibility,
    never breaks — ledgering is best-effort like everything else here. No ledger event when neither
    `tty` nor `iterm_session` was given at all (a Terminal.app lead, or a caller with no marker) —
    tier 1 was never on the table, so that isn't a "fallback".

    `lead_sid` and `group` stay in the signature for callers that still pass them, but neither is
    used by the osascript tier: it has no click-action and no grouping/coalescing concept (unlike
    the retired terminal-notifier tier's `-execute`/`-group`).

    Honours `notify_on_wake` (the CALLER checks this — both call sites do, before building
    title/subtitle/message at all) is NOT re-checked here; RELAY_NO_NOTIFY IS checked here
    directly, so every caller gets the kill-switch for free. Failures swallowed throughout; never
    raises."""
    if os.environ.get("RELAY_NO_NOTIFY"):
        return  # kill-switch: the test suite sets this so a hook/CLI run never fires a REAL
                #             desktop banner (osascript has no dry-run). Also usable in CI.
    notify_via = (cfg or {}).get("notify_via", "auto")
    if notify_via == "terminal-notifier":  # legacy config value (pre-drop) — treat as osascript
        notify_via = "osascript"
    if (tty or iterm_session) and notify_via != "osascript":
        resolved_tty = tty
        reason = None
        if not resolved_tty:
            try:
                import iterm
                resolved_tty = iterm.tty_by_id(iterm_session)
            except Exception:
                reason = "tty-lookup-failed"
        if reason is None:
            if not resolved_tty:
                reason = "no-tty"
            else:
                try:
                    import iterm
                    posted = iterm.notify_via_tty(resolved_tty, title, subtitle + " — " + message[:180])
                except Exception:
                    posted = False
                if posted:
                    return
                reason = "write-failed"
        if state_root is not None:
            append_ledger(state_root, "banner_fallback", session_id=lead_sid, reason=reason)
    try:
        # Tier 2 / only tier without iTerm: macOS's built-in banner via osascript. Same information
        # but degraded: NOT clickable and no per-lead coalescing.
        def q(s):
            return (s or "").replace("\\", "\\\\").replace('"', '\\"')
        script = (f'display notification "{q(subtitle + " — " + message[:180])}" '
                  f'with title "{q(title)}" sound name "Glass"')
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


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
        marker = read_marker(state_root, session_id)
        if not marker:
            return False   # present but unreadable/empty → "any error" → not armed (fail open)
        return not is_tombstoned(marker)
    except Exception:
        return False


def _atomic_write_json(path, obj):
    """Write `obj` as indented JSON to `path` via a sibling tmp file + os.replace, never a
    straight in-place `write_text` — the reachable path behind BUG-lib-8/BUG-hooks-1: a crash, a
    full disk, or a kill mid-write left a truncated/corrupt marker, which pre-fix `is_lead` read as
    an ARMED lead (every other reader treated the same file as absent). Same shape as bin/relay's
    own `auto_trust`'s ~/.claude.json writer."""
    path = Path(path)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def write_marker(state_root, session_id, model=None, iterm_session=None, project=None, cwd=None,
                 tab_label=None, color=None, plugin_version=None, stop_hook_timeout=None,
                 predecessor=None, started=None, backend=None, autonomous=False,
                 autonomous_source="config", lineage_started=None, tier="auto",
                 tier_source="config", tty=None):
    d = lead_dir(state_root, session_id)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(marker_path(state_root, session_id), {
        "session_id": session_id,
        "project": project,          # human-readable project name (defaults to cwd basename at call site)
        "cwd": cwd,                  # where a restored lead should reopen
        "tab_label": tab_label,      # stable relay-controlled tab title → makes `relay focus <lead>` work
        "color": color,              # [r,g,b] tab color; this lead's executors inherit it at spawn
        "last_active": now(),        # heartbeat — refreshed on every write_marker call
        "started": started or now(), # preserved across re-arms by callers that read the existing marker first
        # When the FIRST lead of this lineage started — inherited unchanged through every handoff
        # (cmd_handoff passes the outgoing lead's own lineage), and preserved across re-arms exactly
        # like `started`/`predecessor`. `started` is per-session and a successor's is always brand
        # new, so it can't answer "where in the tab bar has this lead's group always sat" — which is
        # what `relay tidy` orders lead groups by, so a handoff doesn't teleport a whole group to
        # the end of the tab bar. None for a lead that never came through a handoff (its own
        # `started` is its lineage's).
        "lineage_started": lineage_started,
        "model": model,
        "iterm_session": iterm_session,  # $TERM_SESSION_ID — recorded tab metadata (debugging)
        # The lead's own tty, captured ONCE at arm/re-arm time (backlog row 91) — see
        # `_capture_tty`. `notify_banner` tier 1 uses this directly, only falling back to a live
        # `iterm.tty_by_id` lookup (the sole pre-row-91 path, and what a marker armed before this
        # field existed still gets) when it's absent. Additive/best-effort: None is a normal,
        # fully-supported value, not an error.
        "tty": tty,
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
        # Model-tier posture (`relay tier`) — who decides which model an executor runs on: "auto"
        # (the lead decides per packet, today's behaviour), "manual" (the human decides at every
        # spawn/rotate/upgrade), or "lead" (executors mirror this lead's own model class). Same
        # arm-time-reset contract as `autonomous` right above it — deliberately written on EVERY
        # arm, never preserved, so a fresh `lead-start` always resets to "auto" rather than letting
        # a posture silently outlive the plan it was scoped to. Unlike `autonomous` there is no
        # config-level default to inherit: every arm starts "auto"/"config" plain.
        "tier": tier if tier in ("auto", "manual", "lead") else "auto",
        "tier_source": tier_source,
    })


def read_marker(state_root, session_id):
    """The marker dict, or `{}` when absent, unreadable, OR valid JSON that isn't a dict (a bare
    array/string/number/null — BUG-lib-8's repro covers this shape too: every caller downstream
    treats the marker as a dict, so a non-dict parse is "any error" just as much as a JSON
    exception is). Never raises."""
    try:
        p = marker_path(state_root, session_id)
        if not p.exists():
            return {}
        m = json.loads(p.read_text())
        return m if isinstance(m, dict) else {}
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


TIER_POSTURES = ("auto", "manual", "lead")


def tier_state(marker):
    """This lead's model-tier posture as `(tier, source)` — `(str, "config" | "command")`. Mirrors
    `autonomous_state` exactly: read from the marker alone (write_marker's own default is the single
    source of truth an arm starts from), so a lead armed before this feature existed, or any
    unrecognized/missing value, reads as `("auto", "config")` — the same posture a fresh arm
    stamps, never a crash."""
    if not isinstance(marker, dict):
        return ("auto", "config")
    t = marker.get("tier")
    src = marker.get("tier_source")
    return (t if t in TIER_POSTURES else "auto",
            src if src in ("config", "command") else "config")


def set_tier(state_root, session_id, tier):
    """Flip a lead's model-tier posture (read-modify-write, preserving every other marker field) and
    stamp its source as "command" — the `set_autonomous` sibling for `relay tier`. Returns True when
    the marker was updated, False when there is no marker to update."""
    if tier not in TIER_POSTURES:
        return False
    m = read_marker(state_root, session_id)
    if not isinstance(m, dict) or not m:
        return False
    m["tier"] = tier
    m["tier_source"] = "command"
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
        _atomic_write_json(marker_path(state_root, session_id), m)
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
    item is normally the marker dict exactly as stored. Fully defensive: config.json and any
    non-marker entry are skipped, and no input ever raises.

    D2 (BUG-lib-8/BUG-hooks-1's mitigation): a marker that EXISTS but can't be read as a real dict
    (JSON error, or valid JSON that isn't a non-empty object — the same "any error" `is_lead` now
    fails open on) used to be silently dropped here — invisible on the one surface (`relay list`)
    that exists to show lead state. It is now a DISTINCT broken row instead:
    `{"session_id": <dir name>, "broken": True}` — this is the always-visible LEADS surface, so a
    bad marker must never just vanish, only ever show up as something a human can act on."""
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
            except Exception:
                m = None
            if isinstance(m, dict) and m:
                out.append(m)
            else:
                out.append({"session_id": d.name, "broken": True})
    except Exception:
        return out
    # Sort oldest-first; a marker missing `started` sorts as "" (first) rather than crashing — a
    # broken row (no `started` at all) sorts alongside any other marker missing the field.
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


def _notify_marker_change(state_root, marker, verb, reason):
    """LOUD desktop banner for a marker migration or tombstone — lead-found gap: both used to be
    silent, so a hijack (THE INCIDENT, 2026-09-05 22:18:50) or an ordinary pause could leave a lead
    dark (no wakes, no gate) with nothing on screen saying why. `verb` is "migrated" or "ended";
    `marker` is the OLD marker (its `iterm_session`/`project` — the tab that just lost its arming,
    not whatever tab the new/surviving session happens to be in). RELAY_HEADLESS=1 (every headless
    `claude -p` probe relay itself launches) skips this entirely — a probe reading/migrating state
    is not a human at a tab who needs telling; `notify_banner` itself already honours
    RELAY_NO_NOTIFY. Never raises; a failed banner must never turn a successful migrate/tombstone
    into a failed one."""
    if os.environ.get("RELAY_HEADLESS") == "1":
        return
    try:
        cfg = load_config(state_root)
        project = (marker or {}).get("project") or "lead"
        iterm_session = (marker or {}).get("iterm_session")
        title = "relay: lead %s %s" % (project, verb)
        subtitle = reason or verb
        message = "%s — run /relay:mode to re-arm" % (reason or verb)
        notify_banner(cfg, title, subtitle, message, lead_sid=(marker or {}).get("session_id"),
                     iterm_session=iterm_session, tty=(marker or {}).get("tty"),
                     state_root=state_root)
    except Exception:
        pass


def tombstone_lead(state_root, session_id, reason=None, now_ts=None, notify=True):
    """Mark a lead ended-but-resumable instead of deleting it. Retains every other field so
    revive_lead() is lossless.

    THE INCIDENT (lead-found, 2026-09-05 20:16): a LIVE lead's marker was tombstoned during a
    `/reload-plugins` churn with `ended: true`, no reason recorded, and NO ledger event — gate,
    wake and auto posture all went dark unannounced until the lead noticed by accident. The missing
    ledger event is fixed HERE rather than left to each caller to remember: every successful
    tombstone appends its OWN `lead_tombstoned` ledger event (session_id, reason), and `reason` (a
    short caller-supplied label — SessionEnd's own reason string, or "migrated" for migrate_lead's
    call) is stored as `ended_reason` on the marker for the same forensic visibility. Deciding
    WHETHER a missing/unknown SessionEnd reason should even reach this function is the CALLER's
    policy (see hooks/sessionend_lead_cleanup.py's own reason dispatch, which never calls this
    without one of its two recognized pause reasons) — this function stays the general-purpose
    tombstone primitive migrate_lead and others also rely on, so it does not itself refuse a call
    with no reason; it just no longer loses the fact that one wasn't given.

    Also fires the LOUD `_notify_marker_change` desktop banner on every successful tombstone
    UNLESS `notify=False` — `migrate_lead` passes that (it already fires its own "migrated" banner
    for the same event; without it a single migration would pop two banners, one saying "migrated"
    and a second, more alarming one saying "ended", for what the lead should see as ONE thing that
    happened).

    Returns True if a marker was actually tombstoned (no marker, or an already-tombstoned one,
    returns False so callers can stay quiet). Never raises."""
    try:
        m = read_marker(state_root, session_id)
        if not m or is_tombstoned(m):
            return False
        m["ended"] = True
        m["ended_reason"] = reason
        m["ended_at"] = now() if now_ts is None else time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(now_ts))
        marker_path(state_root, session_id).write_text(json.dumps(m, indent=2))
        append_ledger(state_root, "lead_tombstoned", session_id=session_id, reason=reason)
        if notify:
            _notify_marker_change(state_root, m, "ended", reason or "tombstoned")
        return True
    except Exception:
        return False


def revive_lead(state_root, session_id):
    """Re-arm a tombstoned lead (SessionStart source="resume"): drop the tombstone flags and refresh
    last_active. Everything else — project name included — is restored untouched, so a resumed lead
    is indistinguishable from one that never exited. Returns True ONLY if a tombstone was actually
    revived, so a plain fresh start stays a silent no-op. Never raises.

    Row 89/91 follow-up: an iTerm restart gives every restored tab a NEW session UUID while a
    tombstoned marker keeps the old one, so `_lead_alive`/`relay focus`/tidy/tier-1 banners all
    quietly miss the (still very much alive) resumed lead. This process inherits the SAME
    environment the SessionStart hook that called us was invoked with, so when the live
    `$ITERM_SESSION_ID`/`$TERM_SESSION_ID` looks like a real iTerm handle (`LIVE_ITERM_SESSION_RE`
    — the exact shape check bin/relay's `_live_lead_handle` trusts) AND differs from what's
    recorded, this refreshes `iterm_session` and re-captures `tty` (`_capture_tty`) in the SAME
    write. Left untouched when it matches (the common case — no needless AppleScript call on every
    ordinary resume) or when the live value isn't a real iTerm handle at all (Terminal.app, or a
    revive that isn't happening from inside the lead's own tab). Additive to the caller's contract:
    still no ledger event for the refresh itself, exactly as before."""
    try:
        m = read_marker(state_root, session_id)
        if not m or not is_tombstoned(m):
            return False
        m.pop("ended", None)
        m.pop("ended_at", None)
        m.pop("ended_reason", None)
        m["last_active"] = now()
        live = os.environ.get("ITERM_SESSION_ID") or os.environ.get("TERM_SESSION_ID")
        if live and LIVE_ITERM_SESSION_RE.match(live) and live != m.get("iterm_session"):
            m["iterm_session"] = live
            m["tty"] = _capture_tty(live)
        marker_path(state_root, session_id).write_text(json.dumps(m, indent=2))
        return True
    except Exception:
        return False


# ---- migration: lead re-arming survives a session-id change --------------------------------------
# THE INCIDENT (memory: relay-lead-id-changes-on-resume.md): a resumed lead's OWN
# $CLAUDE_CODE_SESSION_ID can change out from under it (observed live: a background-job resume
# reported a different id than the one the lead armed under) while its iTerm TAB is unchanged.
# revive_lead is keyed on the session id matching exactly, so a resume that comes back under a
# NEW id finds no marker/tombstone at all and silently does nothing — gate, wake, auto posture and
# the 🚦 statusline segment all go dark, with no error anywhere, because every hook's own
# `is_lead`/`read_marker` check is correctly reporting "no marker for this id" (it's just the wrong
# question — the id changed, the LEAD didn't). find_lead_by_tab + migrate_lead below answer the
# right question instead: "is there a lead marker for the tab this process is actually running in?"

def _tty_by_id(iterm_session_id):
    """/dev/ttysNNN for an iTerm session id ($TERM_SESSION_ID, "w#t#p#:UUID"), or None — a thin,
    mockable indirection over scripts/iterm.tty_by_id (lazily imported: lead_guard stays free of a
    hard terminal-backend dependency, matching every other function in this file). Tests monkeypatch
    THIS function directly rather than reaching into iterm's AppleScript/subprocess plumbing. Any
    failure (module missing, AppleScript failure, no live match) degrades to None. Never raises."""
    try:
        scripts_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import iterm as _iterm
        return _iterm.tty_by_id(iterm_session_id)
    except Exception:
        return None


# Same shape check bin/relay's `_live_lead_handle` uses to trust a live $ITERM_SESSION_ID/
# $TERM_SESSION_ID value (row 89) — reused here so a re-arm's "does the live tab differ from what's
# recorded" comparison and that function's own live-tab check can never quietly drift apart.
LIVE_ITERM_SESSION_RE = re.compile(r"^w\d+t\d+p\d+:[0-9A-Fa-f-]{36}$")


def _capture_tty(iterm_session):
    """The lead's own tty, captured ONCE at arm/re-arm time — off the load path `notify_banner`'s
    tier 1 otherwise has to hit on every single banner (backlog row 91: an AppleScript lookup that
    misbehaves while iTerm is busy tidying/spawning silently degraded a clickable iTerm banner to
    an un-clickable macOS one). Prefers the AppleScript session→tty lookup (`_tty_by_id`) — arming
    is not a busy moment, so it's trusted here — and falls back to this process's own controlling
    tty (`os.ttyname(0)`) when that fails or there's no `iterm_session` at all. Returns None when
    neither resolves (e.g. called from inside a hook subprocess, whose stdin is a JSON payload
    pipe, not a terminal — the caller keeps whatever `tty` it already had). Never raises."""
    tty = None
    if iterm_session:
        try:
            tty = _tty_by_id(iterm_session)
        except Exception:
            tty = None
    if not tty:
        try:
            tty = os.ttyname(0)
        except Exception:
            tty = None
    return tty


def _own_ancestor_pids():
    """The set of pids in THIS process's own ancestry: its own pid, `os.getppid()`, and every
    pid above that walked via `ps -o ppid=` up to (but not including) pid 1 or a repeat/lookup
    failure. Exists so `_tab_has_live_claude` can tell "the very process whose SessionStart hook
    is asking" (and whichever of ITS OWN ancestors happens to be the `claude` binary) apart from a
    genuinely different `claude` process that merely shares the tab's tty. Best-effort: any `ps`
    failure just truncates the chain where it stands rather than raising — a short chain only
    makes the live-check MORE conservative (more pids read as "foreign"), never less safe."""
    pid = os.getpid()
    chain = {pid}
    for _ in range(64):  # a hard cap — real ancestry chains are a handful of pids deep
        try:
            r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=5)
        except Exception:
            break
        if r.returncode != 0:
            break
        out = (r.stdout or "").strip()
        if not out:
            break
        try:
            ppid = int(out)
        except ValueError:
            break
        if ppid <= 1 or ppid in chain:
            break
        chain.add(ppid)
        pid = ppid
    return chain


def _tab_has_live_claude(iterm_session_id):
    """True when the tab identified by `iterm_session_id` ($TERM_SESSION_ID) still has a `claude`
    process attached to its tty THAT IS NOT this calling process's own ancestor — a thin, mockable
    indirection over scripts/iterm.pids_on_tty, mirroring `_tty_by_id` above (tests monkeypatch
    this function directly for `lead_still_live`/`safe_migrate_by_tab` coverage, and exercise it
    directly with `ps` mocked for the ancestry-exclusion behavior itself).

    Without the ancestry check this returned True for ANY `claude` on the tty, including the very
    process whose SessionStart hook is asking — so a genuine same-tab `claude --resume` (the
    0.3.50 "lead rearm survives a session-id change" feature) always saw itself on the tty and
    `lead_still_live` refused its own legitimate migration, every time, in real life (the unit
    tests only passed because this helper degrades to False in the sandboxed test environment,
    where `ps`/AppleScript don't resolve at all). Excluding this process's own ancestry fixes that:
    in a genuine resume the old lead process has already exited, so the only `claude` left on the
    tty is an ancestor of the resumed process itself — not foreign, so not live. In THE INCIDENT
    this guards against (see `lead_still_live`), the original lead is a SEPARATE, still-running
    process, not an ancestor of the probe checking it — so it still reads as foreign and live.

    Any failure (module missing, AppleScript failure, no live match, no session id at all)
    degrades to False. Never raises. See `lead_still_live`'s docstring for what this guards
    against."""
    if not iterm_session_id:
        return False
    try:
        tty = _tty_by_id(iterm_session_id)
        if not tty:
            return False
        scripts_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import iterm as _iterm
        own = _own_ancestor_pids()
        foreign = [p for p in _iterm.pids_on_tty(tty) if p not in own]
        return bool(foreign)
    except Exception:
        return False


def lead_still_live(marker, poll_seconds):
    """True when `marker` (a `find_lead_by_tab` migrate CANDIDATE's own, OLD marker) looks like a
    real, currently-running lead rather than one whose process has actually exited — the guard
    behind THE INCIDENT (2026-09-05 22:18:50, ledger `lead_migrated old=4dff0f10... new=4a75a4a9...`
    then `session_end 4a75a4a9... reason=other was_lead=true`): `cmd_spawn`'s model-alias probe (a
    headless `claude -p` relay itself launches) ran from the LEAD's own shell, inheriting its
    $TERM_SESSION_ID and cwd — while the real lead was still mid-turn, blocked on that very
    subprocess. Its SessionStart hook matched the live lead's tab+cwd and migrated the marker onto
    the throwaway probe, tombstoning the real, still-running lead.

    Deliberately narrower than the `last_active`-freshness check the fix note first reached for:
    a marker's `last_active` is re-stamped on EVERY lead turn (including its very last one before a
    genuine exit), so "still fresh" is true of almost any recently-used lead whether or not its
    process has actually exited — checking it here would block the ordinary, legitimate
    id-changed-on-resume migration this same code path exists to perform (0.3.50's own
    `TestSessionStartRearmMigration`), not just the incident. `_tab_has_live_claude` instead asks
    the one question that actually distinguishes them: is a `claude` process STILL attached to the
    old marker's tab right now? In the incident, yes (the original lead is mid-turn). In a genuine
    resume, the old process has already exited, so only the NEW session's own (not-yet-migrated)
    process is there — nothing pre-existing to protect. See the report for why the `poll_seconds`
    half of the originally-proposed guard was dropped rather than implemented as literally spec'd.

    Never raises; any error here means "can't prove it's safe" → returns True (don't migrate)."""
    try:
        return _tab_has_live_claude((marker or {}).get("iterm_session"))
    except Exception:
        return True


def safe_migrate_by_tab(state_root, iterm_session, cwd, new_sid, poll_seconds):
    """`find_lead_by_tab` + `migrate_lead`, guarded by `lead_still_live` — THE ONE path both hooks
    that migrate-by-tab (SessionStart's id-changed-on-resume revive; Stop's is_lead fallback) call,
    so the incident guard lives in exactly one place. Ledgers `lead_migrate_refused_live` (never
    `migrate_lead`, so a refused attempt is distinguishable from "no candidate at all") when a
    candidate is found but looks still-live. Returns the OLD session id on a successful migration,
    else None. Never raises."""
    try:
        old_sid = find_lead_by_tab(state_root, iterm_session=iterm_session, cwd=cwd)
        if not old_sid:
            return None
        if lead_still_live(read_marker(state_root, old_sid), poll_seconds):
            append_ledger(state_root, "lead_migrate_refused_live", session_id=new_sid, old_sid=old_sid)
            return None
        return old_sid if migrate_lead(state_root, old_sid, new_sid) else None
    except Exception:
        return None


def find_lead_by_tab(state_root, iterm_session=None, tty=None, cwd=None, log_ambiguous=True):
    """PURE lookup (reads lead markers only; never writes a marker, never migrates): the ONE lead
    marker whose recorded `iterm_session` identifies the CURRENT tab.

    Two ways to identify "current": `iterm_session` matches a candidate's stamped iterm_session by
    exact string equality — the fast, common path, since it's the very value write_marker recorded
    at arm time and what a hook/CLI process inherits via $TERM_SESSION_ID with no subprocess call at
    all. `tty` instead resolves EACH candidate's stamped iterm_session to a live tty path
    (_tty_by_id) and compares that against `tty` — for a caller that only knows the tty, not the
    iTerm session id. Give exactly one of the two; with neither, returns None untouched.

    Fix-list 002 (the gap: a tab match ALONE is not proof of identity — a tombstoned lead's
    `iterm_session` stays recorded forever, so a brand-new, wholly unrelated session started later
    in that same physical tab would otherwise "inherit" it). When `cwd` is given, a candidate must
    ALSO satisfy `os.path.realpath(candidate["cwd"]) == os.path.realpath(cwd)` — a candidate with no
    recorded cwd never matches once `cwd` is given, since it can't prove same-project either way.
    Callers that can't establish a cwd (or a hook whose payload carries none) should pass `cwd=None`
    AND independently refuse to migrate at all — this function has no way to distinguish "caller
    doesn't have a cwd" from "caller doesn't care", so that refusal is the caller's job (both
    hooks below guard their whole migrate attempt on the payload actually carrying a cwd).

    A marker already carrying `migrated_to` is excluded — it has already been superseded by a later
    migration, so matching it again would hand back a lead identity that has already moved on (the
    double-hop case: tab X migrates old→mid, then later mid→new — `new`'s lookup must land on `mid`,
    not resurrect the already-migrated-away `old`).

    Returns the matched marker's session_id, or None when nothing matches. Two or more MATCH is
    ambiguous: rather than guess which one is "the" lead, this returns None and (when
    `log_ambiguous`, the default) appends a `lead_migrate_ambiguous` ledger event naming every
    candidate — so the collision is visible for forensics instead of silently picking one. A caller
    that must stay strictly side-effect-free (e.g. the statusline path) passes `log_ambiguous=False`
    to skip even that ledger write. Never raises."""
    if not iterm_session and not tty:
        return None
    try:
        cwd_real = os.path.realpath(cwd) if cwd else None
        matches = []
        for m in list_leads(state_root):
            sid = m.get("session_id")
            cand = m.get("iterm_session")
            if not sid or not cand or m.get("migrated_to"):
                continue
            if iterm_session is not None:
                hit = cand == iterm_session
            else:
                hit = _tty_by_id(cand) == tty
            if not hit:
                continue
            if cwd_real is not None:
                m_cwd = m.get("cwd")
                if not m_cwd or os.path.realpath(m_cwd) != cwd_real:
                    continue
            matches.append(sid)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and log_ambiguous:
            append_ledger(state_root, "lead_migrate_ambiguous", candidates=matches,
                          iterm_session=iterm_session, tty=tty)
        return None
    except Exception:
        return None


def find_lead_by_project(state_root, project):
    """The single CURRENTLY-ARMED lead whose marker's project matches `project` exactly, or None —
    zero matches, or more than one (ambiguous — never guessed, same rule find_lead_by_tab already
    follows), both return None.

    The fallback owner for hooks/executor_escalation.py's push (lead-found, 2026-09-05 22:01): an
    executor's recorded `owner_lead` can go missing with no handoff to re-parent it (a crash, a
    manual close) — a human who simply re-spawns a fresh lead for the SAME project should still be
    reachable by that executor's report, without this ever guessing between two same-project leads.
    Never raises."""
    if not project:
        return None
    try:
        matches = [m.get("session_id") for m in list_leads(state_root)
                  if not m.get("broken") and m.get("project") == project and not is_tombstoned(m)]
        return matches[0] if len(matches) == 1 else None
    except Exception:
        return None


def migrate_lead(state_root, old_sid, new_sid):
    """Migrate a lead's identity from old_sid to new_sid — the fix for THE INCIDENT above: a resumed
    lead came back under a different session id than the one its marker lives under.

    Writes a NEW marker.json at new_sid: a copy of old_sid's marker (so project/cwd/iterm_session/
    color/predecessor/autonomous posture all carry over untouched — a migrated lead is
    indistinguishable from one that never changed id) with `session_id` corrected, `migrated_from`/
    `migrated_at` stamped, and any tombstone flags dropped (a migration IS a revive: the new id
    starts armed). Also moves the auxiliary per-lead files that would otherwise strand a resumed
    lead's memory of its own state: surfaced_reports.json (don't re-announce reports it already
    saw), handoff_nudged (don't re-nudge a handoff it already got), grace_until (don't drop an
    in-progress /relay:route window).

    Rewrites `owner_lead` on every executor's session.json that pointed at old_sid, so their
    wake/adopt paths keep targeting the lead that's actually still there. Tombstones old_sid
    (kept for audit, not deleted — mirrors a resumable SessionEnd, NOT `close --self`, which fully
    deletes; see this task's report for why the packet's own wording pointed at the wrong function)
    and stamps it `migrated_to: new_sid` so find_lead_by_tab won't match it again on a later hop.
    Appends ONE `lead_migrated` ledger event naming every re-parented executor.

    Idempotent: if new_sid already has a marker stamped `migrated_from == old_sid`, this is a silent
    no-op (returns True — the migration already happened, nothing left to do). Returns False (does
    nothing) when there's no old marker to migrate, old_sid == new_sid, or new_sid already carries a
    DIFFERENT marker of its own (refuses to clobber a real, independently-armed lead). Never raises
    into a caller — every caller here is a hook."""
    try:
        if not old_sid or not new_sid or old_sid == new_sid:
            return False
        old_marker = read_marker(state_root, old_sid)
        if not old_marker:
            return False
        new_marker = read_marker(state_root, new_sid)
        if new_marker:
            return new_marker.get("migrated_from") == old_sid  # already migrated → no-op; else refuse

        migrated = dict(old_marker)
        migrated["session_id"] = new_sid
        migrated["migrated_from"] = old_sid
        migrated["migrated_at"] = now()
        migrated["last_active"] = now()
        migrated.pop("ended", None)
        migrated.pop("ended_at", None)
        migrated.pop("ended_reason", None)
        new_dir = lead_dir(state_root, new_sid)
        new_dir.mkdir(parents=True, exist_ok=True)
        marker_path(state_root, new_sid).write_text(json.dumps(migrated, indent=2))

        old_dir = lead_dir(state_root, old_sid)
        for name in ("surfaced_reports.json", "handoff_nudged", "grace_until"):
            src = old_dir / name
            if src.exists():
                try:
                    shutil.move(str(src), str(new_dir / name))
                except Exception:
                    pass  # best-effort — losing a nudge/surfaced flag is a soft regression, not fatal

        moved_execs = []
        root = Path(state_root)
        if root.exists():
            for d in root.iterdir():
                sj = d / "session.json"
                if not sj.exists():
                    continue
                try:
                    s = json.loads(sj.read_text())
                except Exception:
                    continue
                if s.get("owner_lead") != old_sid:
                    continue
                s["owner_lead"] = new_sid
                s["updated"] = now()
                sj.write_text(json.dumps(s, indent=2))
                moved_execs.append(s.get("session_id") or d.name)

        tombstone_lead(state_root, old_sid, reason="migrated", notify=False)  # own banner below —
                                                                               # see tombstone_lead's
                                                                               # `notify` docstring
        update_marker(state_root, old_sid, migrated_to=new_sid)

        append_ledger(state_root, "lead_migrated", old=old_sid, new=new_sid, executors=moved_execs)
        _notify_marker_change(state_root, old_marker, "migrated", "migrated to a new session id")
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


# ---- 8b: ONE desktop banner per (executor, packet) (lead-found duplicate-banner incident) -------
# THE INCIDENT: a user saw BOTH an iTerm banner and an osascript banner for the SAME
# executor report. Two producers can legitimately fire for the same report: the lead's own
# Stop-hook wake (stop_lead_watch.py's _notify, via _announce_and_wake) and the executor's own
# escalation push (executor_escalation.py, which also now attempts a banner alongside its
# `nudge-lead` text injection — see that hook). Both key off the SAME "has this lead already seen
# this report" question, but ask it at different times: the wake's OWN promotion from pending to
# surfaced (mark_pending → promote_pending, proven-delivery only) lags behind the banner it just
# fired, so an escalation push racing in that exact window sees "not yet surfaced" and fires too.
#
# `claim_notification` is the fix: a tiny stamp file, `notified.json`, under the LEAD's own state
# dir (never the executor's — the lead is the one entity both producers already agree on and can
# both reach by owner_lead), keyed by "<executor>:<packet>". Whichever producer asks FIRST gets
# True (fire the banner); every later ask for the same key gets False (stay quiet). Distinct from
# surfaced_reports.json on purpose: that one means "proven delivered", stamped only later and by
# fewer paths; this one means "a banner was already ATTEMPTED for this", stamped immediately by
# whichever producer gets there first, which is exactly the timing this race needs.

def _notified_path(state_root, lead_sid):
    return lead_dir(state_root, lead_sid) / "notified.json"


def claim_notification(state_root, lead_sid, key, now_ts=None):
    """Claim the ONE desktop-banner slot for `key` ("<executor>:<packet>") under `lead_sid`'s state
    dir. Returns True the FIRST time `key` is claimed anywhere (the caller should fire the banner),
    False on every later call for the same key (already claimed by the other producer — skip the
    banner; nothing else about that producer's own work is affected). Best-effort and FAILS TOWARD
    True: a broken/unreadable stamp file must never silently swallow a legitimate notification —
    the cost of an occasional duplicate banner is far smaller than a missed one."""
    try:
        p = _notified_path(state_root, lead_sid)
        data = {}
        if p.exists():
            try:
                loaded = json.loads(p.read_text())
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        if key in data:
            return False
        data[key] = now() if now_ts is None else now_ts
        d = lead_dir(state_root, lead_sid)
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(p, data)
        return True
    except Exception:
        return True


# ---- #22: announced-but-unproven wakes (§13's lost-wake bug) -----------------------------------
# The bug: the Stop hook stamped `surfaced` the moment it ANNOUNCED, then relied on its exit-2
# reaching the lead. A firing that can't be delivered (the lead is mid-turn, so the harness drops a
# stale hook's exit-2) still kept the stamp, and the report was never announced again — silently
# swallowed. Mirror image of #17: that stamped too little (duplicate wakes), this stamps too early
# (lost wakes), which is strictly worse.
#
# The fix is a two-phase stamp. An announce records the keys as PENDING, which does NOT suppress a
# later announce — so an undelivered wake naturally retries on the lead's next Stop. Only PROVEN
# delivery promotes pending → surfaced; if the wake was dropped, the key stays pending and the next
# Stop re-announces it. (What counts as proof was #22's one mistake: it read the harness's global
# `stop_hook_active` flag, which any other plugin's blocking Stop hook also sets. See the #23 block
# below for relay's own receipt, which replaced it.)
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


def carry_forward_surfaced(state_root, from_sid, to_sid, only_executors=None):
    """Row 67: copy `from_sid`'s surfaced_reports.json + pending_wakes.json onto `to_sid`'s —
    `cmd_handoff`'s successor otherwise starts with NEITHER file, so a report the predecessor had
    already reviewed and committed (surfaced) or was mid-retry announcing (pending) looked brand
    new again and re-woke the successor as "NOT yet proven delivered" (the incident this row names:
    gm-signin-114240 packet 001, committed before the handoff, re-announced right after). Distinct
    from `migrate_lead`'s copy of the same two files: that one moves them for an id migration of
    the SAME lead (resume-by-tab); this one is for a handoff's genuinely NEW successor id, called
    while the predecessor's dir still exists (before `clear_lead` deletes it). Merges onto whatever
    `to_sid` already has rather than clobbering — harmless here (a freshly pre-armed successor has
    neither file yet) and safer if a future caller ever calls this onto a non-empty target.

    Row 70 item 4: `only_executors` scopes the copy to those executor ids — the shape an ADOPTION
    needs, where ONE executor changes hands (`_maybe_adopt` / `_reparent_executors`) and importing
    the old owner's whole history onto the new one would be wrong. Keys are "<executor>:<packet>",
    so the scoping is a prefix match on the key's executor half. None (the handoff case) copies
    everything, unchanged. Best-effort; never raises into a caller."""
    try:
        wanted = None if only_executors is None else {str(e) for e in only_executors}

        def keep(key):
            return wanted is None or str(key).rsplit(":", 1)[0] in wanted

        surfaced = {k for k in load_surfaced(state_root, from_sid) if keep(k)}
        if surfaced:
            mark_surfaced(state_root, to_sid, surfaced)
        pending = {k: v for k, v in load_pending(state_root, from_sid).items() if keep(k)}
        if pending:
            cur = load_pending(state_root, to_sid)
            cur.update(pending)
            _save_pending(state_root, to_sid, cur)
    except Exception:
        pass


# ---- #23: WHOSE continuation is this? (relay's own delivery receipt) ---------------------------
# Field incident 2026-07-22 (~/.relay-tasks/incident-wake-miss-2026-07-22.md — diagnosis by the
# field lead, credited): #22 above read the harness's GLOBAL `stop_hook_active` flag as proof that
# RELAY's wake was delivered. It is no such thing — Claude Code sets that flag whenever the session
# was continued by ANY blocking Stop hook, and this environment runs a second one (a personal
# rules-check that blocks on edit turns). So every post-block turn looked like relay's own post-wake
# re-run: the synchronous announce was suppressed AND never-delivered pending wakes were stamped
# delivered. Two executor reports sat silent for ~2 hours.
#
# The fix: relay keeps its OWN receipt-in-waiting. Every announce records an announce CLAIM (nonce +
# the transcript's byte offset at announce time). A stop_hook_active run promotes pending only when
# that claim is outstanding AND relay's own wake text actually landed in the transcript past that
# offset — a delivered wake is written into the lead's transcript verbatim (observed: as a
# queue-operation/user entry carrying this hook's stderr). No outstanding claim → somebody else's
# continuation → treat it as an ordinary Stop. The needle is deliberately ASCII: transcripts are
# JSON and escape the 🚦 as 🚦.
WAKE_DELIVERY_NEEDLE = "new activity while you were idle"
_TRANSCRIPT_SCAN_MAX = 4 * 1024 * 1024  # never read more than this much transcript tail


def _announce_claim_path(state_root, lead_sid):
    return lead_dir(state_root, lead_sid) / "announce_claim.json"


def load_announce_claim(state_root, lead_sid):
    """The outstanding announce claim ({} when relay has no un-consumed announce out)."""
    try:
        p = _announce_claim_path(state_root, lead_sid)
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def clear_announce_claim(state_root, lead_sid):
    try:
        p = _announce_claim_path(state_root, lead_sid)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def record_announce_claim(state_root, lead_sid, kind, transcript_path=None):
    """Stamp relay's receipt-in-waiting for an announce that is firing right now. `kind` is "sync"
    (announced while answering a live Stop event) or "async" (the background poller's late exit-2,
    the one the harness can drop). Records where the transcript ENDS at this moment, so the later
    delivery check only ever matches text written after this announce."""
    claim = {"kind": kind, "at": time.time(), "nonce": f"{int(time.time() * 1000)}-{os.getpid()}",
             "transcript": str(transcript_path or ""), "offset": 0}
    try:
        if transcript_path:
            claim["offset"] = os.path.getsize(transcript_path)
    except Exception:
        pass
    try:
        d = lead_dir(state_root, lead_sid)
        d.mkdir(parents=True, exist_ok=True)
        _announce_claim_path(state_root, lead_sid).write_text(json.dumps(claim, indent=2))
    except Exception:
        pass
    return claim


def _transcript_has(path, offset, needle):
    """Does `needle` appear in `path` after byte `offset`? Bytes-level substring search — the
    transcript is JSONL and this deliberately makes no assumption about its entry shape (the wake
    has been seen as both a queue-operation and a user entry). Reads at most the last
    _TRANSCRIPT_SCAN_MAX bytes; a shrunken/rotated file falls back to scanning that tail."""
    try:
        size = os.path.getsize(path)
        start = offset if isinstance(offset, int) and 0 <= offset <= size else 0
        if size - start > _TRANSCRIPT_SCAN_MAX:
            start = size - _TRANSCRIPT_SCAN_MAX
        with open(path, "rb") as f:
            f.seek(start)
            return needle.encode() in f.read()
    except Exception:
        return False


def relay_announce_delivered(state_root, lead_sid):
    """Did RELAY's own announce cause this stop_hook_active continuation? One-shot: the claim is
    consumed either way (a claim answers exactly one continuation; a retry writes a fresh one).

    True only when a claim is outstanding AND its wake text is visible in the transcript past the
    offset it recorded. When the claim has no transcript to check against (no transcript_path in the
    payload — chiefly the test harness), a "sync" claim is still trusted: that announce answered a
    live Stop event the harness was waiting on. An unprovable "async" claim reads as NOT delivered,
    which merely costs a retry (capped) — the safe side of the incident."""
    claim = load_announce_claim(state_root, lead_sid)
    clear_announce_claim(state_root, lead_sid)
    if not claim:
        return False  # some OTHER Stop hook continued this session — not relay's re-run
    path = claim.get("transcript")
    if path and os.path.exists(path):
        return _transcript_has(path, claim.get("offset", 0), WAKE_DELIVERY_NEEDLE)
    return claim.get("kind") == "sync"


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


def _executor_status(state_root, exec_sid):
    """`status` recorded in this executor's session.json (None if missing/unreadable)."""
    try:
        s = json.loads((Path(state_root) / str(exec_sid) / "session.json").read_text())
        return s.get("status")
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
        # A closed/superseded executor is a DELIBERATE "done with it" (manual close, retire, or
        # auto-close) — its reports must never nag again, even when the surfaced stamp was refused
        # (a close run from a terminal or a foreign session under the #27 invoker gate) or an older
        # packet's key was never stamped (close stamps only the current packet). Field bug
        # 2026-08-21: closed executors kept waking their lead forever. `dead` stays nag-worthy on
        # purpose — a crash with an unseen report is exactly what the wake exists for.
        if _executor_status(state_root, sid) in ("closed", "superseded"):
            continue
        key = f"{sid}:{packet}"
        if key not in surfaced:
            fresh.append((key, sid, packet, path))
    return fresh


def git_dirty_paths(worktree):
    """Paths with ANY working-tree/index change (staged, modified, untracked, renamed-to) — what
    "this executor's work is still sitting here" looks like. None on any git failure, which callers
    must read as UNKNOWN (never as clean); an empty set means a genuinely clean worktree.

    Lives here rather than in bin/relay so the auto-close sweep (`bin/relay _git_dirty_paths`, a
    thin wrapper over this) and the Stop hook's wake (`report_landed` below, which a hook script
    can import) can never disagree about what "landed" means."""
    if not worktree:
        return None
    try:
        r = subprocess.run(["git", "-C", str(worktree), "status", "--porcelain",
                            "--untracked-files=all"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        out = set()
        for line in r.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            out.add(path.strip().strip('"'))
        return out
    except Exception:
        return None


def report_landed(state_root, exec_sid, report_path):
    """Row 70 item 4, belt-and-braces half: has this executor's report ALREADY landed — is every
    path it claims under "What changed" clean in its worktree? Exactly the test auto-close's
    `landed` reason applies, asked here so the Stop-hook wake can skip a report the previous owner
    already reviewed and committed instead of re-announcing it as "review needed" (the incident in
    `relay-issues/03-surfaced-not-carried-on-adopt.md`, which cost the successor a turn per stale
    report and invited a re-review of committed work).

    Conservative by construction — False for everything it cannot PROVE: no recorded worktree, git
    unreadable, or a report that claims no paths at all (an ops report claims none, and "no claims"
    is not evidence of landing). A wake wrongly skipped is a silent report, which is worse than a
    wake wrongly fired, so every uncertainty resolves towards waking."""
    try:
        import report_verify
        worktree = read_session_json(state_root, exec_sid).get("worktree")
        if not worktree:
            return False
        claimed, _scoped = report_verify.claimed_paths(Path(report_path).read_text())
        if not claimed:
            return False
        dirty = git_dirty_paths(worktree)
        if dirty is None:
            return False
        return not (set(claimed) & dirty)
    except Exception:
        return False


def diff_size_text(worktree):
    """'(diff: N files +A/-D)' for the staged diff in `worktree`, or None when there's nothing to
    show (no worktree, non-repo, or nothing staged) — row 65 item 4: the diff's size travels next
    to every report (`relay check`, `relay list`'s reported footnote, the Stop hook's wake line) so
    a reader can see whether inline reading is affordable BEFORE opening it (the lead-context-burn
    incident this whole feature exists for). Uses `git diff --cached --numstat` so it never has to
    materialize the diff text itself — same one-line-per-file shape bin/relay's own `_diff_stat`
    reads for the auto_commit ledger event, just formatted for display here. Shared here (rather
    than living only in bin/relay) so this hook script — which has no .py extension and isn't a
    normal import target — can print the same figure in the wake line. Never raises; a git failure
    degrades to None like every other git-reading helper in this module."""
    if not worktree:
        return None
    try:
        r = subprocess.run(["git", "-C", str(worktree), "diff", "--cached", "--numstat"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        files = insertions = deletions = 0
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            if parts[0].isdigit():
                insertions += int(parts[0])
            if parts[1].isdigit():
                deletions += int(parts[1])
        return f"(diff: {files} files +{insertions}/-{deletions})" if files else None
    except Exception:
        return None


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


# ---- transport v2: peer addressing (#28 phase 1) ------------------------------------------------
# Claude Code ≥2.1.224 registers every messaging-capable session as ~/.claude/sessions/<pid>.json
# ({"pid", "sessionId", "name", "messagingSocketPath", "updatedAt", …}) and binds that socket. The
# path is the ADDRESS: an incoming peer message carries `from="uds:/tmp/cc-socks/<pid>.sock"`, and
# SendMessage accepts that exact string as `to` (verified live, executor→lead, 2026-08-08).
#
# Why address by socket and not by the `--name` the design doc assumed: names COLLIDE and a colliding
# bare name is a HARD SEND FAILURE, not a best-effort pick — `SendMessage to="[Lead] claude-relay"`
# returned "matches 2 agents. Re-send with the ref". The disambiguating `[ref]` is only ever printed
# by ListAgents/the error itself, so it can't be recorded at spawn. The registry, by contrast, is on
# disk and keyed by the very id relay already stores as `owner_lead`.
#
# The duplicate is not exotic: a lead resumed twice leaves two live pids under ONE sessionId (seen
# live — pids 6583/6646 both `claude --resume 5ab092fb…`). Hence "newest live entry wins" below.

def _sessions_registry_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "sessions"


def peer_registry_entries():
    """Every readable Claude Code session-registry record, newest-updated first. Never throws."""
    out = []
    try:
        for p in _sessions_registry_dir().glob("*.json"):
            try:
                d = json.loads(p.read_text())
            except Exception:
                continue
            if isinstance(d, dict) and d.get("sessionId"):
                out.append(d)
    except Exception:
        return []
    out.sort(key=lambda d: d.get("updatedAt") or d.get("startedAt") or 0, reverse=True)
    return out


def peer_address(claude_session_id):
    """The `uds:<socket>` address to SendMessage a session whose Claude conversation id is
    `claude_session_id`, or None when it can't be resolved (pre-2.1.224 session with no socket, no
    registry entry, dead pid). Never throws — an unresolvable address must degrade to the old wake
    path, never break a packet build."""
    if not claude_session_id:
        return None
    for d in peer_registry_entries():
        if d.get("sessionId") != claude_session_id:
            continue
        sock = d.get("messagingSocketPath")
        # A stale record outlives its process; the socket file outlives it too, so check the pid.
        if sock and d.get("pid") and _pid_alive(d["pid"]):
            return f"uds:{sock}"
    return None


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


# ---- executor agent ----------------------------------------------------------------------------
# The executor ROLE (standing GATES + REPORT FORMAT) is a Claude Code agent definition,
# agents/executor.md in this plugin, passed INLINE at launch (`--agents <json> --agent
# relay-executor`) rather than by plugin name — executors are launched plain (no --plugin-dir), so
# `relay:executor` would not resolve for a lead that loaded relay via --plugin-dir. Inline works in
# every install mode; it is not restored by `claude --resume`, but relay re-passes every launch flag
# on relaunch anyway (same as --settings / --mcp-config).
#
# Verified live (2026-08-21, tests/test_e2e_agent.py): the agent prompt is APPENDED to the harness
# system prompt (not a replacement); CLI --model beats the agent's model; the agent's
# `disallowedTools: Agent` removes the Agent tool from the top-level session; an agent-level
# `Bash(git commit*)` deny does NOT hold under --dangerously-skip-permissions, while the CLI
# `--disallowedTools` rule DOES — hence the git denies ride the CLI flag below, not the agent file.

EXECUTOR_AGENT_NAME = "relay-executor"
EXECUTOR_AGENT_FILE = "executor.md"
EXECUTOR_DENIED_BASH = ("Bash(git commit*)", "Bash(git push*)")


def parse_agent_file(text):
    """Minimal front-matter parser for an agent .md: returns (fields, body). Fields are the
    `key: value` lines between the leading `---` fences; `tools`/`disallowedTools` values are
    split on commas into lists. Body is everything after the closing fence, stripped."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    fields, i = {}, 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k in ("tools", "disallowedTools"):
                v = [t.strip() for t in v.split(",") if t.strip()]
            fields[k] = v
        i += 1
    return fields, "\n".join(lines[i + 1:]).strip()


def load_executor_agent(plugin_root):
    """The inline `--agents` definition for the executor role, or None if the plugin has no
    agents/executor.md (a spawn then falls back to the full GATES footer in the packet — see
    bin/relay build_packet)."""
    try:
        p = Path(plugin_root) / "agents" / EXECUTOR_AGENT_FILE
        fields, body = parse_agent_file(p.read_text())
        if not body:
            return None
        agent = {"description": fields.get("description", "relay executor"), "prompt": body}
        if fields.get("disallowedTools"):
            agent["disallowedTools"] = fields["disallowedTools"]
        if fields.get("tools"):
            agent["tools"] = fields["tools"]
        return {EXECUTOR_AGENT_NAME: agent}
    except Exception:
        return None


def executor_agent_flags(plugin_root):
    """The `claude` argv words that give an executor its role: `--agents <json> --agent
    relay-executor --disallowedTools "Bash(git commit*),Bash(git push*)"` — or [] when the agent
    file is missing. The denies are ONE comma-joined argument on purpose: `--disallowedTools` is
    variadic and would otherwise swallow the prompt positional that follows it."""
    agents = load_executor_agent(plugin_root)
    if not agents:
        return []
    return ["--agents", json.dumps(agents, separators=(",", ":")), "--agent", EXECUTOR_AGENT_NAME,
            "--disallowedTools", ",".join(EXECUTOR_DENIED_BASH)]


# ---- auto-close policy -------------------------------------------------------------------------
# Field observation (2026-08-21): executors finish, report, and then sit idle for hours because
# nobody says `relay close` — tabs pile up, `relay list` fills with noise, and the live processes
# hang around. Closing costs nothing that matters: the report is on disk, staged work stays in the
# worktree, and `relay send` to a closed session auto-resumes the SAME conversation. So relay parks
# them itself, on two deterministic signals, with no model call:
#   landed — the report's claimed files are clean in the worktree (nothing staged/modified/
#            untracked for them): the lead has committed or discarded the work. Immediate (after a
#            short grace so a report written seconds ago isn't judged mid-stage).
#   idle   — reported for longer than auto_close_idle_minutes.
# Both REQUIRE that the owning lead has already surfaced the report (the wake dedup set) — relay
# never parks a report nobody has looked at — and never touch busy/stalled/queued/pinned sessions.

AUTO_CLOSE_LANDED_GRACE_SECONDS = 120

# Row 70 item 1 — the deadlock (2026-09-06 field report, `relay-issues/01-heavy-queue-deadlock.md`):
# a heavy session with a --when-idle packet queued could neither receive it (`deliver_queued` hits
# cmd_send's heaviness gate and puts the item back with `last_error` set) nor be parked (the sweep
# below skipped anything with a queue). Two sessions sat `reported` for hours, work already
# committed. The way out: a queue whose HEAD is undeliverable-for-heaviness is not "a session with
# work in flight" — it is a session that can never run that work at all, so it reads as EMPTY for
# the landed/idle rules and the packet is carried to a successor by hand (`send --rotate`). Only
# heaviness: a head stuck on anything transient (tab unreachable, mid-supersede) may still deliver
# on the next poll, and those DO keep the session open.
QUEUE_HEAVY_REFUSAL = " is heavy — "


def queue_stuck_on_heaviness(last_error):
    """Whether a queued item's recorded `last_error` is cmd_send's heaviness refusal (which reads
    "session '<sid>' is heavy — <reading>. …"). PURE, and deliberately a substring test on the one
    phrase that gate owns: `deliver_queued` stores the refusal as free text, so this is the only
    signal either side has."""
    return bool(last_error) and QUEUE_HEAVY_REFUSAL in str(last_error)


def auto_close_decision(s, *, report_age, surfaced, queued, claimed, dirty, heavy,
                        idle_minutes, grace=AUTO_CLOSE_LANDED_GRACE_SECONDS,
                        queue_stuck_heavy=False, no_change=False):
    """PURE: should session record `s` be parked, and why? Returns (action, reason) with action
    "close" | "retire" (retire when `heavy`, so the successor seed is written), or None.
      report_age   seconds since its current report was written (None = no report)
      surfaced     the owning lead has already seen that report (lead_guard surfaced set)
      queued       number of --when-idle packets waiting (any → keep it)
      claimed      paths the report claims under "What changed" (empty → landed path unavailable)
      dirty        paths currently staged/modified/untracked in the worktree
      heavy        transcript past the heaviness threshold → retire instead of close
      idle_minutes timer threshold; 0/None disables the timer path
      queue_stuck_heavy  the queue head is undeliverable for heaviness (queue_stuck_on_heaviness)
                   → the queue does NOT count as "in use"; see QUEUE_HEAVY_REFUSAL above
      no_change    the report is a finished OPS report that says it staged nothing
                   (report_verify.clean_no_change_report) → see the no-change landing below"""
    if not s or s.get("keep") or s.get("status") != "reported":
        return None
    if report_age is None or not surfaced:
        return None
    if queued and not queue_stuck_heavy:
        return None
    reason = None
    if claimed and report_age >= grace and not (set(claimed) & set(dirty or ())):
        reason = "landed"
    # Row 70 item 3: an ops report claims no paths, so the rule above can never fire for it and the
    # session used to sit out the whole idle timer having finished. It lands the moment BOTH halves
    # agree there is nothing to review: the report says `Status: clean` + "changed nothing"
    # (`no_change`), and the worktree really is clean. `dirty is None` means git was unreadable —
    # that is "unknown", not "clean", and must never land.
    elif no_change and not claimed and dirty is not None and not dirty and report_age >= grace:
        reason = "landed"
    elif idle_minutes and report_age >= float(idle_minutes) * 60:
        reason = f"idle {int(report_age // 60)}m"
    if not reason:
        return None
    return ("retire" if heavy else "close"), reason


# ---- executor context window -------------------------------------------------------------------
# Claude Code opens the default (200K) window for a bare model alias and the 1M window for the
# `[1m]` suffix (`sonnet[1m]` → claude-sonnet-5[1m]; verified live 2026-08-21). Haiku 4.5 is a
# 200K model — no real 1M flavour. The window is fixed when the executor PROCESS starts, and a
# `--resume` keeps the conversation's model, so this is a spawn-time decision; a session that ran
# heavy is widened by retire + respawn (the successor seed says so), never in place.
#
# Who decides: the LEAD, explicitly (`CONTEXT: 1m` in the packet, or `--model sonnet[1m]`); else
# relay mechanically (the packet's referenced files are big enough that reading them would crowd a
# 200K window); else 200K. The executor never picks its own window.

CONTEXT_RE = re.compile(r"^\s*(?:[-*>]\s*)?\**\s*CONTEXT\s*\**\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
CONTEXT_1M_BYTES = 600_000        # ~150K tokens of referenced reading → the 200K window is crowded
NO_1M_TIERS = ("haiku",)          # tiers with no 1M window
_PATH_RE = re.compile(r"(?<![\w/.-])((?:~|\.{1,2})?/?(?:[\w.@-]+/)+[\w.@-]+\.[A-Za-z0-9]{1,8}|[\w.@-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|kt|rb|php|cs|c|h|cpp|hpp|md|txt|json|yaml|yml|toml|sql|sh|html|css))(?![\w/])")


def normalize_context_spec(raw):
    """"1m" | "200k" | None. Accepts 1m/1M/1000k/1000000, 200k/200K/default/standard."""
    if raw is None:
        return None
    v = str(raw).strip().lower().replace(" ", "")
    if v in ("1m", "1000k", "1000000", "1mtok", "1m-context"):
        return "1m"
    if v in ("200k", "200000", "default", "standard", "normal"):
        return "200k"
    return None


def packet_context_spec(body):
    """The window a PACKET declares via a `CONTEXT: 1m` / `CONTEXT: 200k` line, or None."""
    if not body:
        return None
    m = CONTEXT_RE.search(body)
    return normalize_context_spec(m.group(1)) if m else None


def model_has_1m(model):
    return bool(model) and str(model).strip().lower().endswith("[1m]")


def model_with_context(model, ctx):
    """Apply a window to a model string: "1m" adds the `[1m]` suffix, "200k" strips it, None
    leaves it alone."""
    if not model:
        return model
    base = str(model).strip()
    if base.lower().endswith("[1m]"):
        base = base[:-4]
    if ctx == "1m":
        return base + "[1m]"
    return base


def model_supports_1m(model):
    return model_tier(model) not in NO_1M_TIERS


def packet_reading_bytes(body, cwd=None):
    """Total size of the files a packet refers to (paths that exist — relative to `cwd` or
    absolute/home). Pure-mechanical: a proxy for "how much must the executor read", which is the
    only signal relay has for the window. Missing paths count nothing."""
    total, seen = 0, set()
    for m in _PATH_RE.finditer(body or ""):
        raw = m.group(1)
        # BUG-lib-9: Path(raw).expanduser() RAISES (RuntimeError) for "~nosuchuser/..." — CPython
        # pathlib can't resolve a home dir for an unknown user. That candidate must go through the
        # SAME try/except every candidate below already has, not get built ahead of it — a packet
        # merely MENTIONING another user's home path (~ops/deploy.sh, a path copied from another
        # machine) must never crash the spawn.
        cands = []
        if cwd and not raw.startswith(("/", "~")):
            cands.append(Path(cwd) / raw)
        try:
            cands.append(Path(raw).expanduser())
        except Exception:
            pass
        for c in cands:
            try:
                rp = c.resolve()
                if rp in seen:
                    break
                if rp.is_file():
                    seen.add(rp)
                    total += rp.stat().st_size
                    break
            except Exception:
                continue
    return total


def decide_context(model, packet_ctx, reading_bytes, threshold=CONTEXT_1M_BYTES, default_ctx="200k"):
    """(resolved_model, ctx, source) — the executor's context window. Precedence:
      explicit `[1m]` on the model string > packet CONTEXT: line > reading-size heuristic >
      `default_ctx` (the executor_default_context config, "1m" shipped).
    A tier with no 1M window (haiku) is never given the suffix; an EXPLICIT `[1m]` on such a tier
    raises ValueError (the lead asked for something that doesn't exist); a packet/heuristic/default
    ask for 1m just degrades to 200k. The window is a CEILING, not consumption — you pay for tokens
    actually used, so 1m-by-default costs nothing extra on a bounded packet."""
    if model_has_1m(model):
        if not model_supports_1m(model):
            raise ValueError(f"'{model}': the {model_tier(model)} tier has no 1M context window")
        return model, "1m", "--model"
    if packet_ctx:
        want, src = packet_ctx, "packet CONTEXT: line"
    elif reading_bytes >= threshold:
        want, src = "1m", f"referenced reading ~{reading_bytes // 1024}KB"
    else:
        want, src = normalize_context_spec(default_ctx) or "200k", "default"
    if want == "1m" and not model_supports_1m(model):
        note = f"200k (no 1M window on {model_tier(model)}"
        note += ")" if src == "default" else f"; wanted 1m from {src})"
        return model_with_context(model, "200k"), "200k", note
    if want == "1m":
        return model_with_context(model, "1m"), "1m", src
    return model_with_context(model, "200k"), "200k", src


# ---- transcript usage (tokens per session) -----------------------------------------------------
# Claude Code's transcript JSONL carries the API `usage` of every assistant message. Summing it gives
# REAL spend per session (prompt = input + cache_read + cache_creation; output), not the MB proxy.
# Assistant lines are repeated once per content block with the same message id and usage — dedup by
# message id (last wins) or every multi-block turn is counted N times (observed live 2026-08-21:
# 201 of 262 message ids duplicated in one transcript).
#
# `last_prompt`/`last_ts` are the LAST request's reading, not a sum — that's the LIVE context (what
# the next turn actually pays to re-send if the cache is cold), verified against a real transcript
# 2026-09-02: each line carries a top-level `timestamp` (ISO-8601, e.g. "2026-09-02T03:55:36.209Z").

def _parse_ts(raw):
    """ISO-8601 timestamp (as Claude Code's transcript writes it) → epoch seconds, or None."""
    if not raw:
        return None
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def transcript_usage(path):
    """Aggregate usage for one transcript: {"requests", "prompt", "input", "cache_read",
    "cache_create", "output", "models": {model: requests}, "last_prompt", "last_ts", "max_prompt",
    "cache_hit_rate"} — or None when unreadable.
    `last_prompt` is the LAST request's input+cache_read+cache_create (the live context a rotate-
    vs-reuse call should actually weigh); `last_ts` is that request's timestamp as epoch seconds;
    `max_prompt` is the LARGEST single request's input+cache_read+cache_create over the whole
    session — the empirical lower bound on the context window that was actually in effect (a
    request that size could not have succeeded on a smaller window), used to PROVE a session's
    stamped context rather than trust it; `cache_hit_rate` is cache_read / prompt over the WHOLE
    session (0.0-1.0, None when prompt == 0)."""
    try:
        by_id = {}
        order = []
        last_mid, last_ts = None, None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message") or {}
                u = m.get("usage") or {}
                if not u:
                    continue
                mid = m.get("id") or d.get("requestId") or d.get("uuid")
                if mid not in by_id:
                    order.append(mid)
                by_id[mid] = (u, m.get("model"))
                last_mid = mid
                last_ts = _parse_ts(d.get("timestamp"))
        agg = {"requests": 0, "prompt": 0, "input": 0, "cache_read": 0, "cache_create": 0, "output": 0,
               "models": {}, "last_prompt": 0, "last_ts": None, "max_prompt": 0, "cache_hit_rate": None}
        for mid in order:
            u, model = by_id[mid]
            i = int(u.get("input_tokens") or 0); cr = int(u.get("cache_read_input_tokens") or 0)
            cc = int(u.get("cache_creation_input_tokens") or 0); o = int(u.get("output_tokens") or 0)
            agg["requests"] += 1; agg["input"] += i; agg["cache_read"] += cr; agg["cache_create"] += cc
            agg["output"] += o; agg["prompt"] += i + cr + cc
            agg["max_prompt"] = max(agg["max_prompt"], i + cr + cc)
            if model:
                agg["models"][model] = agg["models"].get(model, 0) + 1
        if last_mid is not None:
            u, _ = by_id[last_mid]
            agg["last_prompt"] = (int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                                   + int(u.get("cache_creation_input_tokens") or 0))
            agg["last_ts"] = last_ts
        if agg["prompt"] > 0:
            agg["cache_hit_rate"] = agg["cache_read"] / agg["prompt"]
        return agg
    except Exception:
        return None


def cache_state(usage, now, ttl_minutes):
    """Cache-warm/cold readout for the lead's reuse-vs-rotate call: `("warm", None)` when `now` is
    still within `ttl_minutes` of the session's last request (Claude Code's prompt cache — a 1-hour
    TTL by default — hasn't expired, so the next turn's prefix is still cached free); else
    `("cold", est_rewrite_tokens)` where the estimate is `last_prompt` (the prefix size that will be
    re-written on the next turn). `None` when there's nothing to read (no usage, or a usage with no
    `last_ts` — e.g. a transcript with no per-request timestamp)."""
    if not usage or not usage.get("last_ts"):
        return None
    age = now - usage["last_ts"]
    if age < ttl_minutes * 60:
        return ("warm", None)
    return ("cold", usage.get("last_prompt") or 0)


def human_tokens(n):
    """1234 → '1.2k', 1_234_567 → '1.2M', 0 → '0'."""
    try:
        n = int(n)
    except Exception:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k" if n >= 10_000 else f"{n / 1_000:.1f}k"
    return str(n)


TIER_WINDOWS_FILE = "tier_windows.json"


def load_tier_windows(state_root):
    """{"<model id as modelUsage keys it>": {"window": N, "alias": "<alias probed>",
    "probed_at": ts}, ...} — the cache `relay doctor` writes from its live probes (haiku, sonnet,
    sonnet[1m], opus, opus[1m]). {} when the file doesn't exist yet or is unparsable — callers
    fall back to the launch-time context stamp, they never assume a window."""
    try:
        return json.loads((Path(state_root) / TIER_WINDOWS_FILE).read_text())
    except Exception:
        return {}


def save_tier_windows(state_root, tier_windows):
    path = Path(state_root) / TIER_WINDOWS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tier_windows, indent=2, sort_keys=True))


def window_for(model, tier_windows):
    """Resolve `model` — an alias ("sonnet", "sonnet[1m]") or a concrete model id the way
    modelUsage keys it ("claude-sonnet-5", "claude-sonnet-5[1m]") — to the REAL context window
    `relay doctor` last probed for it, via `tier_windows` (as `load_tier_windows` returns). None
    when that exact model/alias hasn't been probed — callers must not guess in that case, they
    fall back to the launch-time stamp instead. A concrete id matches a `tier_windows` key
    directly; an alias matches an entry's `alias` field."""
    if not model or not tier_windows:
        return None
    model = str(model)
    entry = tier_windows.get(model)
    if entry:
        return entry.get("window")
    for e in tier_windows.values():
        if isinstance(e, dict) and e.get("alias") == model:
            return e.get("window")
    return None


def _fmt_window(n):
    """1_000_000 -> "1M", 200_000 -> "200k" — same units as `human_tokens` but without its
    1-decimal rounding (which would print "1.0M"), and with a generic fallback for any other
    window value a future probe might learn."""
    if n == 1_000_000:
        return "1M"
    if n == 200_000:
        return "200k"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def ctx_window_cell(usage, ctx, model=None, tier_windows=None):
    """(cell_text, contradiction) for the "live/window" CTX reading `relay list`/the board render.
    cell_text is "<live>/<window>" — "264k/1M" or "88k/200k". The window compared against is the
    REAL one `relay doctor` last probed for `model` (via `window_for(model, tier_windows)`) when
    known; otherwise it falls back to the session's launch-time `ctx` stamp ("1m" -> 1_000_000,
    "200k" -> 200_000, anything else/None -> unknown, cell is the live number alone with no slash).
    A `✓` is appended when `max_prompt` (the largest single request's input+cache_read+
    cache_create over the session — transcript_usage's empirical lower bound on the window that
    was actually in effect) exceeds 200_000 on a session whose real-or-stamped window is
    1_000_000: a request that size could only have succeeded on the 1M window, so the window is
    PROVEN, not merely trusted. `contradiction` is True (and the cell reads "!" instead of "✓")
    ONLY when max_prompt exceeds the real-or-stamped window itself — never merely because the
    stamp says "200k": when `tier_windows` says the REAL window is 1M, a 200k stamp is read as
    conservative, not wrong, and the cell renders "<live>/1M" with no flag at all."""
    live = human_tokens((usage or {}).get("last_prompt") or 0)
    max_prompt = (usage or {}).get("max_prompt") or 0
    real = window_for(model, tier_windows) if model and tier_windows else None
    if real is not None:
        window = real
    elif ctx == "1m":
        window = 1_000_000
    elif ctx == "200k":
        window = 200_000
    else:
        window = None
    if window is None:
        return live, False
    cell = f"{live}/{_fmt_window(window)}"
    if max_prompt > window:
        return cell + " !", True
    if window == 1_000_000 and max_prompt > 200_000:
        return cell + " ✓", False
    return cell, False


def usage_cell(usage, cache=None):
    """'<prompt>/<output>' e.g. '1.2M/34k', or '-' when unknown. `cache` — the (state, est) tuple
    `cache_state` returns, or None — grows a third segment: '1.2M/34k ·warm' or
    '1.2M/34k ·cold~212k'; omitted (or an unrecognized state) renders no segment."""
    if not usage:
        return "-"
    base = f"{human_tokens(usage.get('prompt', 0))}/{human_tokens(usage.get('output', 0))}"
    if not cache:
        return base
    state, est = cache
    if state == "warm":
        return f"{base} ·warm"
    if state == "cold":
        return f"{base} ·cold~{human_tokens(est)}"
    return base


def is_heavy(usage, mb, token_threshold, mb_threshold):
    """Whether a session (lead or executor) counts as 'heavy'. `usage` (transcript_usage's dict, or
    None) is the primary signal: heavy when its `last_prompt` (the live context) is at/above
    `token_threshold`. `usage is None` means the transcript couldn't be parsed for real usage at all
    (unlocatable, or not a Claude Code transcript) — falls back to the raw MB reading against
    `mb_threshold` so that case never silently reads as 'never heavy'. Both unavailable (mb is also
    None) → never heavy; there's nothing to warn about, and guessing would be worse than quiet.
    Shared by bin/relay's executor gate (§6e e2, `relay list`'s heavy footnote/CTX columns) and the
    lead's own handoff nudge — moved here so both read the exact same rule."""
    if usage is not None:
        return (usage.get("last_prompt") or 0) >= token_threshold
    return mb is not None and mb >= mb_threshold


def heavy_reading_text(usage, mb):
    """The human-readable 'how heavy' reading behind the heavy footnote, the send-time gate message,
    and the lead's handoff nudge: token reading when usage parsed (the real signal), else the
    MB-fallback reading with a note that it's a proxy."""
    if usage is not None:
        return f"{human_tokens(usage.get('last_prompt') or 0)} ctx"
    return f"{mb:.1f}MB (transcript unreadable for usage; MB proxy)" if mb is not None else "unknown"


def lead_window_for(model, tier_windows=None):
    """The REAL context window for a LEAD's own conversation, or None when it's genuinely unknown.
    A lead's marker carries no explicit `context` stamp the way an executor's session record does
    (see `ctx_window_cell`) — only `model`, which may carry the `[1m]` suffix — so this prefers
    `relay doctor`'s probed window (`window_for`) when known, then infers 1_000_000 from a
    `[1m]`-suffixed model. Anything else (a bare model like "sonnet"/"opus", or no model at all)
    returns None rather than guessing 200_000: backlog row 49 already rules that a bare model is
    never ASSERTED to be 200k, and since every real lead marker on this machine carries
    `model: null`, defaulting to 200_000 here would silently cap every lead's nudge line at 150k
    (see `lead_nudge_threshold`) and the 300k line would never apply to a real lead. Callers
    (`lead_nudge_threshold`, `lead_nudge_reading_text`) already treat None as "uncapped" / "?"."""
    real = window_for(model, tier_windows) if model and tier_windows else None
    if real is not None:
        return real
    return 1_000_000 if model_has_1m(model) else None


def lead_nudge_threshold(cfg, window=None):
    """The EFFECTIVE lead handoff-nudge threshold, in tokens: `lead_nudge_tokens` (default 300000)
    — see LEAD_DEFAULTS for why a lead earns a higher line than an executor's
    `context_nudge_tokens`. Capped to `context_nudge_tokens` (default 150000) when `window` — the
    lead's REAL context window, from `lead_window_for` — is known to be exactly 200_000: a
    200k-window lead can never actually reach 300k live context, so nudging it on that line would
    silently mean 'never'; capping instead means it's still told when it's heavy, just on the same
    line an executor would be. `window` None/unknown, or any other value (1_000_000 included),
    leaves the threshold uncapped."""
    lead_t = float(cfg.get("lead_nudge_tokens", LEAD_DEFAULTS["lead_nudge_tokens"]))
    if window == 200_000:
        exec_t = float(cfg.get("context_nudge_tokens", LEAD_DEFAULTS["context_nudge_tokens"]))
        return min(lead_t, exec_t)
    return lead_t


def lead_nudge_reading_text(tokens, threshold, window):
    """The lead-specific 'how heavy' reading that NAMES the line and the window (unlike the plain
    executor `heavy_reading_text`, a lead's line can be capped by its window, so the reading has to
    show which line actually applies): '<live> live, line <threshold> on a <window> window', e.g.
    '312k live, line 300k on a 1M window'. `window` None/unknown renders as '?'."""
    win_label = _fmt_window(window) if window else "?"
    return (f"{human_tokens(tokens)} live, line {human_tokens(threshold)} on a {win_label} window")


def launch_cell(s):
    """Compact 'what was this executor launched with': mcp/context/role/effort, e.g. 'none/1m/A/high'
    (A = agent-roled, G = legacy full-GATES packets; '?' for records that predate a field — every
    executor spawned since executor_default_effort shipped has a real 4th segment, never '?')."""
    mcp = mcp_spec_label(s.get("mcp")) if s.get("mcp") is not None else "?"
    ctx = s.get("context") or ("1m" if model_has_1m(s.get("model")) else "?")
    role = "A" if s.get("agent") else ("G" if "agent" in s else "?")
    return f"{mcp}/{ctx}/{role}/{s.get('effort') or '?'}"


# ---- executor effort ---------------------------------------------------------------------------
# Effort is the second half of the model dial: WHICH model (the rubric) and HOW HARD it thinks.
# Per-process like the model — set at launch via `claude --effort`, verified live 2026-08-21 (an
# unknown value is warned-and-ignored by the CLI, never fatal). Precedence: `--effort` flag >
# packet `EFFORT:` line > executor_default_effort (LEAD_DEFAULTS above) — ALWAYS explicit now,
# never the CLI's own default and never an unspecified executor's silent inheritance of the
# human's personal `effortLevel` (see resolve_executor_effort_default and the LIVE INCIDENT it
# closes, 2026-09-05, the same class of leak executor_default_model closed for the model).
# Pairing guidance lives in the spawn skill: mechanical/script-checkable → low/medium; the
# workhorse default; unknown-root-cause / core-logic → xhigh (max when correctness beats cost).

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT_RE = re.compile(r"^\s*(?:[-*>]\s*)?\**\s*EFFORT\s*\**\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def normalize_effort_spec(raw):
    """A valid effort level or None."""
    if raw is None:
        return None
    v = str(raw).strip().lower()
    return v if v in EFFORT_LEVELS else None


def packet_effort_spec(body):
    """The effort a PACKET declares via an `EFFORT: <level>` line, or None."""
    if not body:
        return None
    m = EFFORT_RE.search(body)
    return normalize_effort_spec(m.group(1)) if m else None


def resolve_executor_effort_default(cfg):
    """The effort level to stamp on an executor when neither `--effort` nor a packet `EFFORT:`
    line pins one — `executor_default_effort` from `cfg` (relay's own policy; the CLI default is
    `high`), validated against EFFORT_LEVELS exactly like an `--effort` flag value. Raises
    ValueError on an invalid config value: a bad config must fail LOUDLY at spawn, never fall
    through silently to whatever the CLI (or the human's personal ~/.claude/settings.json
    effortLevel) would have picked instead."""
    v = cfg.get("executor_default_effort", "high")
    if v not in EFFORT_LEVELS:
        raise ValueError(f"config executor_default_effort '{v}' is invalid — valid: "
                          f"{', '.join(EFFORT_LEVELS)}")
    return v


# ---- packet lint -------------------------------------------------------------------------------
# Zero-token sanity pass over an OUTGOING packet (spawn / send / `relay lint`). Advisory only —
# prints, never blocks (spawn's hard refusals, e.g. an unknown MCP allowlist name, stay where they
# are). Exists because a packet can now carry declarations (`MCP:`, `CONTEXT:`, `## Preconditions`)
# whose absence is silent: an executor launched without the Linear server just lacks the tool, and
# the lead only finds out from the report.

PRECONDITIONS_RE = re.compile(r"^#{1,6}[ \t]*preconditions\b", re.IGNORECASE | re.MULTILINE)
MCP_HINT_WORDS = ("linear", "jira", "gmail", "google calendar", "google drive", "notion", "slack",
                  "chrome", "browser", "playwright", "puppeteer", "github mcp", "mcp server", "mcp tool")
COMMIT_RE = re.compile(r"\bgit (commit|push)\b|\b(commit|push) (your|the|these|all|this|it)\b", re.IGNORECASE)
ASK_RE = re.compile(r"\b(ask|check with|confirm with|clarify with) (the )?(user|human|lead|me)\b", re.IGNORECASE)

# Packet-shape hints (spawn skill's model rubric, quoted not paraphrased): a packet that's fully
# specified and script-checkable is haiku work; a packet that carries an unanswered question is
# opus work. These are INFO nudges on the packet's own shape, independent of what model was chosen.
OPUS_SHAPE_WORDS = ("investigate", "figure out", "root cause", "unknown", "diagnose", "why does")
HAIKU_DISQUALIFY_WORDS = ("investigate", "figure out", "root cause", "why", "unclear", "diagnose")
ACCEPTANCE_CMD_RE = re.compile(
    r"^(?:#{1,6}[ \t]*)?acceptance\b.*?(?:```|^\s*(?:pytest|npm|make|python3 -m|go test)\b)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL)
FILES_LIST_RE = re.compile(r"^\s*files\s*:", re.IGNORECASE | re.MULTILINE)
BACKTICK_PATH_RE = re.compile(r"`[^`\n]*[/.][^`\n]*`")
REPRO_RE = re.compile(r"\brepro(?:duce)?\b|\bsteps to\b", re.IGNORECASE)


def lint_packet(body, cwd=None, known_servers=None, model=None, reading_bytes=None):
    """List of (level, code, message) findings. level ∈ {"warn", "info"}. Pure given its inputs;
    `known_servers` is the name set from known_mcp_servers(cwd) (None → skip the unknown-name
    check), `reading_bytes` the packet_reading_bytes(body, cwd) total (None → computed here)."""
    out = []
    text = body or ""
    low = text.lower()
    if len(text.strip()) < 80:
        out.append(("warn", "short-packet", "packet body is very short — an executor treats it cold, "
                    "with no access to this conversation; spell out the task and acceptance criteria"))
    if PRECONDITIONS_RE.search(text) is None:
        out.append(("warn", "no-preconditions", "no ## Preconditions section — authoring one forces the "
                    "world-state walk before send (checkout pulled? code live? DDL applied?)"))
    # MCP
    mcp_line = PACKET_MCP_RE.search(text)
    mcp_spec = packet_mcp_spec(text)
    # BUG-cli-6/BUG-lib-7: normalize_mcp_spec is total (any non-empty string that isn't a keyword
    # falls through to a comma-split allowlist), so `mcp_spec is None` never actually happens when
    # `mcp_line` is present — this finding could never fire. Linter-local fix: re-check the RAW
    # value's comma-parts against the plausible-server-name shape directly, rather than relying on
    # normalize_mcp_spec's (unchanged) resolution to signal failure. `mcp_spec is None` is kept
    # alongside it — harmless today, and correct again if normalize_mcp_spec's totality is ever
    # narrowed later.
    if mcp_line and (mcp_spec is None or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+", part.strip())
            for part in mcp_line.group(1).split(",") if part.strip())):
        out.append(("warn", "mcp-unparsable", f"MCP: line present but its value isn't none/inherit/a,b: "
                    f"'{mcp_line.group(1)}'"))
    if mcp_spec is None:
        hits = sorted({w for w in MCP_HINT_WORDS if w in low})
        if known_servers:
            hits += sorted(n for n in known_servers if n.lower() in low and n.lower() not in hits)
        if hits:
            out.append(("warn", "mcp-mentioned-not-declared",
                        f"packet mentions {', '.join(hits)} but declares no MCP: line — executors launch "
                        f"with NO MCP servers; add `MCP: <server>` (configured) or `MCP: inherit` if the "
                        f"task really needs the tool"))
    elif isinstance(mcp_spec, list) and known_servers is not None:
        unknown = [n for n in mcp_spec if n not in known_servers]
        if unknown:
            out.append(("warn", "mcp-unknown-server", f"MCP: names {', '.join(unknown)} — not configured in "
                        f"~/.claude.json / <worktree>/.mcp.json (spawn will refuse); plugin/connector MCPs "
                        f"need `MCP: inherit`"))
    # context window
    ctx_m = CONTEXT_RE.search(text)
    ctx = packet_context_spec(text)
    if ctx_m and ctx is None:
        out.append(("warn", "context-unparsable", f"CONTEXT: line present but value isn't 1m/200k: "
                    f"'{ctx_m.group(1)}'"))
    rb = reading_bytes if reading_bytes is not None else packet_reading_bytes(text, cwd=cwd)
    if rb >= CONTEXT_1M_BYTES:
        if ctx == "200k":
            out.append(("warn", "context-200k-big-reading", f"packet references ~{rb // 1024}KB of files but "
                        f"pins CONTEXT: 200k — the executor will compact early"))
        elif ctx is None:
            out.append(("info", "context-auto-1m", f"packet references ~{rb // 1024}KB of files — relay will "
                        f"launch the executor on the 1M window (declare CONTEXT: 200k to override)"))
    if ctx == "1m" and model and not model_supports_1m(model):
        out.append(("warn", "context-1m-on-haiku", f"CONTEXT: 1m but model '{model}' has no 1M window — "
                    f"it will run on 200k"))
    if rb >= 2 * CONTEXT_1M_BYTES:
        out.append(("warn", "reading-front-loaded", f"packet front-loads ~{rb // 1024}KB of required "
                    f"reading — every rotate/resume re-writes that prefix at cache-write price; trim "
                    f"REQUIRED READING to what the first task needs"))
    ef_m = EFFORT_RE.search(text)
    if ef_m and normalize_effort_spec(ef_m.group(1)) is None:
        out.append(("warn", "effort-unparsable", f"EFFORT: line present but value isn't one of "
                    f"{'/'.join(EFFORT_LEVELS)}: '{ef_m.group(1)}' — the CLI would ignore it"))
    # role violations the executor can't honour
    if COMMIT_RE.search(text):
        out.append(("warn", "asks-to-commit", "packet tells the executor to commit/push — executors stage "
                    "only (commit/push are denied); phrase it as 'stage for review'"))
    if ASK_RE.search(text):
        out.append(("warn", "asks-to-ask", "packet tells the executor to ask someone — executors never ask "
                    "in the tab; phrase it as 'stop and report the blocker'"))
    # packet shape (spawn skill's model rubric, echoed not reinterpreted)
    tier = model_tier(model)
    has_acceptance_cmd = bool(ACCEPTANCE_CMD_RE.search(text))
    has_file_list = bool(FILES_LIST_RE.search(text)) or len(BACKTICK_PATH_RE.findall(text)) >= 2
    if tier != "haiku" and has_acceptance_cmd and has_file_list \
            and not any(w in low for w in HAIKU_DISQUALIFY_WORDS):
        out.append(("info", "shape-haiku", "this packet names its files and has a command that checks "
                    "it's done — haiku can do this; you picked something bigger"))
    if tier not in ("opus", "fable") and any(w in low for w in OPUS_SHAPE_WORDS) \
            and REPRO_RE.search(text) is None:
        out.append(("info", "shape-opus", "this packet asks the executor to figure something out "
                    "(investigate / root cause / why) and gives no repro — a wrong answer would look "
                    "right; consider opus"))
    return out


# ---- model alias resolution --------------------------------------------------------------------
# A bare alias (`sonnet`, `opus`, `haiku`, `fable`) means "the latest THIS Claude Code build knows" —
# the same alias resolves to different models on machines running different CLI versions, and on
# some the 1M window only takes with the full id (`claude-sonnet-5[1m]`). Relay therefore resolves
# an alias through the installed CLI itself at spawn time (`claude --model <alias> -p` emits a
# system/init event naming the concrete id — the same probe relay doctor reads; an unknown model is
# reported as an error before any work) and LAUNCHES WITH THE CONCRETE ID. One probe per alias per
# Claude Code version per machine, cached in <state_root>/models.json; `/model` remains the human's
# tool — relay just stops passing ambiguous strings. Fail-open: if the probe itself fails (offline,
# timeout) the alias is passed through unchanged, with a note.

MODEL_ALIASES = ("sonnet", "opus", "haiku", "fable")


def split_model_suffix(model):
    """('sonnet', '[1m]') for 'sonnet[1m]'; ('claude-opus-5', '') for a full id."""
    m = str(model or "").strip()
    return (m[:-4], "[1m]") if m.lower().endswith("[1m]") else (m, "")


def is_model_alias(model):
    base, _ = split_model_suffix(model)
    return base.lower() in MODEL_ALIASES


def load_model_cache(cache_path):
    try:
        return json.loads(Path(cache_path).read_text())
    except Exception:
        return {}


def resolve_model(model, cache_path, cli_version, probe):
    """(resolved_model, source). `probe(alias)` → (resolved_id|None, error_text|None). Full ids are
    returned untouched ("explicit"); aliases come from the cache ("cache") or the probe ("probe");
    a probe failure returns the alias unchanged ("unresolved: <why>"); an unrecognised model raises
    ValueError. The `[1m]` suffix is preserved across resolution."""
    base, suffix = split_model_suffix(model)
    if not is_model_alias(base):
        return model, "explicit"
    cache = load_model_cache(cache_path)
    per_ver = cache.get(str(cli_version)) or {}
    hit = per_ver.get(base.lower())
    if hit:
        return hit + suffix, "cache"
    rid, err = probe(base)
    if err and ("unrecognized" in err.lower() or "may not exist" in err.lower()):
        raise ValueError(f"model '{base}' is not recognised by this Claude Code ({cli_version}): {err.strip()[:120]}")
    if not rid:
        return model, f"unresolved: {(err or 'no init event').strip()[:80]}"
    try:
        cache.setdefault(str(cli_version), {})[base.lower()] = rid
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps(cache, indent=2))
    except Exception:
        pass
    return rid + suffix, "probe"


# ---- executor MCP policy -----------------------------------------------------------------------
# An executor does worktree file/git work; every MCP server it inherits (claude.ai connectors like
# Gmail/Linear, plugin MCPs like claude-in-chrome, user/project servers) costs tokens on EVERY turn
# (tool-name roster + per-server instruction blocks, plus schemas once loaded) and is a side-effect
# surface nobody asked for. Relay therefore decides an executor's MCP set itself, like its model:
# default "none" (no config knob — on purpose: a global "inherit" would quietly undo the saving for
# every packet); a packet opts in with its own `MCP: linear` / `MCP: inherit` line, or
# `relay spawn --mcp …` overrides per spawn.
#
# Mechanism (verified live 2026-08-21): `claude --strict-mcp-config --mcp-config '{"mcpServers":{}}'`
# yields a session with NO mcp__* tools at all — connectors and plugin MCPs included. An allowlist
# is the same flags with a per-executor mcp.json holding just the named servers, copied from the
# places the CLI reads them (~/.claude.json top-level `mcpServers`, its `projects[<cwd>].mcpServers`,
# and <cwd>/.mcp.json). Connector/plugin MCPs have no config entry to copy, so they can only come
# back via "inherit".

MCP_NONE_JSON = '{"mcpServers":{}}'
MCP_DEFAULT = "none"   # what an executor gets when its packet declares nothing and no --mcp is passed


def normalize_mcp_spec(raw):
    """Canonical form of an executor MCP spec: "none" | "inherit" | sorted list of server names.
    Accepts the packet `MCP:` value, the `--mcp` CLI value (bare flag → "inherit"; "a,b" → list),
    or a stored session value. None/"" → "none" (the policy default, never silent inheritance)."""
    if raw is None or raw is True:
        return "inherit" if raw is True else "none"
    if isinstance(raw, (list, tuple, set)):
        names = sorted({str(n).strip() for n in raw if str(n).strip()})
        return names or "none"
    s = str(raw).strip()
    if s.lower() in ("", "none", "off", "false", "0"):
        return "none"
    if s.lower() in ("inherit", "all", "on", "true", "1"):
        return "inherit"
    return normalize_mcp_spec(s.split(","))


PACKET_MCP_RE = re.compile(r"^\s*(?:[-*>]\s*)?\**\s*MCP\s*\**\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def packet_mcp_spec(body):
    """The MCP set a PACKET declares, or None if it doesn't. A packet is the single source of truth
    for what its executor needs: a line `MCP: linear` / `MCP: linear, chrome-devtools` /
    `MCP: inherit` / `MCP: none` anywhere in the authored body (typically the header; a leading
    `-`/`*`/bold is tolerated). First such line wins. The lead writes the packet, so it already
    knows whether the task needs a server — no flag to remember, no config to edit, no classifier
    call. Code fences are NOT skipped on purpose: a packet quoting `MCP: x` in an example is rare,
    and a false positive only costs a relaunch/warning, never a missing tool."""
    if not body:
        return None
    m = PACKET_MCP_RE.search(body)
    return normalize_mcp_spec(m.group(1)) if m else None


def mcp_covers(have, need):
    """True if an executor launched with MCP set `have` satisfies a packet needing `need`.
    need None/"none" → always; have "inherit" → always; have "none" → only if need is none;
    lists → need ⊆ have; need "inherit" needs have "inherit"."""
    need = normalize_mcp_spec(need)
    have = normalize_mcp_spec(have)
    if need == "none" or have == "inherit":
        return True
    if have == "none" or need == "inherit":
        return False
    return set(need) <= set(have)


def mcp_union(have, need):
    """Smallest spec covering both — what a relaunch should use when `have` doesn't cover `need`."""
    have = normalize_mcp_spec(have); need = normalize_mcp_spec(need)
    if "inherit" in (have, need):
        return "inherit"
    if have == "none":
        return need
    if need == "none":
        return have
    return normalize_mcp_spec(list(have) + list(need))


def mcp_spec_label(spec):
    """Short human form for `relay list`/ledgers: none | inherit | linear,chrome."""
    spec = normalize_mcp_spec(spec)
    return ",".join(spec) if isinstance(spec, list) else spec


def known_mcp_servers(cwd=None, claude_json=None):
    """Name → server-config dict for every MCP server the CLI would load for a session in `cwd`
    from CONFIG FILES (user ~/.claude.json `mcpServers`, its `projects[cwd].mcpServers`, and
    <cwd>/.mcp.json). Later sources win on a name clash, matching the CLI's project-over-user
    precedence. Best-effort: unreadable files contribute nothing. Does NOT see plugin or claude.ai
    connector MCPs (they have no file entry)."""
    servers = {}
    cj = Path(claude_json) if claude_json else Path.home() / ".claude.json"
    try:
        d = json.loads(cj.read_text())
        if isinstance(d.get("mcpServers"), dict):
            servers.update(d["mcpServers"])
        if cwd:
            proj = (d.get("projects") or {}).get(str(cwd)) or {}
            if isinstance(proj.get("mcpServers"), dict):
                servers.update(proj["mcpServers"])
    except Exception:
        pass
    if cwd:
        try:
            d = json.loads((Path(cwd) / ".mcp.json").read_text())
            if isinstance(d.get("mcpServers"), dict):
                servers.update(d["mcpServers"])
        except Exception:
            pass
    return servers


def mcp_cli_flags(spec, state_root=None, exec_name=None, cwd=None, claude_json=None):
    """The extra `claude` flags (a list of argv words, NOT yet shell-quoted) that realise an
    executor MCP spec:
      "inherit" → []                              (plain launch, CLI loads whatever it normally would)
      "none"    → --strict-mcp-config --mcp-config '{"mcpServers":{}}'
      [names]   → --strict-mcp-config --mcp-config <state_root>/<exec_name>/mcp.json
                  holding exactly those servers (copied from known_mcp_servers(cwd)).
    Raises ValueError naming the unknown servers (and the known ones) when an allowlist asks for
    something no config file defines — a spawn must refuse loudly rather than launch an executor
    silently missing the tool the packet depends on."""
    spec = normalize_mcp_spec(spec)
    if spec == "inherit":
        return []
    if spec == "none":
        return ["--strict-mcp-config", "--mcp-config", MCP_NONE_JSON]
    known = known_mcp_servers(cwd=cwd, claude_json=claude_json)
    missing = [n for n in spec if n not in known]
    if missing:
        raise ValueError(
            f"unknown MCP server(s) {', '.join(missing)} — configured servers for {cwd or 'this cwd'}: "
            f"{', '.join(sorted(known)) or '(none)'}. Plugin/connector MCPs (e.g. claude-in-chrome, "
            f"claude.ai Gmail) have no config entry and are only reachable with --mcp inherit.")
    if not state_root or not exec_name:
        raise ValueError("an MCP allowlist needs state_root and exec_name to write its mcp.json")
    d = Path(state_root) / str(exec_name)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {n: known[n] for n in spec}}, indent=2))
    return ["--strict-mcp-config", "--mcp-config", str(p)]


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
                            "command": "%s %s" % (shlex.quote(hook_path), shlex.quote(exec_name)),
                            "timeout": timeout,
                        }
                    ]
                }
            ]
        }
    }


def _strip_1m_suffix(model_id):
    """Strip a trailing `[1m]` context-window marker (with any surrounding whitespace) from a
    model id string, for same-model comparisons where "sonnet[1m]" and "sonnet" name the same
    underlying tier. Non-strings pass through unchanged (never raises on odd config content)."""
    if not isinstance(model_id, str):
        return model_id
    return re.sub(r"\s*\[1m\]\s*$", "", model_id).strip()


def normalize_fallback_models(fallback, exec_model=None):
    """Claude Code 2.1.263 validates a `--settings` file's `fallbackModel` key as a LIST, not a
    bare string — but README documents `executor_fallback_model` as a single model id (the common
    case), so a bare string there made EVERY new executor fail settings validation at launch
    (lead-found incident: every spawn broke, tonight, across three projects). This normalizes:

      - a bare string  → wrapped in a one-element list
      - a list         → passed through unchanged (already the form Claude Code wants)
      - None/empty     → None (no fallbackModel key at all)

    and then drops any entry naming the SAME model as `exec_model` (the executor's own launch
    model) — compared with any trailing `[1m]` context-window suffix stripped from BOTH sides
    (`_strip_1m_suffix`), since a fallback pointing at the model already running would just spin in
    place on an overload rather than actually falling back to anything different.

    Returns `(models, dropped)`: `models` is the normalized list with any self-referential entries
    removed (None if nothing is left to configure), `dropped` is the list of raw entries removed
    for matching `exec_model` (empty when nothing was dropped — including when `exec_model` is not
    given at all). Pure and never raises; this module never prints — callers own how (or whether)
    to surface `dropped` to the human."""
    if not fallback:
        return None, []
    models = list(fallback) if isinstance(fallback, list) else [fallback]
    dropped = []
    if exec_model:
        exec_key = _strip_1m_suffix(exec_model)
        kept = []
        for m in models:
            if _strip_1m_suffix(m) == exec_key:
                dropped.append(m)
            else:
                kept.append(m)
        models = kept
    return (models or None), dropped


def write_escalation_settings(state_root, plugin_root, exec_name, timeout=30, include_hooks=True,
                              fallback=None, effort=None, exec_model=None):
    """Write this executor's own `--settings` file into its state dir. PER-EXECUTOR (not shared),
    because the file carries that executor's relay name as a hook argument — see
    build_escalation_settings for why the hook can't derive it. Regenerated on each call so it
    always points at the CURRENTLY live plugin_root/version. Returns the path (str), or None on any
    failure — a write failure must fall back to spawning WITHOUT escalation armed rather than
    failing the whole spawn.

    `fallback` is normalized through `normalize_fallback_models` (string→list, self-referential
    entries against `exec_model` dropped) before being stamped as `fallbackModel` — see that
    function for the "why" (a bare string broke Claude Code's own settings validation). Callers
    that also want to WARN about a dropped entry should call `normalize_fallback_models` themselves
    first (this function doesn't print — see that function's own docstring).

    `effort`, when given, is also stamped into the file as `{"effortLevel": effort}` — belt and
    braces alongside the `--effort` CLI flag (executor effort policy above): Claude Code's
    settings-precedence rule (managed > `--settings` file > project local > project > user) means
    this per-launch file's effortLevel wins over the human's own ~/.claude/settings.json even on a
    relaunch path that might drop the flag."""
    try:
        d = Path(state_root) / str(exec_name)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "settings.json"
        content = build_escalation_settings(plugin_root, exec_name, timeout=timeout) if include_hooks else {}
        models, _dropped = normalize_fallback_models(fallback, exec_model)
        if models:
            content["fallbackModel"] = models
        if effort:
            content["effortLevel"] = effort
        p.write_text(json.dumps(content, indent=2))
        return str(p)
    except Exception:
        return None
