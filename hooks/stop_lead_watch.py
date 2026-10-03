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

Also, once per lead ever: if live context (transcript_usage's last_prompt) has grown past
lead_nudge_tokens (a LEAD's own line — window-capped via lead_guard.lead_nudge_threshold; see
LEAD_DEFAULTS — never the executors-only context_nudge_tokens), OR the transcript file has grown
past handoff_nudge_mb (the secondary "session age" / compaction-count signal), nudge a handoff
(summarize → /relay:stop → fresh session + /relay:mode) — a best-effort nudge, not automation.

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
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "scripts"))

STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")
RELAY_BIN = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "bin", "relay")
# THIS hook's own plugin root — after a plugin reload this script runs from the NEW version, so
# reading .claude-plugin/plugin.json / hooks/hooks.json from here (not wherever the lead armed
# under) is what keeps touch_lead's version re-stamp always current.
PLUGIN_ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")

# Backlog row 87: the fixed marker for the "approaching heavy" wake line — ANSI can't survive the
# injected-text path (see the module docstring's sibling in bin/relay), so this emoji IS the colour.
CTX_WARN_MARKER = "\U0001f7e0"  # 🟠


def _notify(cfg, message, project=None, executor=None, lead_sid=None, iterm_session=None,
            subtitle=None, tty=None):
    """Desktop notification for a lead wake — resolves the title (project name) and default
    subtitle (which executor reported) from THIS call site's own vocabulary, then hands off to
    `lead_guard.notify_banner` for the actual two-tier chain (iTerm OSC → osascript — see that
    function's docstring). Lead-found gap (fixed): that chain used to be copy-pasted here AND
    skipped entirely by bin/relay's own `desktop_nudge`, which went straight to osascript; both now
    call the ONE shared implementation.

    Title names the project, subtitle/body names the executor. Configurable via notify_on_wake
    (checked HERE, before resolving title/subtitle at all — notify_banner itself only checks the
    RELAY_NO_NOTIFY kill-switch, since not every caller ties itself to notify_on_wake the same
    way). `tty` (row 91 — the caller's own marker read, passed straight through) lets tier 1 skip
    the live AppleScript lookup entirely; `state_root` is always STATE_ROOT here so a tier-2
    fall-through gets ledgered like every other relay banner."""
    if not cfg.get("notify_on_wake", True):
        return
    import lead_guard as lg
    title = f"relay · {project}" if project else "relay — review needed"
    # `subtitle` lets non-report callers (e.g. the SessionStart re-arm) say what actually happened;
    # without it the default below would mislabel every notification as "review needed".
    if subtitle is None:
        subtitle = f"{executor} reported" if executor else "review needed"
    lg.notify_banner(cfg, title, subtitle, message, lead_sid=lead_sid, iterm_session=iterm_session,
                     tty=tty, state_root=STATE_ROOT)


