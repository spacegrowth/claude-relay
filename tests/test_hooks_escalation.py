"""
Bug-hunt suite: hooks/executor_escalation.py — the EXECUTOR-side single-shot Stop-hook push.

Armed on an executor via `--settings` at spawn (lead_guard.build_escalation_settings), never by the
plugin manifest: an executor launches plain, with no plugin loaded. It is a net UNDER the lead's own
fast path (hooks/stop_lead_watch.py), not a replacement for it.

Oracle:
  1. README.md § "A second layer underneath" (L425-435).
  2. docs/wake-watch-design.md §9 (the design this hook replaced the old watcher with), and
     lib/lead_guard.py's #22/§13 block for the delivery-aware once-per-packet gate.
  3. The hook's own docstring, chiefly "HARD RULE: any error → exit 0. A bug here must never brick
     the executor's normal Stop behavior."

This hook shells out to `relay nudge-lead`, which types into a REAL iTerm tab. Every test here
either stays on a branch that never reaches that call, or loads the hook in-process and intercepts
exactly that one subprocess (TestEscalationSendPath) — `relay whoami --json` is deliberately let
through to the real CLI against the same tmp HOME, so the identity contract is genuinely exercised.
No test may reach the real terminal.

Run: pytest tests/test_hooks_escalation.py -q
"""
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

ESCALATE = "executor_escalation.py"

DRIVERS, DRIVER_IDS = H.DRIVERS, H.DRIVER_IDS

MALFORMED = H.MALFORMED_STDIN


# =================================================================================================
# executor_escalation — the single-shot push
# =================================================================================================

def escalation_ledger(home, sid="exec-1"):
    return lg.load_escalation(H.state_root(home), sid)


class TestEscalationFailOpen:
    """executor_escalation:24 — "HARD RULE: any error → exit 0. A bug here must never brick the
    executor's normal Stop behavior"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("name", sorted(MALFORMED))
    def test_malformed_stdin_is_silent(self, drv, name, tmp_path):
        H.make_executor(tmp_path, owner_lead=None)
        run = drv(ESCALATE, None, tmp_path, raw=MALFORMED[name], argv=("exec-1",))
        assert run.returncode == 0 and run.stdout == ""

    def test_home_unset_is_silent(self, tmp_path):
        run = H.run_hook(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",),
                         env_extra={"HOME": ""})
        assert run.returncode == 0

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_session_relay_does_not_know_is_silent(self, drv, tmp_path):
        """_whoami:37-52 — "Returns ... None on ANY failure ... the caller treats None exactly like
        the old 'not a relay executor session' case: silent, zero impact"."""
        run = drv(ESCALATE, {"session_id": "nobody"}, tmp_path, argv=("nobody",))
        assert run.returncode == 0 and run.stdout == ""
        assert escalation_ledger(tmp_path, "nobody") == {}

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_lead_session_is_not_treated_as_an_executor(self, drv, tmp_path):
        """`relay whoami` reports role=lead there, and _whoami returns None for any non-executor
        role — the escalation hook must never fire for a lead."""
        H.arm_lead(tmp_path, "lead-1")
        run = drv(ESCALATE, {"session_id": "lead-1"}, tmp_path, argv=("lead-1",))
        assert run.returncode == 0 and run.stdout == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_corrupt_session_json_is_silent(self, drv, tmp_path):
        d = H.make_executor(tmp_path, owner_lead=None)
        (d / "session.json").write_text("{ truncated")
        assert drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",)).returncode == 0
        assert escalation_ledger(tmp_path) == {}

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_corrupt_escalation_ledger_is_survived(self, drv, tmp_path):
        """load_escalation:1437-1445 — "or {} if missing/unreadable". A broken ledger must not gate
        the push out AND must not crash the hook."""
        H.make_executor(tmp_path, owner_lead=None)
        (H.state_root(tmp_path) / "exec-1" / "escalation.json").write_text("{ not json")
        assert drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",)).returncode == 0
        assert escalation_ledger(tmp_path)["1"]["status"] == "notified"


