"""
Bug-hunt suite: the two PreToolUse hooks, driven as real subprocesses with crafted stdin payloads
and a tmp HOME.

  hooks/pretool_route_guard.py — matcher Edit|Write|MultiEdit, the lead-only inline-edit gate.
  hooks/pretool_bash_gate.py   — matcher Bash, the lead-only LOGGING-ONLY verb classifier.

Oracle, in the shared bug-hunt priority order:
  1. README.md "## The routing gate (friction, not trust)" (L312-322) and its config table (L633,
     L635).
  2. skills/mode/SKILL.md "What the gate does and does NOT cover" (L82-91).
  3. Each hook's own module docstring, chiefly the HARD RULE: "any error, missing file,
     unparseable payload, or unexpected shape → exit 0 (allow)".

Findings are recorded in tests/bughunt/hooks-findings.md; a test that asserts the DOCUMENTED
behaviour where the code disagrees carries an xfail(strict=True) naming the BUG id.

Run: pytest tests/test_hooks_pretool.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

ROUTE = "pretool_route_guard.py"
BASH = "pretool_bash_gate.py"

# Both drivers must agree on every one of these behaviours: the subprocess one IS the contract
# (Claude Code launches a process), the in-process one is the same script with `coverage` able to
# see it. Parametrising over both is what keeps the cheap driver honest.
DRIVERS, DRIVER_IDS = H.DRIVERS, H.DRIVER_IDS


def big(n):
    """A `new_string`/`content` body of exactly n lines."""
    return "\n".join("line %d" % i for i in range(n))


def write_payload(file_path, content):
    return {"session_id": "lead-1", "tool_name": "Write",
            "tool_input": {"file_path": str(file_path), "content": content}}


def edit_payload(file_path, new_string):
    return {"session_id": "lead-1", "tool_name": "Edit",
            "tool_input": {"file_path": str(file_path), "new_string": new_string}}


def existing(tmp_path, name="already_here.py"):
    p = tmp_path / name
    p.write_text("original\n")
    return p


# =================================================================================================
# HARD RULE — fail open. "any error, missing file, unparseable payload, or unexpected shape →
# exit 0 (allow). A broken hook must never brick normal Claude Code usage."
# (hooks/pretool_route_guard.py:8-9, hooks/pretool_bash_gate.py:11-13)
# =================================================================================================

MALFORMED = H.MALFORMED_STDIN


class TestPreToolFailOpen:
    """HARD RULE: nothing a malformed payload or a hostile environment can do may produce a
    non-zero exit or a deny.

    The hostile-ENVIRONMENT cases (HOME unset, HOME a file, state root a file) run through the
    REAL subprocess driver only: they turn on what `os.path.expanduser("~")` resolves to at
    interpreter start, which a same-process test cannot honestly reproduce. The malformed-PAYLOAD
    cases run through both, since there the two drivers are genuinely equivalent."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("hook", [ROUTE, BASH])
    @pytest.mark.parametrize("name", sorted(MALFORMED))
    def test_malformed_stdin_allows_silently(self, drv, hook, name, tmp_path):
        """route_guard:8 / bash_gate:11 — "unparseable payload ... → exit 0 (allow)"."""
        H.arm_lead(tmp_path)  # armed, so nothing is short-circuited by the not-a-lead fast path
        run = drv(hook, None, tmp_path, raw=MALFORMED[name])
        assert run.returncode == 0, run.stderr
        assert run.stdout.strip() == ""

    @pytest.mark.parametrize("hook", [ROUTE, BASH])
    def test_missing_session_id_allows(self, hook, tmp_path):
        """route_guard:35 / bash_gate:38 — `if not sid ... sys.exit(0)`: with no session id there
        is no lead to check, so the hook must take the zero-impact path."""
        H.arm_lead(tmp_path)
        run = H.run_hook(hook, {"tool_name": "Write",
                                "tool_input": {"file_path": str(tmp_path / "x.py"),
                                               "content": big(200)}}, tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""

    @pytest.mark.parametrize("hook", [ROUTE, BASH])
    def test_missing_tool_input_allows(self, hook, tmp_path):
        """"unexpected shape → exit 0". An armed lead with no `tool_input` at all must not crash
        and must not deny."""
        H.arm_lead(tmp_path)
        run = H.run_hook(hook, {"session_id": "lead-1", "tool_name": "Write"}, tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("hook", [ROUTE, BASH])
    def test_tool_input_wrong_type_allows(self, drv, hook, tmp_path):
        """`tool_input` as a list/string instead of an object. A NON-EMPTY list survives the code's
        `payload.get("tool_input", {}) or {}` guard and then raises on `.get(...)` — so this is the
        case that actually exercises the outer `except Exception: sys.exit(0)` hard fail-open
        (route_guard:81-82, bash_gate:55-56), not just the guard in front of it."""
        H.arm_lead(tmp_path)
        for bad in ([1, 2], "a string", 7, None):
            run = drv(hook, {"session_id": "lead-1", "tool_name": "Write",
                             "tool_input": bad}, tmp_path)
            assert run.returncode == 0 and run.stdout.strip() == "", bad

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_non_numeric_threshold_in_config_allows(self, drv, tmp_path):
        """exceeds_gate:363-368 compares `lines >= config["edit_line_threshold"]` directly, so a
        hand-edited config with a STRING threshold raises inside the guard. The HARD RULE decides
        what happens next: allow, never block on a config the user got wrong."""
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, edit_line_threshold="40")
        run = drv(ROUTE, write_payload(tmp_path / "n.py", big(400)), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""

    @pytest.mark.parametrize("hook", [ROUTE, BASH])
    def test_home_unset_allows(self, hook, tmp_path):
        """No HOME at all: `os.path.expanduser("~")` cannot resolve a state root, so the hook must
        fall through to allow rather than raise."""
        run = H.run_hook(hook, write_payload(tmp_path / "x.py", big(200)), tmp_path,
                         env_extra={"HOME": ""})
        assert run.returncode == 0 and run.stdout.strip() == ""

    @pytest.mark.parametrize("hook", [ROUTE, BASH])
    def test_home_is_a_file_allows(self, hook, tmp_path):
        """HOME pointing at a regular file makes every state path unopenable — still exit 0."""
        f = tmp_path / "not-a-dir"
        f.write_text("x")
        run = H.run_hook(hook, write_payload(tmp_path / "x.py", big(200)), tmp_path,
                         env_extra={"HOME": str(f)})
        assert run.returncode == 0 and run.stdout.strip() == ""

    @pytest.mark.parametrize("hook", [ROUTE, BASH])
    def test_state_root_absent_allows(self, hook, tmp_path):
        """No `~/.relay-tasks` at all — the overwhelmingly common case on any machine that has
        never run relay. README:320-322 "every other session on the machine is untouched"."""
        run = H.run_hook(hook, write_payload(tmp_path / "x.py", big(200)), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""
        assert not H.state_root(tmp_path).exists()

    @pytest.mark.parametrize("hook", [ROUTE, BASH])
    def test_state_root_is_a_file_allows(self, hook, tmp_path):
        """`~/.relay-tasks` existing as a FILE breaks every path join under it."""
        H.state_root(tmp_path).write_text("this is not a directory")
        run = H.run_hook(hook, write_payload(tmp_path / "x.py", big(200)), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""

    def test_read_only_state_root_still_allows_a_small_edit(self, tmp_path):
        """An unwritable state root makes the ledger append fail. The gate's decision must not
        depend on being able to write — a small edit still passes silently."""
        root = H.arm_lead(tmp_path)
        target = existing(tmp_path)
        os.chmod(root, 0o500)
        try:
            run = H.run_hook(ROUTE, edit_payload(target, big(3)), tmp_path)
            assert run.returncode == 0 and run.stdout.strip() == ""
        finally:
            os.chmod(root, 0o700)


# =================================================================================================
# Route guard — the zero-impact path
# =================================================================================================

class TestRouteGuardZeroImpact:
    """README:320-322 — "it only acts in /relay:mode sessions — every other session on the machine
    is untouched (the hook fast-exits, fail-open)"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_non_lead_session_is_never_gated(self, drv, tmp_path):
        """No marker for this session id → not a lead → allow, even for a huge new file."""
        run = drv(ROUTE, write_payload(tmp_path / "huge.py", big(500)), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_another_leads_marker_does_not_gate_this_session(self, drv, tmp_path):
        """Arming is per-session-id: a machine with lead-1 armed must leave lead-2's (and every
        executor's) edits alone."""
        H.arm_lead(tmp_path, "lead-1")
        payload = write_payload(tmp_path / "huge.py", big(500))
        payload["session_id"] = "some-other-session"
        run = drv(ROUTE, payload, tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_tombstoned_lead_is_not_gated(self, drv, tmp_path):
        """lead_guard.is_lead:441-446 — "A TOMBSTONED marker counts as NOT a lead ... until a
        resume revives it, the gate and the wake must stay off"."""
        root = H.arm_lead(tmp_path)
        assert lg.tombstone_lead(root, "lead-1") is True
        run = drv(ROUTE, write_payload(tmp_path / "huge.py", big(500)), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""


# =================================================================================================
# Route guard — the threshold
# =================================================================================================

class TestRouteGuardThreshold:
    """README:633 — `edit_line_threshold` 40, "Block ... if it adds this many lines or more"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("lines,denied", [(1, False), (39, False), (40, True), (41, True)])
    def test_edit_line_threshold_boundary(self, drv, lines, denied, tmp_path):
        """"this many lines or more" — 39 passes, 40 blocks. On an EXISTING file, so the
        new-file rule can't be what decides it."""
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        run = drv(ROUTE, edit_payload(target, big(lines)), tmp_path)
        assert run.returncode == 0
        assert H.is_deny(run) is denied

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_configured_threshold_is_honoured(self, drv, tmp_path):
        """The threshold is config-driven (README:633), not hardcoded in the hook."""
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, edit_line_threshold=5)
        target = existing(tmp_path)
        assert H.is_deny(drv(ROUTE, edit_payload(target, big(4)), tmp_path)) is False
        assert H.is_deny(drv(ROUTE, edit_payload(target, big(5)), tmp_path)) is True

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_multiedit_sums_its_edits(self, drv, tmp_path):
        """lead_guard.edit_line_count:311 — "MultiEdit → sum of each edit's new_string". Ten
        four-line edits are forty lines: many small edits in one call must not slip the gate."""
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        payload = {"session_id": "lead-1", "tool_name": "MultiEdit",
                   "tool_input": {"file_path": str(target),
                                  "edits": [{"new_string": big(4)} for _ in range(10)]}}
        run = drv(ROUTE, payload, tmp_path)
        assert H.is_deny(run) is True
        assert "40 lines" in H.deny_reason(run)

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_multiedit_below_the_sum_passes(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        payload = {"session_id": "lead-1", "tool_name": "MultiEdit",
                   "tool_input": {"file_path": str(target),
                                  "edits": [{"new_string": big(4)} for _ in range(9)]}}
        assert H.is_deny(drv(ROUTE, payload, tmp_path)) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_multiedit_with_junk_entries_degrades_to_allow(self, drv, tmp_path):
        """edit_line_count:317 counts only dict entries — "Any unexpected shape degrades to 0
        (fail-open ... under-counting is the safe direction)"."""
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        payload = {"session_id": "lead-1", "tool_name": "MultiEdit",
                   "tool_input": {"file_path": str(target),
                                  "edits": ["not a dict", 5, None, {"no_new_string": "x"}]}}
        assert H.is_deny(drv(ROUTE, payload, tmp_path)) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_write_to_existing_file_is_sized_not_auto_blocked(self, drv, tmp_path):
        """A Write over an EXISTING path is not a new file, so only the line count decides —
        `block_on_new_file` must not swallow the overwrite case."""
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        assert H.is_deny(drv(ROUTE, write_payload(target, big(5)), tmp_path)) is False
        assert H.is_deny(drv(ROUTE, write_payload(target, big(60)), tmp_path)) is True

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_new_file_blocked_however_small(self, drv, tmp_path):
        """README:314 — the gate blocks "over ~40 new lines, OR creating a new file"."""
        H.arm_lead(tmp_path)
        run = drv(ROUTE, write_payload(tmp_path / "brand_new.py", "one line"), tmp_path)
        assert H.is_deny(run) is True
        assert "new file" in H.deny_reason(run)

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_block_on_new_file_can_be_turned_off(self, drv, tmp_path):
        """`block_on_new_file` is a documented config key (LEAD_DEFAULTS:30); with it off a small
        new file is a small edit."""
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, block_on_new_file=False)
        assert H.is_deny(drv(ROUTE, write_payload(tmp_path / "n.py", "one"), tmp_path)) is False
        # ...but the line threshold still applies to it.
        assert H.is_deny(drv(ROUTE, write_payload(tmp_path / "n2.py", big(80)), tmp_path)) is True


# =================================================================================================
# Route guard — the deny decision's own shape
# =================================================================================================

class TestRouteGuardDenyShape:
    """route_guard:7-8 — "to block, print a permissionDecision:"deny" JSON on stdout and exit 0"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_deny_json_shape_and_exit_code(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        run = drv(ROUTE, write_payload(tmp_path / "n.py", big(90)), tmp_path)
        assert run.returncode == 0            # a deny is exit 0 + stdout, never a non-zero exit
        out = json.loads(run.stdout)
        assert set(out) == {"hookSpecificOutput"}
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert isinstance(hso["permissionDecisionReason"], str) and hso["permissionDecisionReason"]

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_deny_reason_names_both_escape_hatches(self, drv, tmp_path):
        """README:314-315 and SKILL.md:84-85: the block must tell the lead to delegate OR to run
        `/relay:route retain`. A block with no way forward is the failure mode this text exists to
        prevent."""
        H.arm_lead(tmp_path)
        reason = H.deny_reason(drv(ROUTE, write_payload(tmp_path / "n.py", big(90)), tmp_path))
        assert "/relay:spawn" in reason and "/relay:send" in reason
        assert "/relay:route retain" in reason

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_deny_reason_states_the_honest_bash_limit(self, drv, tmp_path):
        """README:320 / SKILL.md:85-88 — the gate does NOT cover Bash, and says so at the moment
        it blocks, which is the only moment the lead is reading it."""
        H.arm_lead(tmp_path)
        reason = H.deny_reason(drv(ROUTE, write_payload(tmp_path / "n.py", big(90)), tmp_path))
        assert "Bash" in reason and "NOT gated" in reason

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_deny_reason_reports_the_measured_size(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        assert "47 lines" in H.deny_reason(drv(ROUTE, edit_payload(target, big(47)), tmp_path))

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_allow_is_silent_never_a_nag(self, drv, tmp_path):
        """route_guard:55 — "small review-class fix → silent allow, never nag". Stdout must be
        EMPTY, not an `allow` decision: anything on stdout is a decision Claude Code will act on."""
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        run = drv(ROUTE, edit_payload(target, big(3)), tmp_path)
        assert run.stdout == "" and run.stderr == ""


# =================================================================================================
# Route guard — the packet-file exemption
# =================================================================================================

class TestRouteGuardExemption:
    """SKILL.md:89-91 — "Packet files are exempt: anything under `~/.relay-tasks/` or named
    `*-packet.md` passes the gate freely — writing packets IS the lead's job"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_new_packet_file_under_state_root_passes(self, drv, tmp_path):
        root = H.arm_lead(tmp_path)
        p = root / "bh-hooks" / "packets" / "002-packet.md"
        assert H.is_deny(drv(ROUTE, write_payload(p, big(400)), tmp_path)) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_any_new_file_under_state_root_passes(self, drv, tmp_path):
        """"anything under ~/.relay-tasks/" — not just files matching the packet name."""
        root = H.arm_lead(tmp_path)
        assert H.is_deny(drv(ROUTE, write_payload(root / "notes.md", big(400)), tmp_path)) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_packet_named_file_anywhere_passes(self, drv, tmp_path):
        """"or named *-packet.md WHEREVER the lead chose to draft it" (lead_guard:346-348)."""
        H.arm_lead(tmp_path)
        p = tmp_path / "scratch" / "007-packet.md"
        p.parent.mkdir()
        assert H.is_deny(drv(ROUTE, write_payload(p, big(400)), tmp_path)) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_tilde_paths_are_expanded_before_the_exemption_check(self, drv, tmp_path):
        """lead_guard.is_gate_exempt:349 calls `.expanduser()`. A `~`-relative packet path must be
        recognised as being under the state root, not treated as a literal `./~` directory."""
        H.arm_lead(tmp_path)
        payload = write_payload("~/.relay-tasks/bh/packets/001-packet.md", big(400))
        assert H.is_deny(drv(ROUTE, payload, tmp_path)) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_tilde_path_outside_state_root_is_still_gated(self, drv, tmp_path):
        """The mirror of the case above: expanduser must not turn every `~` path into an exemption."""
        H.arm_lead(tmp_path)
        assert H.is_deny(drv(ROUTE, write_payload("~/ordinary_new_file.py", "x"), tmp_path)) is True

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_symlink_into_state_root_is_exempt(self, drv, tmp_path):
        """is_gate_exempt resolves before comparing, so a symlinked drafting dir that really lives
        under the state root is exempt — the check is about where the bytes land, not the spelling."""
        root = H.arm_lead(tmp_path)
        (root / "drafts").mkdir(parents=True, exist_ok=True)
        link = tmp_path / "drafts-link"
        link.symlink_to(root / "drafts")
        assert H.is_deny(drv(ROUTE, write_payload(link / "big.md", big(400)), tmp_path)) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_lookalike_names_are_not_exempt(self, drv, tmp_path):
        """`*-packet.md` is a suffix rule; `packet.md`, `001-packet.py` and `x-packet.md.bak` are
        ordinary files and must still be gated."""
        H.arm_lead(tmp_path)
        for name in ("packet.md", "001-packet.py", "x-packet.md.bak", "packet-001.md"):
            run = drv(ROUTE, write_payload(tmp_path / name, big(400)), tmp_path)
            assert H.is_deny(run) is True, name

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_exemption_beats_the_line_threshold_too(self, drv, tmp_path):
        """Exempt means exempt: an EXISTING packet file rewritten with 400 lines still passes."""
        root = H.arm_lead(tmp_path)
        p = root / "bh" / "packets" / "001-packet.md"
        p.parent.mkdir(parents=True)
        p.write_text("old\n")
        assert H.is_deny(drv(ROUTE, edit_payload(p, big(400)), tmp_path)) is False


# =================================================================================================
# Route guard — the retain grace window
# =================================================================================================

class TestRouteGuardGraceWindow:
    """README:315-316 / config table:635 — `/relay:route retain "<reason>"` opens a ~2-minute
    (`grace_seconds`, 120) window; route_guard:38-40 lets everything through while it is open."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_open_grace_window_allows_a_gated_edit(self, drv, tmp_path):
        root = H.arm_lead(tmp_path)
        lg.set_grace(root, "lead-1", 120)
        assert H.is_deny(drv(ROUTE, write_payload(tmp_path / "n.py", big(400)), tmp_path)) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_expired_grace_window_gates_again(self, drv, tmp_path):
        """The window EXPIRES — a retain is not a permanent unarm."""
        root = H.arm_lead(tmp_path)
        lg.set_grace(root, "lead-1", -1)          # already in the past
        assert H.is_deny(drv(ROUTE, write_payload(tmp_path / "n.py", big(400)), tmp_path)) is True

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_grace_is_per_session_not_machine_wide(self, drv, tmp_path):
        """lead_guard.grace_path:233 files the window under `lead/<sid>/`. One lead's retain must
        never open the gate for a different lead on the same machine."""
        root = H.arm_lead(tmp_path, "lead-1")
        lg.write_marker(root, "lead-2")
        lg.set_grace(root, "lead-1", 120)
        payload = write_payload(tmp_path / "n.py", big(400))
        payload["session_id"] = "lead-2"
        assert H.is_deny(drv(ROUTE, payload, tmp_path)) is True

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_corrupt_grace_file_falls_back_to_gating(self, drv, tmp_path):
        """in_grace:664-674 swallows a bad stamp and returns False. An unreadable window must
        close the gate, not open it — fail-open here would mean fail-UNARMED."""
        root = H.arm_lead(tmp_path)
        lg.set_grace(root, "lead-1", 120)
        lg.grace_path(root, "lead-1").write_text("not-a-timestamp")
        assert H.is_deny(drv(ROUTE, write_payload(tmp_path / "n.py", big(400)), tmp_path)) is True


# =================================================================================================
# Route guard — the ledger
# =================================================================================================

class TestRouteGuardLedger:
    """README:317-318 — "Every block and retain is logged to ~/.relay-tasks/sessions.jsonl"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_block_is_ledgered_with_its_evidence(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        target = tmp_path / "cli.py"
        drv(ROUTE, write_payload(target, big(90)), tmp_path)
        recs = [r for r in H.ledger(tmp_path) if r["event"] == "blocked"]
        assert len(recs) == 1
        r = recs[0]
        assert r["session_id"] == "lead-1"
        assert r["file_path"] == str(target)
        assert r["lines"] == 90 and r["new_file"] is True
        assert "ts" in r

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_allowed_edits_are_not_ledgered(self, drv, tmp_path):
        """Only blocks are events. A silent allow that logged would turn the ledger into a diary
        of every keystroke."""
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        drv(ROUTE, edit_payload(target, big(3)), tmp_path)
        assert [r for r in H.ledger(tmp_path) if r["event"] == "blocked"] == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_in_grace_edits_are_not_ledgered_as_blocked(self, drv, tmp_path):
        root = H.arm_lead(tmp_path)
        lg.set_grace(root, "lead-1", 120)
        drv(ROUTE, write_payload(tmp_path / "n.py", big(400)), tmp_path)
        assert [r for r in H.ledger(tmp_path) if r["event"] == "blocked"] == []


# =================================================================================================
# Route guard — unicode / very long values
# =================================================================================================

class TestRouteGuardOddInputs:

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_unicode_path_and_content(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        target = tmp_path / "ünïcodé — 日本語.py"
        content = "\n".join("héllo 🚦 %d" % i for i in range(90))
        run = drv(ROUTE, write_payload(target, content), tmp_path)
        assert H.is_deny(run) is True
        assert H.ledger(tmp_path)[-1]["file_path"] == str(target)

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_very_long_single_line_is_one_line(self, drv, tmp_path):
        """The gate counts LINES, not bytes (lead_guard._count_lines:303). A 200KB one-liner into
        an existing file is a one-line edit and must pass — this is what keeps a long regex or a
        minified blob from reading as "delegate this"."""
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        assert H.is_deny(drv(ROUTE, edit_payload(target, "x" * 200000), tmp_path)) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_empty_content_write_to_existing_file_passes(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        target = existing(tmp_path)
        assert H.is_deny(drv(ROUTE, write_payload(target, ""), tmp_path)) is False


# =================================================================================================
# BUG-hooks-1 — a corrupt lead marker turns the gate ON, not off
# =================================================================================================

class TestRouteGuardCorruptMarker:

    @pytest.mark.xfail(strict=True, reason="BUG-hooks-1: an unparseable lead marker makes is_lead "
                                           "return True, so the gate DENIES instead of failing open")
    @pytest.mark.parametrize("drv", [H.run_hook], ids=["subprocess"])
    def test_corrupt_marker_fails_open(self, drv, tmp_path):
        """CONTRACT — hooks/pretool_route_guard.py:8-9: "HARD RULE: any error, missing file,
        unparseable payload, or UNEXPECTED SHAPE → exit 0 (allow). A broken hook must never brick
        normal Claude Code usage." And lead_guard.is_lead:438-439: "Marker absent (OR ANY ERROR) →
        not lead → the hooks fast-exit-allow".

        ACTUAL — is_lead:447-451 tests `marker_path(...).exists()` and then
        `not is_tombstoned(read_marker(...))`; read_marker:499-504 swallows the JSON error and
        returns `{}`, `is_tombstoned({})` is False, so a truncated marker reads as a LIVE ARMED
        LEAD and this edit is denied. A half-written marker (crash mid-write, full disk) therefore
        leaves the lead unable to edit anything, with a message that blames its edit size."""
        root = H.arm_lead(tmp_path)
        lg.marker_path(root, "lead-1").write_text('{"session_id": "lead-1", "proj')  # truncated
        run = drv(ROUTE, write_payload(tmp_path / "n.py", big(400)), tmp_path)
        assert run.returncode == 0
        assert H.is_deny(run) is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_corrupt_marker_current_behaviour_is_at_least_exit_zero(self, drv, tmp_path):
        """Whatever BUG-hooks-1 resolves to, the non-negotiable half of the HARD RULE holds: the
        hook exits 0 and never crashes the tool call."""
        root = H.arm_lead(tmp_path)
        lg.marker_path(root, "lead-1").write_text("{not json at all")
        assert drv(ROUTE, write_payload(tmp_path / "n.py", big(400)), tmp_path).returncode == 0


# =================================================================================================
# Bash gate — LOGGING ONLY
# =================================================================================================

def bash_payload(command, sid="lead-1"):
    return {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": command}}


def would_have_blocked(home):
    return [r for r in H.ledger(home) if r["event"] == "would_have_blocked"]


class TestBashGateNeverDenies:
    """bash_gate:6-10 — "LOGGING ONLY (dry-run-first) — this hook NEVER denies; it always allows,
    whether or not it logs." Blocking mode ships later. README:320 says the same from the user's
    side: Bash is not gated."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("cmd", [
        "npm install left-pad",
        "npm ci",
        "npm run build",
        "pip3 install requests",
        "yarn add react",
        "gcc -o x x.c",
        "cargo build --release",
        "git clone https://example.com/r.git",
        "sed -i '' s/a/b/ f.txt",
        "cat <<EOF > /etc/systemd/system/x.service",
        "echo hi | tee /tmp/x",
        "rsync -a a/ b/",
    ])
    def test_implementation_verbs_are_logged_but_allowed(self, drv, cmd, tmp_path):
        H.arm_lead(tmp_path)
        run = drv(BASH, bash_payload(cmd), tmp_path)
        assert run.returncode == 0
        assert run.stdout.strip() == "", "the Bash gate must never emit a decision"
        assert len(would_have_blocked(tmp_path)) == 1, cmd

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("cmd", [
        'git commit -m "x"',
        "git push origin main",
        "systemctl restart api",
        "systemctl status api",
        "clickhouse-client --query 'select 1'",
        "pytest tests/ -q",
        "npm test",
        "npm run test:unit",
        "go test ./...",
        "cargo test",
        "make test",
        "tox",
        "ls -la",
        "cat README.md",
        "git status",
        "git diff --stat",
        "grep -rn foo lib/",
    ])
    def test_custody_and_read_verbs_leave_no_trace(self, drv, cmd, tmp_path):
        """lead_guard:388-397 — custody verbs free-pass and are "never ledgered"; reads
        (cat/ls/grep/git status/...) "free-pass by construction" because no implementation rule
        matches them."""
        H.arm_lead(tmp_path)
        run = drv(BASH, bash_payload(cmd), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""
        assert would_have_blocked(tmp_path) == [], cmd

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_custody_wins_on_overlap(self, drv, tmp_path):
        """lead_guard:381-387 — "CUSTODY_RULES are checked first and win on overlap ... e.g.
        `npm run build` (implementation) vs `npm test`/`npm run test:*` (custody)". A command that
        matches both must free-pass."""
        H.arm_lead(tmp_path)
        drv(BASH, bash_payload("npm run build && npm test"), tmp_path)
        assert would_have_blocked(tmp_path) == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_make_test_is_custody_but_bare_make_is_implementation(self, drv, tmp_path):
        """The narrowest overlap in the taxonomy: `make` is a compiler verb, `make test` is not."""
        H.arm_lead(tmp_path)
        drv(BASH, bash_payload("make test"), tmp_path)
        assert would_have_blocked(tmp_path) == []
        drv(BASH, bash_payload("make -j8"), tmp_path)
        assert [r["rule"] for r in would_have_blocked(tmp_path)] == ["compiler"]

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_ledger_record_carries_command_and_rule(self, drv, tmp_path):
        """The whole point of phase 1 is tuning the allowlist against real logs (bash_gate:5-7), so
        the record has to say WHICH rule matched and WHAT it matched on."""
        H.arm_lead(tmp_path)
        drv(BASH, bash_payload("npm install left-pad"), tmp_path)
        r = would_have_blocked(tmp_path)[0]
        assert r["session_id"] == "lead-1"
        assert r["rule"] == "npm-install"
        assert r["command"] == "npm install left-pad"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_herestring_is_not_a_heredoc(self, drv, tmp_path):
        """The heredoc rule's `(?<!<)<<(?!<)` guard exists so `<<<` (a here-STRING, a read) is not
        logged as a mutation."""
        H.arm_lead(tmp_path)
        drv(BASH, bash_payload('grep foo <<< "$var"'), tmp_path)
        assert would_have_blocked(tmp_path) == []


class TestBashGateZeroImpact:

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_non_lead_session_writes_nothing_at_all(self, drv, tmp_path):
        """bash_gate:37-39 — "Not a lead session → the entire zero-impact path". Not even the
        state root may be created."""
        run = drv(BASH, bash_payload("npm install left-pad", sid="stranger"), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""
        assert not H.state_root(tmp_path).exists()

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_tombstoned_lead_logs_nothing(self, drv, tmp_path):
        root = H.arm_lead(tmp_path)
        lg.tombstone_lead(root, "lead-1")
        drv(BASH, bash_payload("npm install left-pad"), tmp_path)
        assert would_have_blocked(tmp_path) == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_kill_switch_silences_the_ledger(self, drv, tmp_path):
        """LEAD_DEFAULTS `bash_gate_logging` (lib/lead_guard.py:137-143) — "Flip off to silence the
        ledger without a release"; bash_gate:42-43 exits before writing anything."""
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_gate_logging=False)
        run = drv(BASH, bash_payload("npm install left-pad"), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""
        assert would_have_blocked(tmp_path) == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_logging_is_on_by_default(self, drv, tmp_path):
        """"Default True because logging has no user-visible effect" — with NO config file at all."""
        H.arm_lead(tmp_path)
        drv(BASH, bash_payload("npm install left-pad"), tmp_path)
        assert len(would_have_blocked(tmp_path)) == 1

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_corrupt_config_falls_back_to_defaults(self, drv, tmp_path):
        """load_config:243-245 — "missing/corrupt file → pure defaults"; the kill-switch must not
        be readable as "off" out of a broken file."""
        root = H.arm_lead(tmp_path)
        (root / "lead").mkdir(parents=True, exist_ok=True)
        (root / "lead" / "config.json").write_text("{ this is not json")
        drv(BASH, bash_payload("npm install left-pad"), tmp_path)
        assert len(would_have_blocked(tmp_path)) == 1

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_missing_command_key_is_a_no_op(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        run = drv(BASH, {"session_id": "lead-1", "tool_name": "Bash", "tool_input": {}}, tmp_path)
        assert run.returncode == 0 and would_have_blocked(tmp_path) == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_non_string_command_degrades_to_free_pass(self, drv, tmp_path):
        """classify_bash_command:414-436 — "an unparseable/non-string cmd degrades to None
        (unclassified → free-pass, the safe direction for a logging-only gate)"."""
        H.arm_lead(tmp_path)
        for bad in (None, 5, ["npm", "install"], {"a": 1}):
            run = drv(BASH, {"session_id": "lead-1", "tool_name": "Bash",
                             "tool_input": {"command": bad}}, tmp_path)
            assert run.returncode == 0 and run.stdout.strip() == "", bad
        assert would_have_blocked(tmp_path) == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_unicode_and_very_long_commands_survive(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        cmd = "npm install " + " ".join("päckage-🚦-%d" % i for i in range(2000))
        run = drv(BASH, bash_payload(cmd), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""
        assert would_have_blocked(tmp_path)[0]["command"] == cmd
