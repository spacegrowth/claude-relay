"""
Bug-hunt suite: the two session-lifecycle hooks — the lead-arming state machine.

  hooks/sessionend_lead_cleanup.py   — SessionEnd: hard-clear vs tombstone vs leave alone.
  hooks/sessionstart_lead_rearm.py   — SessionStart: revive a tombstone on resume.

Oracle:
  1. docs/lead-arming-durability.md, quoted throughout both hooks' docstrings.
  2. skills/mode/SKILL.md.
  3. The hooks' own docstrings — in particular the state machine spelled out in
     sessionstart_lead_rearm.py:16-20:

         SessionEnd(clear|logout)           → hard clear      (conversation genuinely gone)
         SessionEnd(exit|prompt_input_exit) → tombstone       (a pause, not a death)
         SessionStart(resume)               → REVIVE
         SessionStart(clear)                → hard clear      (context wiped; do not resurrect)
         SessionStart(startup|compact)      → no-op

     and the 2026-07-10 INCIDENT note in sessionend_lead_cleanup.py:9-13: an unconditional
     clear_lead on any SessionEnd payload deleted an armed lead's state during a plugin reload.

The executor-side escalation hook has its own file (tests/test_hooks_escalation.py) purely for
size; it shares this file's harness and constants.

Run: pytest tests/test_hooks_session.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

START = "sessionstart_lead_rearm.py"
END = "sessionend_lead_cleanup.py"

DRIVERS, DRIVER_IDS = H.DRIVERS, H.DRIVER_IDS

MALFORMED = H.MALFORMED_STDIN


def marker_state(home, sid="lead-1"):
    """"gone" | "tombstoned" | "armed" — the three observable states of a lead marker."""
    root = H.state_root(home)
    if not lg.marker_path(root, sid).exists():
        return "gone"
    return "tombstoned" if lg.is_tombstoned(lg.read_marker(root, sid)) else "armed"


# =================================================================================================
# SessionEnd
# =================================================================================================

class TestSessionEndFailOpen:
    """sessionend_lead_cleanup:7 — "Best-effort and silent. HARD RULE: never throw, never block
    session end"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("name", sorted(MALFORMED))
    def test_malformed_stdin_is_silent(self, drv, name, tmp_path):
        """Includes the shapes that parse as JSON but are not objects (`[1,2,3]`, `null`, `42`):
        those reach `payload.get(...)` and raise, which is what exercises this hook's single
        catch-all `except Exception: pass` (sessionend_lead_cleanup:56-57)."""
        H.arm_lead(tmp_path)
        run = drv(END, None, tmp_path, raw=MALFORMED[name])
        assert run.returncode == 0 and run.stdout == "" and run.stderr == ""
        assert marker_state(tmp_path) == "armed", "a payload it can't read must change nothing"

    def test_home_unset_is_silent(self, tmp_path):
        run = H.run_hook(END, {"session_id": "lead-1", "reason": "clear"}, tmp_path,
                         env_extra={"HOME": ""})
        assert run.returncode == 0

    def test_state_root_is_a_file_is_silent(self, tmp_path):
        H.state_root(tmp_path).write_text("not a dir")
        assert H.run_hook(END, {"session_id": "lead-1", "reason": "clear"}, tmp_path).returncode == 0

    def test_no_session_id_touches_nothing(self, tmp_path):
        H.arm_lead(tmp_path)
        run = H.run_hook(END, {"reason": "clear"}, tmp_path)
        assert run.returncode == 0
        assert marker_state(tmp_path) == "armed"
        assert H.ledger_events(tmp_path) == [], "no sid → nothing to attribute, nothing to log"