class TestEscalationIdentity:
    """executor_escalation:17-22 and :149-155 — "Still learns its NAME from argv[1] ... deriving it
    from Claude Code's own payload is exactly the bug that kept this hook from EVER firing in
    production"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_identity_comes_from_argv_not_the_payload(self, drv, tmp_path):
        """The payload carries the CLAUDE session id, which names no relay state dir at all."""
        H.make_executor(tmp_path, "exec-1", owner_lead=None,
                        claude_session="f0a5e989-ba22-440a-80a2-fe38c5f73146")
        drv(ESCALATE, {"session_id": "f0a5e989-ba22-440a-80a2-fe38c5f73146"}, tmp_path,
            argv=("exec-1",))
        assert escalation_ledger(tmp_path)["1"]["status"] == "notified"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_payload_id_is_the_last_resort_fallback(self, drv, tmp_path):
        """"Payload id is kept only as a last-resort fallback for a settings file written before
        names were passed" (L154-155)."""
        H.make_executor(tmp_path, "exec-1", owner_lead=None)
        drv(ESCALATE, {"session_id": "exec-1"}, tmp_path)     # no argv at all
        assert escalation_ledger(tmp_path)["1"]["status"] == "notified"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_no_identity_anywhere_is_silent(self, drv, tmp_path):
        assert drv(ESCALATE, {}, tmp_path).returncode == 0


class TestEscalationDecisionTree:
    """README:425-435 and escalation_decision:1458-1490 — the four outcomes. None of these tests
    reaches `_push_to_lead`, so no `relay nudge-lead` subprocess is ever attempted."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_no_report_yet_does_nothing(self, drv, tmp_path):
        """L177-179 — "idle mid-work, nothing written yet → nothing to push". This is the common
        Stop: an executor going idle to ask a question must not burn its one shot."""
        H.make_executor(tmp_path, owner_lead="lead-1", report=None)
        H.arm_lead(tmp_path, "lead-1")
        run = drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        assert run.returncode == 0
        assert escalation_ledger(tmp_path) == {}

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_already_surfaced_is_resolved_not_silent(self, drv, tmp_path):
        """§9.6a (L13-15) — "the resolved path LOGS `escalation_resolved` instead of exiting
        silently, so the dedup working and a dead hook don't look identical"."""
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        lg.mark_surfaced(H.state_root(tmp_path), "lead-1", ["exec-1:1"])
        run = drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        assert run.returncode == 0
        assert escalation_ledger(tmp_path)["1"] == {"status": "resolved", "confirmed": True}
        rec = [r for r in H.ledger(tmp_path) if r["event"] == "escalation_resolved"]
        assert rec and rec[0]["packet"] == 1 and rec[0]["owner_lead"] == "lead-1"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_unowned_notifies_the_human_directly(self, drv, tmp_path):
        """"if the report's owning lead has no owner ... you get notified directly" (README:428)."""
        H.make_executor(tmp_path, owner_lead=None)
        run = drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        assert run.returncode == 0
        assert escalation_ledger(tmp_path)["1"]["status"] == "notified"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_owner_missing_notifies_the_human_directly(self, drv, tmp_path):
        """"do NOT assume a marker exists just because owner_lead is non-null"
        (escalation_decision:1466-1468) — a crashed/closed/pruned lead."""
        H.make_executor(tmp_path, owner_lead="a-lead-that-is-gone")
        run = drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        assert run.returncode == 0
        assert escalation_ledger(tmp_path)["1"]["status"] == "notified"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_notification_names_the_executor_and_the_reason(self, drv, tmp_path):
        """_notify_human:55-72 — the fallback banner has to say WHICH executor and WHY nobody
        else will see it."""
        H.make_executor(tmp_path, owner_lead=None)
        drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",), no_notify=False)
        calls = " ".join(H.stub_calls(tmp_path / "stub-calls.log"))
        assert "exec-1" in calls
        assert "no owning lead to notice it" in calls

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_owner_missing_reason_is_distinct(self, drv, tmp_path):
        H.make_executor(tmp_path, owner_lead="ghost")
        drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",), no_notify=False)
        calls = " ".join(H.stub_calls(tmp_path / "stub-calls.log"))
        assert "crashed, closed, or pruned" in calls

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_relay_no_notify_suppresses_the_fallback_banner(self, drv, tmp_path):
        H.make_executor(tmp_path, owner_lead=None)
        drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))   # kill-switch on
        assert H.stub_calls(tmp_path / "stub-calls.log") == []
        assert escalation_ledger(tmp_path)["1"]["status"] == "notified", \
            "a suppressed banner must not change the recorded outcome"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_kill_switch_stops_everything(self, drv, tmp_path):
        """LEAD_DEFAULTS `executor_escalation` (L124-128) — "kill-switch matches the
        auto_wake/notify_on_wake pattern"."""
        H.arm_lead(tmp_path, "lead-1")
        H.write_config(tmp_path, executor_escalation=False)
        H.make_executor(tmp_path, owner_lead="lead-1")
        run = drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        assert run.returncode == 0
        assert escalation_ledger(tmp_path) == {}