def _announce_and_wake(lg, cfg, sid, lines, surfaced_keys, notify_msg, kind="sync",
                       transcript_path=None):
    # Row 87: `notify_msg=None` means "skip the banner tier for this wake entirely" — used for a
    # wake whose `lines` are ALL 🟠 ctx-warn lines (row 57's `_maybe_warn_ctx_heavy` already fired
    # that executor's ONE desktop banner from `_check_one`, under its own `<sid>:ctx-warn` key; a
    # second banner here for the same sid would just be noise). A wake that mixes a 🟠 line with a
    # report/commit still gets its normal banner (see main()'s notify_msg selection) — only the
    # all-🟠 case is silent here. The exit-2/stdout announce below is UNCHANGED either way; only the
    # desktop banner is conditional on this.
    # #22 (§13): record the announce as PENDING, never as surfaced. Firing is not delivering — this
    # exit-2 only wakes the lead if the harness is still listening to THIS hook process, and a
    # stale poller announcing while the lead is mid-turn is exactly the case that got dropped and
    # then silently deduped forever. The stamp becomes real when delivery is proven (the
    # stop_hook_active re-run in main, or an #17 channel), so an undelivered wake retries instead.
    if surfaced_keys:
        capped = lg.mark_pending(STATE_ROOT, sid, surfaced_keys)
        if capped:
            lg.append_ledger(STATE_ROOT, "wake_retry_capped", session_id=sid, keys=capped)
    # Identify the source so the notification says WHICH project/executor and can click to the lead.
    marker = lg.read_marker(STATE_ROOT, sid)
    project = marker.get("project")
    executor = surfaced_keys[0].split(":")[0] if surfaced_keys else None  # first executor that reported
    # 8b (lead-found): dedupe the DESKTOP BANNER only — never the stdout/model-facing announce
    # above, which always fires regardless — against the executor's OWN escalation push notifying
    # for the SAME report (a genuine race: this wake's own pending→surfaced promotion, just above,
    # hasn't landed yet when the escalation hook's decision tree reads it). Claim every key in this
    # batch (so each report is independently deduped even when several land in one announce); skip
    # the banner only when EVERY key here was already claimed by someone else.
    claims = [lg.claim_notification(STATE_ROOT, sid, key) for key in surfaced_keys]
    if notify_msg is not None and (not surfaced_keys or any(claims)):
        _notify(cfg, notify_msg, project=project, executor=executor, lead_sid=sid,
                iterm_session=marker.get("iterm_session"), tty=marker.get("tty"))
    # Emoji-forward banner: the model echoes this into its announcement, so 🚦 is a visible,
    # consistent "you have a relay update" marker in the lead's on-screen text.
    #
    # The instruction half is POSTURE-AWARE (§6f, task #16 phase 1). This text is injected at the
    # exact beat autonomous mode redefines — "announce and WAIT" vs "announce, act, and record" — so
    # a hardcoded "WAIT / do NOT act" would silently override the posture the user just granted, and
    # the toggle would appear not to work. Manual stays the default and its wording is unchanged.
    if lg.autonomous_state(marker)[0]:
        instruction = (
            "\n\nYou are in AUTONOMOUS MODE: announce, ACT, and record — do not wait for the user on "
            "the routine, in-plan steps. Open your reply with '🚦 [relay] — review needed:', surface "
            "these, then proceed on whatever is clearly the next step within the ALREADY-APPROVED "
            "plan, announcing each autonomous action together with what you would have asked "
            "(\"proceeded: sent packet 003 — under manual mode this would have waited for your go\"). "
            "STOP and ask anyway if any of these applies: a risk flag / failing tests / UNVERIFIED "
            "claim bearing on correctness; core logic, ledgers, parity tests, migrations or deploys; "
            "an irreversible or outward-facing action; work not in the approved plan; genuine "
            "ambiguity. Say which stop-list item stopped you when one does.\n\n"
            "COMMITTING an executor's work is permitted without asking ONLY when ALL FIVE hold: "
            "(1) `relay verify` says COUNTS-MATCH; (2) the report's TL;DR is Status: clean, Risk "
            "flags: none, UNVERIFIED: none — clean-with-caveats STOPS; (3) the packet was in the "
            "approved plan; (4) nothing sign-off-gated is touched (core logic, ledgers, "
            "parity/golden tests, migrations, deploys — and in this repo hooks/, lib/lead_guard.py, "
            "ledger formats); (5) the diff has been REVIEWED — by default via `/relay:review <sid>` "
            "(a same-model fork reads the whole diff every time, no blanket carve-out for this "
            "repo's own gated files; you read its findings), hunks opened inline only when a "
            "finding names a sign-off-gated path AND the change there is more than a guard or a "
            "rename. Run `relay verify <sid> "
            "--for-autocommit --in-plan --diff-reviewed --findings <path>` (the saved findings path; "
            "omit --findings only if you read the diff inline) and pass the attestation flags only if "
            "they are TRUE — it prints CLEARED or NOT-CLEARED-BECAUSE-<reason>. On NOT-CLEARED, "
            "stop and ask, naming the condition. The verifier gates the AUTOMATION; it never "
            "replaces reviewing the diff, and COUNTS-MATCH never means the report is true.\n\n"
            "If any line above starts with 🟠, surface that line verbatim to the user in your reply, "
            "then continue.\n")
    else:
        instruction = (
            "\n\nOpen your reply with the marker '🚦 [relay] — review needed:', surface these to the "
            "user, and WAIT for their direction. Do NOT auto-review, auto-commit, or otherwise act "
            "on them yourself until the user asks. If a report needs reviewing, tell the user it's "
            "ready and ask whether to review it. If any line above starts with 🟠, surface that line "
            "verbatim to the user in your reply, then continue.\n")
    # #23: relay's OWN receipt-in-waiting for THIS announce (nonce + transcript offset). The next
    # stop_hook_active run promotes pending only if this claim is outstanding and its wake text
    # actually landed — never on the global flag alone, which any other blocking Stop hook sets.
    try:
        lg.record_announce_claim(STATE_ROOT, sid, kind, transcript_path)
    except Exception:
        pass
    sys.stderr.write(
        # The needle below is what proves delivery later (it reappears verbatim in the lead's
        # transcript), so it comes FROM lead_guard rather than being spelled out twice.
        f"🚦 [relay] — review needed: {lg.WAKE_DELIVERY_NEEDLE}:\n"
        + "\n".join(lines)
        + instruction
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
    line carries a BRIEF of the report (its first line) so you know what happened at a glance.

    Row 70 item 4 (belt and braces): a report whose claimed files are already clean at HEAD has
    LANDED — somebody, usually a predecessor lead before a handoff, already reviewed and committed
    it. Waking on that costs a turn and invites a re-review of committed work, so it is skipped and
    ledgered (`wake_skipped_landed`) rather than announced. `report_landed` is conservative: it says
    False for anything it cannot prove, so an uncertain report still wakes."""
    lines, keys = [], []
    for key, exsid, packet, path in lg.new_reports_for(STATE_ROOT, sid):
        landed = lg.claims_landed(STATE_ROOT, exsid, path)
        if landed is True:
            # Row 93: skipping is only safe when relay can PROVE the landing. Stamp the one-shot
            # surfaced mark ONLY when the auto-close sweep's own `landed` ledger event exists for
            # this packet; otherwise ledger the skip without stamping so the next Stop re-evaluates
            # (an early or mistaken skip must never silence a report forever again).
            try:
                proven = lg.landed_event_exists(STATE_ROOT, exsid, packet)
                if proven:
                    lg.mark_surfaced(STATE_ROOT, sid, [key])
                lg.append_ledger(STATE_ROOT, "wake_skipped_landed", session_id=sid,
                                 executor=exsid, packet=packet, key=key, reason="landed",
                                 stamped=bool(proven))
            except Exception:
                pass
            continue
        brief = _report_brief(path)
        # Item 4 (lead-context-burn note): diff size travels next to the report here too, so the
        # very first thing the lead sees says whether an inline read is affordable.
        dsize = lg.diff_size_text(lg.read_session_json(STATE_ROOT, exsid).get("worktree"))
        head = f"  ✅ executor '{exsid}' reported (packet {packet:03d})" + (f" {dsize}" if dsize else "")
        lines.append(f"{head} — {brief}\n       report: {path}" if brief
                     else f"{head} — report at {path}")
        keys.append(key)
    return lines, keys


def _executor_transcript_path(claude_session):
    """Best-effort location of an EXECUTOR's own transcript JSONL, by `claude_session` id — mirrors
    bin/relay's `_transcript_project_dirs`/`_find_transcript_path` (duplicated here, deliberately:
    this hook must never shell out to bin/relay, which also has no `.py` extension and so isn't a
    normal import target for a hook script). None on any error/missing/not-found, same contract."""
    if not claude_session:
        return None
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"
    try:
        for d in root.iterdir():
            if not d.is_dir():
                continue
            p = d / f"{claude_session}.jsonl"
            if p.exists():
                return p
    except OSError:
        return None
    return None


def _ctx_warn_lines(lg, cfg, sid):
    """Backlog row 87: one 🟠 wake line per executor OWNED by this lead that has crossed
    `context_warn_tokens` (the same line whose desktop banner `_maybe_warn_ctx_heavy` already fired,
    in bin/relay, from `_check_one`) but not yet `context_nudge_tokens` (the rotate line — past that,
    the existing heavy footnote/banner already own it). Claims a SEPARATE once-only key
    (`<exec_sid>:ctx-warn-wake`) from the banner's own `<exec_sid>:ctx-warn`, so the banner and this
    wake line each fire exactly once, independently — a lead that missed the desktop banner (asleep,
    notifications off) still gets told here, and vice versa.

    Kill-switch: `ctx_warn_wake` (default True) — checked BEFORE claiming anything, same reasoning
    as the banner's own `notify_on_wake` check (`_maybe_warn_ctx_heavy`'s comment): a silenced line
    must never burn the once-only stamp, so flipping it back on later still gets the FIRST real
    check its line.

    Best-effort/fail-open throughout, like every other block in this hook: a corrupt session.json,
    an unlocatable transcript, or any other single-executor failure is skipped rather than raised,
    and never affects any other executor's line."""
    if not cfg.get("ctx_warn_wake", True):
        return []
    warn_threshold = float(cfg.get("context_warn_tokens", lg.LEAD_DEFAULTS["context_warn_tokens"]))
    nudge_threshold = float(cfg.get("context_nudge_tokens", lg.LEAD_DEFAULTS["context_nudge_tokens"]))
    if warn_threshold >= nudge_threshold:
        return []  # row 57's own "never fires" clause — a misconfigured line is inert, not an error
    lines = []
    try:
        root = Path(STATE_ROOT)
        if not root.exists():
            return []
        for d in sorted(root.iterdir()):
            exec_sid = d.name
            sj = d / "session.json"
            if not sj.exists():
                continue
            try:
                s = json.loads(sj.read_text())
            except Exception:
                continue
            if s.get("owner_lead") != sid:
                continue  # not owned by THIS lead (another lead's, or unowned) — not ours to warn on
            if s.get("status") not in ("busy", "idle", "stalled", "reported"):
                continue
            try:
                path = _executor_transcript_path(s.get("claude_session"))
                usage = lg.transcript_usage(str(path)) if path else None
            except Exception:
                usage = None
            if usage is None:
                continue  # no live reading to warn on — same rule as the banner
            last_prompt = usage.get("last_prompt") or 0
            if last_prompt < warn_threshold or last_prompt >= nudge_threshold:
                continue
            key = f"{exec_sid}:ctx-warn-wake"
            if not lg.claim_notification(STATE_ROOT, sid, key):
                continue  # already warned once for this sid
            ctx_k = int(last_prompt // 1000)
            warn_k = int(warn_threshold // 1000)
            nudge_k = int(nudge_threshold // 1000)
            lines.append(
                f"  {CTX_WARN_MARKER} {exec_sid} is approaching heavy ({ctx_k}k ctx, warn line "
                f"{warn_k}k, rotate line {nudge_k}k) — plan a rotate: relay retire {exec_sid} + a "
                f"fresh spawn for its next packet")
    except Exception:
        pass
    return lines


def _notify_summary(lines):
    """A one-line, emoji-stripped summary for the macOS notification banner."""
    for ln in lines:
        t = ln.strip().lstrip("✅📝 ").strip()
        if t:
            return t
    return "new relay activity — review when ready"


def main():
    # THE INCIDENT (2026-09-05 22:18:50) — see sessionstart_lead_rearm.py's main() for the full
    # account: a headless `claude -p` relay itself launches inherits the LEAD's own tab env, and
    # every one of this plugin's hooks must return immediately on relay's own RELAY_HEADLESS=1
    # marker, before even reading the payload.
    if os.environ.get("RELAY_HEADLESS") == "1":
        sys.exit(0)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        import lead_guard as lg
        sid = payload.get("session_id")
        # The `or` below is the id-changed-on-resume fallback (memory: relay-lead-id-changes-on-
        # resume.md): ONLY reached when sid would otherwise conclude "not a lead" — migrates a
        # marker found for this tab under a different id, so is_lead(sid) is then true for the rest
        # of this hook. Guarded on `payload.get("cwd")` (fix-list 002): a shared tab is NOT proof of
        # the same project — a fresh, unrelated session that merely reuses an old lead's tab must
        # never inherit it, and a payload with no cwd can't prove same-project either way, so it
        # doesn't even attempt the lookup. `safe_migrate_by_tab` additionally refuses when the
        # matched old marker still looks like a LIVE lead (THE INCIDENT, see main()'s top) — this
        # exact unconditional migrate-on-tab-match was the other path into that incident.
        if not sid or not (lg.is_lead(STATE_ROOT, sid)
                            or (payload.get("cwd") and lg.safe_migrate_by_tab(
                                STATE_ROOT, lg.env_tab_id(), payload.get("cwd"),
                                sid, lg.load_config(STATE_ROOT).get(
                                    "poll_seconds", lg.LEAD_DEFAULTS["poll_seconds"])))):
            sys.exit(0)  # not a lead session → silent, zero impact
        # Heartbeat: every lead turn refreshes last_active (and re-stamps plugin_version/
        # stop_hook_timeout from THIS hook's own plugin root) so `relay list` reflects real liveness
        # and the current version — not whichever version was live at last arm time.
        try:
            lg.touch_lead(STATE_ROOT, sid, plugin_root=PLUGIN_ROOT)
        except Exception:
            pass
        cfg = lg.load_config(STATE_ROOT)

        # Auto-close (lead_guard "auto-close policy"): every lead turn-end is the "lead thinks"
        # beat — park this lead's finished executors (landed / idle past threshold). Delegated to
        # the CLI so there is ONE implementation (relay list/check run the same sweep). Best-effort,
        # bounded, silent here: the ledger records it and `relay list` shows `closed (auto)`.
        if cfg.get("auto_close", True):
            try:
                subprocess.run([RELAY_BIN, "_auto-close-sweep", "--lead", sid],
                               capture_output=True, timeout=25)
            except Exception:
                pass

        # Live board (task: "make relay board live"): every lead turn-end is also a "state
        # changed" moment for the board — rewrite board.html/board.json in place so a page left
        # open sees fresh state within one meta-refresh interval, with NO server process.
        # Unconditional (unlike the sweep above): refresh_live_board itself no-ops instantly
        # unless live mode is actually active (config board_live, or a prior `--live` run's
        # board.json sidecar), so this costs nothing extra for a lead that never touched `board`.
        try:
            subprocess.run([RELAY_BIN, "_refresh-board"], capture_output=True, timeout=25)
        except Exception:
            pass

        transcript_path = payload.get("transcript_path")

        # #22: promote announced-but-unproven wakes once delivery is PROVEN.
        # #23 (field incident 2026-07-22, diagnosis credited in lead_guard's #23 block): the proof
        # is NOT the bare stop_hook_active flag — Claude Code sets that for ANY blocking Stop hook,
        # and a foreign one (a rules-check that blocks on edit turns) was stamping never-delivered
        # wakes as delivered. relay_announce_delivered consults RELAY's own announce claim instead:
        # an outstanding claim whose wake text actually reached the transcript. A stop_hook_active
        # run with no outstanding claim is somebody else's continuation and promotes nothing.
        if payload.get("stop_hook_active"):
            try:
                if lg.relay_announce_delivered(STATE_ROOT, sid):
                    promoted = lg.promote_pending(STATE_ROOT, sid)
                    if promoted:
                        lg.append_ledger(STATE_ROOT, "wake_delivered", session_id=sid, keys=promoted)
            except Exception:
                pass

        # Synchronous announce of what's ALREADY visible at stop time. #23: this runs on EVERY Stop,
        # including stop_hook_active ones. It used to be skipped there "to avoid a tight re-announce
        # loop", which is what silenced reports for hours on every turn a foreign hook blocked. The
        # loop it feared is closed by the two-phase stamp instead: a wake that WAS delivered is
        # promoted to surfaced by the block above (in this same run, before this) and so isn't
        # re-announced, while a wake that was never delivered is exactly the thing that must retry.
        # Either way we then fall THROUGH to arm the background watcher below, so a report landing
        # while the lead sits idle awaiting your answer is still caught.
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
        # Backlog row 87: the "approaching heavy" 🟠 line(s) — after the report lines, before the
        # handoff nudge below (see _ctx_warn_lines' own docstring for the once-per-executor/kill-
        # switch rules). Never added to surfaced_keys: like the handoff nudge line, this isn't a
        # report, so nothing here participates in the report-delivery dedupe machinery above.
        try:
            lines += _ctx_warn_lines(lg, cfg, sid)
        except Exception:
            pass  # fail-open — a heads-up line is best-effort, never worth breaking the hook over
        # Handoff nudge — token-first, MB as the secondary "session age" signal: live context
        # (transcript_usage's last_prompt) is the primary trip, exactly like the executor heaviness
        # gate (lead_guard.is_heavy); a heavy transcript-MB-on-disk (never shrinks — compaction
        # shrinks context, not the file) trips it too, since a big MB alone means several
        # compactions in — the lead's memory of the plan is a summary of summaries. Fires ONCE ever
        # per lead (flag file, not the surfaced_keys dedup — it isn't a report) and rides whatever
        # wake already fires below rather than opening a second exit-2 path.
        try:
            if cfg.get("handoff_nudge", True) and not lg.handoff_nudged(STATE_ROOT, sid):
                mb = lg.transcript_mb(transcript_path)
                usage = lg.transcript_usage(transcript_path)
                marker = lg.read_marker(STATE_ROOT, sid)
                window = lg.lead_window_for(marker.get("model"), lg.load_tier_windows(STATE_ROOT))
                token_threshold = lg.lead_nudge_threshold(cfg, window)
                mb_threshold = float(cfg.get("handoff_nudge_mb", 5))
                tokens = usage.get("last_prompt") if usage else None
                tokens_over = tokens is not None and tokens >= token_threshold
                mb_over = mb >= mb_threshold
                if tokens_over or mb_over:
                    lg.mark_handoff_nudged(STATE_ROOT, sid)  # mark FIRST — a crash after this
                                                              # can't double-nudge; worst case one
                                                              # nudge is silently skipped
                    if tokens_over:  # token-first: say the real signal when it's the one that tripped
                        reading = lg.lead_nudge_reading_text(tokens, token_threshold, window)
                    else:
                        reading = (f"~{mb:.1f}MB transcript, past {mb_threshold:g}MB: several "
                                   "compactions in")
                    lines.append(
                        f"  \U0001f501 this lead session is getting heavy ({reading}). "
                        "Consider handing off: write a handoff md, then run /relay:handoff "
                        "<md> — it opens a pre-armed successor and steps this session down."
                    )
                    lg.append_ledger(STATE_ROOT, "handoff_nudged", session_id=sid, mb=round(mb, 1),
                                      tokens=(int(tokens) if tokens is not None else None))
        except Exception:
            pass  # fail-open — the nudge is best-effort, never worth breaking the hook over
        if lines:
            # Row 87: a wake whose lines are ALL 🟠 ctx-warn lines has nothing new to bannerize — the
            # desktop banner for each of those sids already fired (from bin/relay's own
            # `_maybe_warn_ctx_heavy`, under its own `<sid>:ctx-warn` key) — so pass notify_msg=None
            # to skip that tier here rather than duplicate it. Any OTHER mix (a report, a commit, the
            # handoff nudge) still gets its normal summary/banner.
            all_ctx_warn = all(ln.lstrip().startswith(CTX_WARN_MARKER) for ln in lines)
            notify_msg = None if all_ctx_warn else _notify_summary(lines)
            _announce_and_wake(lg, cfg, sid, lines, surfaced_keys, notify_msg,
                               kind="sync", transcript_path=transcript_path)  # exits 2

        # Nothing instant. If an executor is still busy, become a BACKGROUND poller that waits for
        # its report and wakes when it lands. One poller per lead (lock); a later Stop while it runs
        # just exits 0.
        if not (cfg.get("auto_wake", True) and lg.has_inflight_executors(STATE_ROOT, sid)):
            sys.exit(0)
        interval = max(1, int(cfg.get("poll_interval", 5)))
        if not lg.acquire_poll_lock(STATE_ROOT, sid, interval):
            sys.exit(0)  # a poller is already watching
        try:
            deadline = time.time() + max(1, int(cfg.get("poll_seconds", 1800)))
            while time.time() < deadline:
                time.sleep(interval)
                lg.heartbeat_poll_lock(STATE_ROOT, sid)  # proof of life every tick
                if not lg.is_lead(STATE_ROOT, sid):
                    sys.exit(0)  # lead stepped down / session ended while we waited
                rlines, rkeys = _report_lines(lg, sid)
                if rlines:
                    # kind="async": THIS exit-2 is the droppable one (a stale poller firing while
                    # the lead is mid-turn), so its claim is only ever honoured against transcript
                    # evidence that the wake really landed — never trusted on faith.
                    _announce_and_wake(lg, cfg, sid, rlines, rkeys, _notify_summary(rlines),
                                       kind="async", transcript_path=transcript_path)  # exits 2
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