class TestSessionEndReasonPolicy:
    """The 2026-07-10 incident policy (sessionend_lead_cleanup:9-35): "only clear lead state on
    documented real-end reasons; unknown/missing reasons preserve the marker (fail-safe in favor of
    staying armed)"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("reason", ["clear", "logout"])
    def test_hard_clear_reasons_remove_the_marker(self, drv, reason, tmp_path):
        """"the conversation is genuinely gone. A revived lead would be armed with a model that has
        no idea it's a lead, which is worse than unarmed. HARD CLEAR"."""
        H.arm_lead(tmp_path)
        assert drv(END, {"session_id": "lead-1", "reason": reason}, tmp_path).returncode == 0
        assert marker_state(tmp_path) == "gone"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("reason", ["exit", "prompt_input_exit"])
    def test_resumable_reasons_tombstone_rather_than_delete(self, drv, reason, tmp_path):
        """"RESUMABLE: `--resume` restores the same session_id and the full conversation. A pause,
        not a death. TOMBSTONE." The identity must survive; only the arming drops."""
        root = H.arm_lead(tmp_path, project="proj", cwd="/work", iterm_session="w0t1p0")
        assert drv(END, {"session_id": "lead-1", "reason": reason}, tmp_path).returncode == 0
        assert marker_state(tmp_path) == "tombstoned"
        m = lg.read_marker(root, "lead-1")
        assert m["project"] == "proj" and m["cwd"] == "/work" and m["iterm_session"] == "w0t1p0"
        assert m["ended"] is True and m["ended_at"]
        assert lg.is_lead(root, "lead-1") is False, "a tombstone must not count as armed"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("reason", ["other", "", None, "weird", "reload", 42])
    def test_unknown_reasons_preserve_the_arming(self, drv, reason, tmp_path):
        """THE 2026-07-10 INCIDENT: "during a plugin reload sequence, an armed lead's entire
        lead/<sid>/ dir vanished without the session ending." `"other"` is what headless
        `claude -p` produces (L32-33) — it must not unarm a live lead."""
        H.arm_lead(tmp_path)
        assert drv(END, {"session_id": "lead-1", "reason": reason}, tmp_path).returncode == 0
        assert marker_state(tmp_path) == "armed"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_missing_reason_key_preserves_the_arming(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        assert drv(END, {"session_id": "lead-1"}, tmp_path).returncode == 0
        assert marker_state(tmp_path) == "armed"


class TestSessionEndScoping:

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("reason", ["clear", "exit"])
    def test_only_this_sessions_marker_is_touched(self, drv, reason, tmp_path):
        """"clears the lead marker for THIS session only; must not clear another lead's marker"."""
        root = H.arm_lead(tmp_path, "lead-1")
        lg.write_marker(root, "lead-2", project="other")
        drv(END, {"session_id": "lead-1", "reason": reason}, tmp_path)
        assert marker_state(tmp_path, "lead-2") == "armed"
        assert lg.is_lead(root, "lead-2") is True

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("reason", ["clear", "exit"])
    def test_an_executor_session_ending_is_a_no_op_on_state(self, drv, reason, tmp_path):
        """An executor has no lead marker, so there is nothing to clear — and its own state dir
        must survive untouched (its report is the lead's to review)."""
        H.arm_lead(tmp_path, "lead-1")
        d = H.make_executor(tmp_path, "exec-1", status="reported")
        drv(END, {"session_id": "exec-1", "reason": reason}, tmp_path)
        assert (d / "session.json").exists()
        assert (d / "packets" / "001-report.md").exists()
        assert marker_state(tmp_path, "lead-1") == "armed"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_hard_clear_is_idempotent(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        drv(END, {"session_id": "lead-1", "reason": "clear"}, tmp_path)
        assert drv(END, {"session_id": "lead-1", "reason": "clear"}, tmp_path).returncode == 0
        assert marker_state(tmp_path) == "gone"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_tombstoning_is_idempotent_and_keeps_the_first_timestamp(self, drv, tmp_path):
        """tombstone_lead:707-722 returns False for an already-tombstoned marker "so callers can
        stay quiet" — a second exit must not restamp ended_at or re-log."""
        root = H.arm_lead(tmp_path)
        drv(END, {"session_id": "lead-1", "reason": "exit"}, tmp_path)
        first = lg.read_marker(root, "lead-1")["ended_at"]
        drv(END, {"session_id": "lead-1", "reason": "exit"}, tmp_path)
        assert lg.read_marker(root, "lead-1")["ended_at"] == first
        assert H.ledger_events(tmp_path).count("lead_tombstoned") == 1


class TestSessionEndLedger:
    """sessionend_lead_cleanup:12-13 — "Every SessionEnd is logged to the ledger with its reason
    for future incident attribution"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("reason,was_lead", [("clear", True), ("exit", True), ("other", True)])
    def test_every_session_end_is_logged_with_its_reason(self, drv, reason, was_lead, tmp_path):
        H.arm_lead(tmp_path)
        drv(END, {"session_id": "lead-1", "reason": reason}, tmp_path)
        rec = [r for r in H.ledger(tmp_path) if r["event"] == "session_end"][0]
        assert rec["session_id"] == "lead-1"
        assert rec["reason"] == reason
        assert rec["was_lead"] is was_lead

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_non_lead_session_end_is_logged_as_such(self, drv, tmp_path):
        drv(END, {"session_id": "some-other-session", "reason": "exit"}, tmp_path)
        rec = [r for r in H.ledger(tmp_path) if r["event"] == "session_end"][0]
        assert rec["was_lead"] is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_tombstone_itself_is_logged(self, drv, tmp_path):
        H.arm_lead(tmp_path)
        drv(END, {"session_id": "lead-1", "reason": "prompt_input_exit"}, tmp_path)
        assert H.ledger_events(tmp_path) == ["session_end", "lead_tombstoned"]
        rec = [r for r in H.ledger(tmp_path) if r["event"] == "lead_tombstoned"][0]
        assert rec["reason"] == "prompt_input_exit"


# =================================================================================================
# SessionStart
# =================================================================================================

class TestSessionStartFailOpen:
    """sessionstart_lead_rearm:27 — "HARD RULE: any error → exit 0 (fail open). A bug here must
    never block a session from starting"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("name", sorted(MALFORMED))
    def test_malformed_stdin_is_silent(self, drv, name, tmp_path):
        root = H.arm_lead(tmp_path)
        lg.tombstone_lead(root, "lead-1")
        run = drv(START, None, tmp_path, raw=MALFORMED[name])
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(tmp_path) == "tombstoned"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_unhashable_source_does_not_block_the_session(self, drv, tmp_path):
        """`source` is tested with `in` against two sets (L77, L85), so a LIST value raises
        TypeError: unhashable. That path is the hook's outer `except Exception: pass` (L107-108),
        and the HARD RULE says what it must do — exit 0 and leave the marker exactly as it was.
        A SessionStart hook that raised here would be a session that cannot start."""
        root = H.arm_lead(tmp_path)
        lg.tombstone_lead(root, "lead-1")
        run = drv(START, {"session_id": "lead-1", "source": ["resume"]}, tmp_path)
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(tmp_path) == "tombstoned"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_no_session_id_is_a_no_op_either_way(self, drv, tmp_path):
        """L74-75 — `if not sid: sys.exit(0)`, before any state is touched."""
        root = H.arm_lead(tmp_path)
        lg.tombstone_lead(root, "lead-1")
        run = drv(START, {"source": "resume"}, tmp_path)
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(tmp_path) == "tombstoned"

    def test_home_unset_is_silent(self, tmp_path):
        run = H.run_hook(START, {"session_id": "lead-1", "source": "resume"}, tmp_path,
                         env_extra={"HOME": ""})
        assert run.returncode == 0 and run.stdout == ""

    def test_no_session_id_is_a_no_op(self, tmp_path):
        root = H.arm_lead(tmp_path)
        lg.tombstone_lead(root, "lead-1")
        run = H.run_hook(START, {"source": "resume"}, tmp_path)
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(tmp_path) == "tombstoned"


class TestSessionStartStateMachine:
    """The closed state machine in sessionstart_lead_rearm:16-20, whose `source` values are
    "spiked and verified on this build, not taken from docs" (L22)."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_resume_revives_a_tombstone_losslessly(self, drv, tmp_path):
        """revive_lead:724-742 — "Everything else — project name included — is restored untouched,
        so a resumed lead is indistinguishable from one that never exited"."""
        root = H.arm_lead(tmp_path, project="proj", cwd="/work", iterm_session="w0t1p0",
                          color=(9, 9, 9), predecessor="lead-0")
        before = lg.read_marker(root, "lead-1")
        lg.tombstone_lead(root, "lead-1")
        run = drv(START, {"session_id": "lead-1", "source": "resume"}, tmp_path)
        assert run.returncode == 0
        assert marker_state(tmp_path) == "armed"
        assert lg.is_lead(root, "lead-1") is True
        after = lg.read_marker(root, "lead-1")
        for k in ("project", "cwd", "iterm_session", "color", "predecessor", "started"):
            assert after.get(k) == before.get(k), k
        assert "ended" not in after and "ended_at" not in after

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_revive_announces_itself_on_stdout(self, drv, tmp_path):
        """L93-99 — "Loudness is the point: the original defect was that unarming happened in
        silence. This MUST be stdout — a SessionStart hook's stdout is surfaced as session context;
        its stderr goes nowhere the user will see." """
        root = H.arm_lead(tmp_path, project="claude-relay")
        lg.tombstone_lead(root, "lead-1")
        run = drv(START, {"session_id": "lead-1", "source": "resume"}, tmp_path)
        assert run.stdout.startswith("🚦 [relay] — lead mode restored for this resumed session")
        assert "claude-relay" in run.stdout
        assert "Gate and auto-wake are active again." in run.stdout
        assert run.stderr == "", "stderr goes nowhere on SessionStart — nothing may be written there"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_clear_hard_clears_and_does_not_resurrect(self, drv, tmp_path):
        """"SessionStart(clear) → hard clear (context wiped; do not resurrect)" — L77-83: "the
        model returns with no lead context, so a marker or tombstone left behind would be actively
        wrong"."""
        root = H.arm_lead(tmp_path)
        lg.tombstone_lead(root, "lead-1")
        run = drv(START, {"session_id": "lead-1", "source": "clear"}, tmp_path)
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(tmp_path) == "gone"
        rec = [r for r in H.ledger(tmp_path) if r["event"] == "lead_cleared_on_start"]
        assert rec and rec[0]["source"] == "clear"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_clear_also_drops_a_live_armed_marker(self, drv, tmp_path):
        """`/clear` wipes the conversation whether or not the session had exited first."""
        H.arm_lead(tmp_path)
        drv(START, {"session_id": "lead-1", "source": "clear"}, tmp_path)
        assert marker_state(tmp_path) == "gone"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("source", ["startup", "compact", None, "other", ""])
    def test_other_sources_are_explicit_no_ops(self, drv, source, tmp_path):
        """"SessionStart(startup|compact) → no-op". L23-25: compact "fires SessionStart but NEVER
        SessionEnd, so it can't unarm anything. This hook still runs on every compaction, so it
        must stay cheap and EXPLICITLY no-op there"."""
        root = H.arm_lead(tmp_path)
        lg.tombstone_lead(root, "lead-1")
        run = drv(START, {"session_id": "lead-1", "source": source}, tmp_path)
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(tmp_path) == "tombstoned"
        assert H.ledger_events(tmp_path) == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_compaction_never_unarms_a_live_lead(self, drv, tmp_path):
        """The compaction case for an ARMED lead — the one that runs constantly in real use."""
        H.arm_lead(tmp_path)
        for _ in range(3):
            assert drv(START, {"session_id": "lead-1", "source": "compact"},
                       tmp_path).returncode == 0
        assert marker_state(tmp_path) == "armed"


class TestSessionStartDoesNotArmStrangers:
    """"re-arms only when the marker's tab/identity matches; must NOT arm a fresh unrelated
    session" — revive_lead "Returns True ONLY if a tombstone was actually revived, so a plain
    fresh start stays a silent no-op"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("source", ["resume", "startup", "clear", "compact"])
    def test_a_session_with_no_marker_is_never_armed(self, drv, source, tmp_path):
        run = drv(START, {"session_id": "a-brand-new-session", "source": source}, tmp_path)
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(tmp_path, "a-brand-new-session") == "gone"
        assert H.ledger_events(tmp_path) == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_resuming_one_lead_does_not_revive_another(self, drv, tmp_path):
        """Two tombstoned leads on one machine: resuming lead-1 must leave lead-2 tombstoned."""
        root = H.arm_lead(tmp_path, "lead-1")
        lg.write_marker(root, "lead-2")
        lg.tombstone_lead(root, "lead-1")
        lg.tombstone_lead(root, "lead-2")
        drv(START, {"session_id": "lead-1", "source": "resume"}, tmp_path)
        assert marker_state(tmp_path, "lead-1") == "armed"
        assert marker_state(tmp_path, "lead-2") == "tombstoned"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_resuming_an_already_armed_lead_is_a_silent_no_op(self, drv, tmp_path):
        """No tombstone to revive → nothing announced and nothing ledgered, so a resume that
        never went through SessionEnd doesn't produce a spurious "restored" banner."""
        root = H.arm_lead(tmp_path)
        run = drv(START, {"session_id": "lead-1", "source": "resume"}, tmp_path)
        assert run.returncode == 0 and run.stdout == ""
        assert H.ledger_events(tmp_path) == []
        assert lg.is_lead(root, "lead-1") is True

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_executor_session_is_never_armed_as_a_lead(self, drv, tmp_path):
        H.make_executor(tmp_path, "exec-1")
        run = drv(START, {"session_id": "exec-1", "source": "resume"}, tmp_path)
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(tmp_path, "exec-1") == "gone"


class TestSessionEndStartRoundTrip:
    """The two halves as one closed loop (sessionstart_lead_rearm:12-20) — this is the regression
    the whole tombstone design exists to prevent: "The resumed session came back silently
    unarmed: routing gate off, wake structurally impossible"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("reason", ["exit", "prompt_input_exit"])
    def test_exit_then_resume_comes_back_armed(self, drv, reason, tmp_path):
        root = H.arm_lead(tmp_path, project="proj")
        drv(END, {"session_id": "lead-1", "reason": reason}, tmp_path)
        assert lg.is_lead(root, "lead-1") is False
        drv(START, {"session_id": "lead-1", "source": "resume"}, tmp_path)
        assert lg.is_lead(root, "lead-1") is True
        assert H.ledger_events(tmp_path) == ["session_end", "lead_tombstoned", "lead_rearmed"]

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_clear_then_resume_stays_unarmed(self, drv, tmp_path):
        """A hard-cleared lead is gone for good: `--resume` on a `/clear`ed conversation must not
        silently re-arm a model that no longer knows it is a lead."""
        root = H.arm_lead(tmp_path)
        drv(END, {"session_id": "lead-1", "reason": "clear"}, tmp_path)
        drv(START, {"session_id": "lead-1", "source": "resume"}, tmp_path)
        assert lg.is_lead(root, "lead-1") is False
        assert marker_state(tmp_path) == "gone"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_revived_lead_is_gated_again(self, drv, tmp_path):
        """The point of re-arming: the routing gate comes back on. A tombstoned lead's edits pass;
        the same edit after the resume is denied."""
        root = H.arm_lead(tmp_path)
        big = {"session_id": "lead-1", "tool_name": "Write",
               "tool_input": {"file_path": str(tmp_path / "n.py"), "content": "x"}}
        drv(END, {"session_id": "lead-1", "reason": "exit"}, tmp_path)
        assert H.is_deny(drv("pretool_route_guard.py", big, tmp_path)) is False
        drv(START, {"session_id": "lead-1", "source": "resume"}, tmp_path)
        assert H.is_deny(drv("pretool_route_guard.py", big, tmp_path)) is True
        assert lg.is_lead(root, "lead-1") is True

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_revived_lead_can_be_woken_again(self, drv, tmp_path):
        """The other half of the original defect: "wake structurally impossible (every hook
        fast-exits on is_lead)". After the resume the Stop hook must wake it."""
        H.arm_lead(tmp_path, project="proj")
        H.write_config(tmp_path)
        H.make_executor(tmp_path, status="reported")
        stop = {"session_id": "lead-1", "cwd": str(tmp_path)}
        drv(END, {"session_id": "lead-1", "reason": "exit"}, tmp_path)
        assert drv("stop_lead_watch.py", stop, tmp_path).returncode == 0
        drv(START, {"session_id": "lead-1", "source": "resume"}, tmp_path)
        assert drv("stop_lead_watch.py", stop, tmp_path).returncode == 2


class TestSessionStartRearmNotification:
    """_notify_rearm:41-61 — "the ONLY channel that reaches the human here ... Honours the same
    `notify_on_wake` config and `RELAY_NO_NOTIFY` kill-switch as every other relay notification"."""

    def test_relay_no_notify_suppresses_the_rearm_banner(self, tmp_path):
        root = H.arm_lead(tmp_path, project="proj")
        lg.tombstone_lead(root, "lead-1")
        run = H.run_hook(START, {"session_id": "lead-1", "source": "resume"}, tmp_path)
        assert run.stdout.startswith("🚦 [relay]")      # the model-facing half still fires
        assert H.stub_calls(tmp_path / "stub-calls.log") == []

    def test_notify_on_wake_false_suppresses_the_rearm_banner(self, tmp_path):
        root = H.arm_lead(tmp_path, project="proj")
        H.write_config(tmp_path, notify_on_wake=False)
        lg.tombstone_lead(root, "lead-1")
        H.run_hook(START, {"session_id": "lead-1", "source": "resume"}, tmp_path, no_notify=False)
        assert H.stub_calls(tmp_path / "stub-calls.log") == []

    def test_the_rearm_does_post_a_banner_by_default(self, tmp_path):
        """Positive control. The subtitle must say what actually happened — _notify:66-69: without
        an explicit `subtitle` "the default below would mislabel every notification as 'review
        needed'"."""
        root = H.arm_lead(tmp_path, project="proj")
        H.write_config(tmp_path)
        lg.tombstone_lead(root, "lead-1")
        H.run_hook(START, {"session_id": "lead-1", "source": "resume"}, tmp_path, no_notify=False)
        calls = " ".join(H.stub_calls(tmp_path / "stub-calls.log"))
        assert "terminal-notifier" in calls
        assert "-subtitle lead re-armed on resume" in calls
        assert "review needed" not in calls
        assert "relay · proj" in calls

    def test_a_failed_notification_never_affects_arming(self, tmp_path):
        """"Best-effort; a notification failure must never affect arming" (L103-104). With NO
        terminal-notifier and NO osascript on PATH at all, the re-arm must still happen."""
        root = H.arm_lead(tmp_path, project="proj")
        H.write_config(tmp_path)
        lg.tombstone_lead(root, "lead-1")
        empty = tmp_path / "emptybin"
        empty.mkdir()
        run = H.run_hook(START, {"session_id": "lead-1", "source": "resume"}, tmp_path,
                         no_notify=False, env_extra={"PATH": str(empty)})
        assert run.returncode == 0
        assert lg.is_lead(root, "lead-1") is True