class TestOwnerFallback:
    """11b (lead-found, 2026-09-05 22:01): when the recorded owner_lead has gone missing with no
    handoff to re-parent it (a crash, a manual close — 11a's proactive re-parenting is the net
    UNDER this, not a replacement for it), fall back to the SINGLE currently-armed lead whose
    project matches this executor's own owner_project, rather than giving up straight to
    "owner-missing". Never guesses between two same-project candidates."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_finds_the_fallback_and_resolves_through_the_real_hook_subprocess(self, drv, tmp_path):
        """Driven through the REAL hook subprocess end to end: the fallback lookup itself needs no
        interception (it's pure state), and pre-surfacing the report under the fallback keeps this
        on the "resolved" branch — no `nudge-lead` subprocess ever attempted, so this needs none of
        TestEscalationSendPath's in-process interception."""
        H.make_executor(tmp_path, owner_lead="dead-lead", owner_project="webapp")
        H.arm_lead(tmp_path, "lead-new", project="webapp")
        lg.mark_surfaced(H.state_root(tmp_path), "lead-new", ["exec-1:1"])
        run = drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        assert run.returncode == 0
        fallback_rec = [r for r in H.ledger(tmp_path) if r["event"] == "owner_fallback"]
        assert len(fallback_rec) == 1
        assert fallback_rec[0]["old_owner"] == "dead-lead"
        assert fallback_rec[0]["new_owner"] == "lead-new"
        assert fallback_rec[0]["project"] == "webapp"
        resolved_rec = [r for r in H.ledger(tmp_path) if r["event"] == "escalation_resolved"]
        assert resolved_rec and resolved_rec[0]["owner_lead"] == "lead-new"
        assert escalation_ledger(tmp_path)["1"]["status"] == "resolved"

    def test_pushes_to_the_fallback_lead_not_the_dead_owner(self, tmp_path, monkeypatch):
        """The "send" branch, in-process with `nudge-lead` intercepted (same technique
        TestEscalationSendPath uses) — the push must reach the FALLBACK's tab, never the dead
        owner's."""
        H.make_executor(tmp_path, owner_lead="dead-lead", owner_project="webapp")
        H.arm_lead(tmp_path, "lead-new", project="webapp")
        mod = load_escalation_module()
        real_run = mod.subprocess.run
        calls = []
        monkeypatch.setattr(mod, "STATE_ROOT", str(H.state_root(tmp_path)))
        monkeypatch.setattr("sys.argv", [ESCALATE, "exec-1"])
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("RELAY_NO_NOTIFY", "1")

        def fake_run(cmd, **kw):
            if len(cmd) > 1 and cmd[1] == "nudge-lead":
                calls.append(list(cmd))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return real_run(cmd, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "x"})))
        with pytest.raises(SystemExit):
            mod.main()
        assert len(calls) == 1
        assert calls[0][1:3] == ["nudge-lead", "lead-new"]
        rec = [r for r in H.ledger(tmp_path) if r["event"] == "owner_fallback"]
        assert rec and rec[0]["new_owner"] == "lead-new"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_two_same_project_leads_are_never_guessed_between(self, drv, tmp_path):
        H.make_executor(tmp_path, owner_lead="dead-lead", owner_project="webapp")
        H.arm_lead(tmp_path, "lead-a", project="webapp")
        H.arm_lead(tmp_path, "lead-b", project="webapp")
        run = drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        assert run.returncode == 0
        assert H.ledger(tmp_path) == [] or not any(
            r["event"] == "owner_fallback" for r in H.ledger(tmp_path))
        assert escalation_ledger(tmp_path)["1"]["status"] == "notified"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_no_owner_project_recorded_is_not_a_fallback_candidate(self, drv, tmp_path):
        """An executor spawned before owner_project was tracked (or genuinely unowned) must not
        somehow match every lead with no project — find_lead_by_project(None) is a hard no."""
        H.make_executor(tmp_path, owner_lead="dead-lead")   # no owner_project at all
        H.arm_lead(tmp_path, "lead-new", project="webapp")
        run = drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        assert run.returncode == 0
        assert not any(r["event"] == "owner_fallback" for r in H.ledger(tmp_path))
        assert escalation_ledger(tmp_path)["1"]["status"] == "notified"


class TestEscalationOncePerPacketGate:
    """_already_handled:123-138 — "The once-per-packet gate, now delivery-aware (#22)"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_repeated_stop_does_not_re_notify(self, drv, tmp_path):
        """"No grace window, no polling, no retry — it acts once and exits" (README:433)."""
        H.make_executor(tmp_path, owner_lead=None)
        for _ in range(3):
            drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",), no_notify=False)
        assert escalation_ledger(tmp_path) == {"1": {"status": "notified", "confirmed": True}}
        assert len(H.stub_calls(tmp_path / "stub-calls.log")) <= 1

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_new_packet_gets_its_own_shot(self, drv, tmp_path):
        """The ledger is keyed by packet, so packet 2 is not gated by packet 1's entry."""
        H.make_executor(tmp_path, owner_lead=None, packet=1)
        drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        H.make_executor(tmp_path, owner_lead=None, packet=2)
        drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",))
        assert set(escalation_ledger(tmp_path)) == {"1", "2"}

    def test_a_legacy_entry_without_confirmed_reads_as_confirmed(self, tmp_path):
        """_mark:113-115 — "Entries with NO `confirmed` field are legacy and treated as confirmed,
        so this can never resurrect an old packet"."""
        mod = load_escalation_module()
        root = H.state_root(tmp_path)
        H.make_executor(tmp_path, owner_lead="lead-1")
        mod.STATE_ROOT = str(root)
        lg.save_escalation(root, "exec-1", {"1": {"status": "sent"}})
        assert mod._already_handled(lg, "exec-1", 1, "lead-1") is True

    def test_an_unconfirmed_send_re_arms_while_still_unsurfaced(self, tmp_path):
        """"if the report is STILL not in the owning lead's surfaced set, the nudge demonstrably
        did not result in the lead handling it, so a later executor Stop is allowed to push again"
        — the §13 fix (a nudge swallowed by a busy lead's tab)."""
        mod = load_escalation_module()
        root = H.state_root(tmp_path)
        mod.STATE_ROOT = str(root)
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        lg.save_escalation(root, "exec-1", {"1": {"status": "sent", "confirmed": False}})
        assert mod._already_handled(lg, "exec-1", 1, "lead-1") is False

    def test_an_unconfirmed_send_stops_re_arming_once_the_lead_surfaced_it(self, tmp_path):
        """"the lead handled it — stop re-arming": the entry is promoted to confirmed in place."""
        mod = load_escalation_module()
        root = H.state_root(tmp_path)
        mod.STATE_ROOT = str(root)
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        lg.save_escalation(root, "exec-1", {"1": {"status": "sent", "confirmed": False}})
        lg.mark_surfaced(root, "lead-1", ["exec-1:1"])
        assert mod._already_handled(lg, "exec-1", 1, "lead-1") is True
        assert lg.load_escalation(root, "exec-1")["1"]["confirmed"] is True

    @pytest.mark.parametrize("status", ["resolved", "notified", "failed"])
    def test_a_non_send_entry_always_gates_out(self, status, tmp_path):
        """"An entry that never claimed delivery (resolved/notified/failed) ... gates out exactly
        as it always did" — only an unconfirmed `sent` re-arms."""
        mod = load_escalation_module()
        root = H.state_root(tmp_path)
        mod.STATE_ROOT = str(root)
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        lg.save_escalation(root, "exec-1", {"1": {"status": status, "confirmed": False}})
        assert mod._already_handled(lg, "exec-1", 1, "lead-1") is True


def load_escalation_module():
    """hooks/executor_escalation.py as an importable module — the load pattern
    tests/test_lead_guard.py::TestExecutorEscalationHookSendPath already uses, for the branches
    that must be reached without letting a real `relay nudge-lead` run."""
    import importlib.util
    path = str(H.HOOKS_DIR / ESCALATE)
    spec = importlib.util.spec_from_file_location("escalation_under_test_%d" % id(object()), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEscalationSendPath:
    """The ONE branch that shells out to `relay nudge-lead` (which types into a real iTerm tab).
    Driven in-process with that single subprocess call intercepted; `relay whoami --json` is let
    through to the real CLI against the same tmp HOME, so the identity contract is genuinely
    exercised rather than stubbed."""

    def _run(self, tmp_path, monkeypatch, calls, *, argv_name="exec-1", nudge_ok=True,
             payload=None):
        mod = load_escalation_module()
        root = H.state_root(tmp_path)
        real_run = mod.subprocess.run          # captured BEFORE patching, so forwarding can't recurse
        monkeypatch.setattr(mod, "STATE_ROOT", str(root))
        monkeypatch.setattr("sys.argv", [ESCALATE, argv_name])
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("RELAY_NO_NOTIFY", "1")

        def fake_run(cmd, **kw):
            if len(cmd) > 1 and cmd[1] == "nudge-lead":
                calls.append(list(cmd))
                return SimpleNamespace(returncode=0 if nudge_ok else 1, stdout="", stderr="")
            return real_run(cmd, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload or {"session_id": "x"})))
        with pytest.raises(SystemExit) as ei:
            mod.main()
        return ei.value.code

    def test_a_reported_executor_pushes_into_its_owning_leads_tab(self, tmp_path, monkeypatch):
        """README:429-431 — "it types a message straight into the lead's tab — unconditionally"."""
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        calls = []
        assert self._run(tmp_path, monkeypatch, calls) == 0
        assert len(calls) == 1
        assert calls[0][1:3] == ["nudge-lead", "lead-1"]
        assert "exec-1" in calls[0][3] and "packet 001" in calls[0][3]

    def test_the_push_is_unconditional_even_for_a_busy_lead(self, tmp_path, monkeypatch):
        """§9.5b (L7-9) — "injecting mid-turn is harmless ... so the rule is simply 'always send.'"
        This fails if anyone reintroduces a busy/stale guard."""
        root = H.arm_lead(tmp_path, "lead-1")
        m = lg.read_marker(root, "lead-1")
        m["state"], m["state_since"] = "busy", lg.now()
        lg.marker_path(root, "lead-1").write_text(json.dumps(m))
        H.make_executor(tmp_path, owner_lead="lead-1")
        calls = []
        assert self._run(tmp_path, monkeypatch, calls) == 0
        assert len(calls) == 1

    def test_a_successful_push_is_recorded_unconfirmed(self, tmp_path, monkeypatch):
        """#22/§13 (L111-115) — "a push is marked `confirmed: False` until something proves the
        lead actually acted on the report ... Burning the one shot on a nudge that landed in a busy
        lead's tab and was never consumed is exactly what left §13's report unannounced for 95
        minutes"."""
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        self._run(tmp_path, monkeypatch, [])
        assert escalation_ledger(tmp_path)["1"] == {"status": "sent", "confirmed": False}

    def test_a_failed_push_is_recorded_as_failed_not_delivered(self, tmp_path, monkeypatch):
        """_push_to_lead:75-82 (D4) — "a failed push must not be recorded as delivered (the
        once-per-packet ledger used to lie about this)"."""
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        assert self._run(tmp_path, monkeypatch, [], nudge_ok=False) == 0
        assert escalation_ledger(tmp_path)["1"] == {"status": "failed", "confirmed": True}
        rec = [r for r in H.ledger(tmp_path) if r["event"] == "escalation_push_failed"]
        assert rec and rec[0]["packet"] == 1 and rec[0]["owner_lead"] == "lead-1"

    def test_a_failed_push_is_terminal(self, tmp_path, monkeypatch):
        """"`failed` is terminal either way — _already_handled only re-arms an unconfirmed
        `sent`" (L206-207): no retry storm against a lead that cannot be reached."""
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        self._run(tmp_path, monkeypatch, [], nudge_ok=False)
        calls = []
        self._run(tmp_path, monkeypatch, calls)
        assert calls == []

    def test_an_unconfirmed_send_pushes_again_on_a_later_stop(self, tmp_path, monkeypatch):
        """The #22 re-arm, end to end: still unsurfaced → the next executor Stop pushes again."""
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        self._run(tmp_path, monkeypatch, [])
        calls = []
        self._run(tmp_path, monkeypatch, calls)
        assert len(calls) == 1

    def test_a_surfaced_report_stops_the_re_arm(self, tmp_path, monkeypatch):
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        self._run(tmp_path, monkeypatch, [])
        lg.mark_surfaced(H.state_root(tmp_path), "lead-1", ["exec-1:1"])
        calls = []
        self._run(tmp_path, monkeypatch, calls)
        assert calls == []
        assert escalation_ledger(tmp_path)["1"]["confirmed"] is True

    def test_the_queued_packet_delivery_runs_before_the_kill_switch(self, tmp_path, monkeypatch):
        """_deliver_queued:92-102 and L164-170 — "Deliberately ahead of the escalation kill-switch
        and the once-per-packet gate below: queue delivery is a separate feature from the wake push
        and must not inherit its gating"."""
        H.arm_lead(tmp_path, "lead-1")
        d = H.make_executor(tmp_path, owner_lead="lead-1")
        (d / "queue.json").write_text(json.dumps({"packets": []}))
        H.write_config(tmp_path, executor_escalation=False)
        seen = []
        mod = load_escalation_module()
        real_run = mod.subprocess.run
        monkeypatch.setattr(mod, "STATE_ROOT", str(H.state_root(tmp_path)))
        monkeypatch.setattr("sys.argv", [ESCALATE, "exec-1"])
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("RELAY_NO_NOTIFY", "1")

        def fake_run(cmd, **kw):
            seen.append(list(cmd))
            if len(cmd) > 1 and cmd[1] in ("nudge-lead", "_deliver-queued"):
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return real_run(cmd, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "x"})))
        with pytest.raises(SystemExit):
            mod.main()
        assert any(c[1] == "_deliver-queued" for c in seen), \
            "the queue must be delivered even with escalation switched off"
        assert not any(c[1] == "nudge-lead" for c in seen), "...but the push must still be gated"

    def test_no_queue_file_means_no_subprocess_for_it(self, tmp_path, monkeypatch):
        """"Skips the subprocess entirely when there's no queue file — the overwhelmingly common
        Stop"."""
        H.arm_lead(tmp_path, "lead-1")
        H.make_executor(tmp_path, owner_lead="lead-1")
        seen = []
        mod = load_escalation_module()
        real_run = mod.subprocess.run
        monkeypatch.setattr(mod, "STATE_ROOT", str(H.state_root(tmp_path)))
        monkeypatch.setattr("sys.argv", [ESCALATE, "exec-1"])
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("RELAY_NO_NOTIFY", "1")

        def fake_run(cmd, **kw):
            seen.append(list(cmd))
            if len(cmd) > 1 and cmd[1] == "nudge-lead":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return real_run(cmd, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "x"})))
        with pytest.raises(SystemExit):
            mod.main()
        assert not any(c[1] == "_deliver-queued" for c in seen)


class TestDuplicateBannerDedup:
    """8b (lead-found): a user saw BOTH an iTerm banner and an osascript banner for the SAME
    executor report. Two producers can decide to notify for the same report — the lead's own
    Stop-hook wake (hooks/stop_lead_watch.py) and this hook's own escalation push — because this
    wake's own pending→surfaced promotion (proven-delivery only) lags behind the banner it just
    fired, so an escalation push racing in that exact window still sees "not yet surfaced" and
    fires too. `lead_guard.claim_notification` is the fix: a shared per-lead stamp file
    (notified.json) that whichever producer asks FIRST claims; the other stays quiet. Proven here
    both orders, through the wake's REAL hook subprocess; the escalation side is driven in-process
    (same technique as TestEscalationSendPath above) so its `nudge-lead`/notifier subprocess calls
    can be intercepted without ever reaching a real tab or a real desktop banner."""

    def _escalate(self, tmp_path, monkeypatch):
        """Runs executor_escalation.py's main() in-process: `relay whoami --json` reaches the real
        CLI against this tmp HOME (same identity contract TestEscalationSendPath exercises);
        `nudge-lead` and any osascript call are intercepted and recorded instead of reaching a real
        tab or a real desktop banner. Returns the list of intercepted calls."""
        mod = load_escalation_module()
        root = H.state_root(tmp_path)
        real_run = mod.subprocess.run
        calls = []
        monkeypatch.setattr(mod, "STATE_ROOT", str(root))
        monkeypatch.setattr("sys.argv", [ESCALATE, "exec-1"])
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)

        def fake_run(cmd, **kw):
            if len(cmd) > 1 and cmd[1] == "nudge-lead":
                calls.append(list(cmd))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd and cmd[0] == "osascript":
                calls.append(list(cmd))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return real_run(cmd, **kw)

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "x"})))
        with pytest.raises(SystemExit):
            mod.main()
        return calls

    def _notifier_calls(self, calls):
        """Matches EITHER shape this test collects: raw stub-log lines (strings, from the real
        wake subprocess's stub PATH) or intercepted argv lists (from the in-process escalation
        run) — either way, a call whose command names osascript."""
        out = []
        for c in calls:
            if not c:
                continue
            first = c[0] if isinstance(c, (list, tuple)) else c
            if str(first).strip() == "osascript" or (isinstance(c, str) and "osascript" in c):
                out.append(c)
        return out

    def _wake(self, tmp_path):
        H.write_config(tmp_path, auto_close=False)
        return H.run_hook("stop_lead_watch.py", H.stop_payload(tmp_path, sid="lead-1"), tmp_path,
                          no_notify=False)

    def test_wake_first_then_escalation_stays_quiet(self, tmp_path, monkeypatch):
        H.arm_lead(tmp_path, "lead-1", project="webapp")
        H.make_executor(tmp_path, "exec-1", owner_lead="lead-1", status="reported")

        wake_run = self._wake(tmp_path)
        assert wake_run.returncode == H.WAKE
        assert len(self._notifier_calls(H.stub_calls(tmp_path / "stub-calls.log"))) == 1

        esc_calls = self._escalate(tmp_path, monkeypatch)
        assert self._notifier_calls(esc_calls) == [], \
            "the wake already claimed this report — the escalation push must stay quiet"
        assert any(c[1] == "nudge-lead" for c in esc_calls), \
            "the dedupe is banner-only — the text push itself still happens"

    def test_escalation_first_then_wake_stays_quiet(self, tmp_path, monkeypatch):
        H.arm_lead(tmp_path, "lead-1", project="webapp")
        H.make_executor(tmp_path, "exec-1", owner_lead="lead-1", status="reported")

        esc_calls = self._escalate(tmp_path, monkeypatch)
        assert len(self._notifier_calls(esc_calls)) == 1

        wake_run = self._wake(tmp_path)
        assert wake_run.returncode == H.WAKE   # still announces on stdout/stderr — only the
                                                # DESKTOP banner is deduped, not the wake itself
        assert self._notifier_calls(H.stub_calls(tmp_path / "stub-calls.log")) == [], \
            "the escalation push already claimed this report — the wake's banner must stay quiet"
