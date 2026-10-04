"""
`relay handoff --here` — in-place lead rotation on the tmux backend.

  bin/relay  `handoff --here` (refusals, rotation marker, detached helper) and the hidden
             `_rotate-here <pane>` helper (wait for turn end → /clear → wait for the hook → pointer)
  hooks/sessionend_lead_cleanup.py   SessionEnd(clear) + matching marker → tombstone, not clear
  hooks/sessionstart_lead_rearm.py   SessionStart(clear) + valid marker → arm the successor

NO REAL TMUX: every test runs with `tmux_backend._tmux` replaced by a recorder (autouse fixture
below), so nothing here can reach a tmux server, and `subprocess.Popen` is patched wherever the
helper would be launched. The full rotation test drives both hooks in-process from the fake
`/clear` send — a stubbed end-to-end of the whole sequence.

Run: pytest tests/test_rotate_here.py -q
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import tmux_backend  # noqa: E402

START = "sessionstart_lead_rearm.py"
END = "sessionend_lead_cleanup.py"
PANE = "%7"
HANDLE = "tmux:%7"
PRED = "pred-sid-1111"
SUCC = "succ-sid-2222"
OLD_STAMP = "2026-01-01T00:00:00"


# ---- fixtures -----------------------------------------------------------------------------------

class FakeTmux:
    """Stands in for the tmux CLI under `tmux_backend._tmux`: records argv, answers the pane-exists
    probe for live panes, fails everything else harmlessly. `on_send` lets a test react to a typed
    line (the full-rotation test runs the hooks when `/clear` is typed)."""

    def __init__(self):
        self.calls = []
        self.live = {PANE}
        self.on_send = None

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        ok = True
        out = ""
        if args[:1] == ["display-message"] and "-p" in args:
            pane = args[args.index("-t") + 1]
            ok = pane in self.live
            out = pane if ok else ""
        elif args[:1] == ["send-keys"]:
            pane = args[args.index("-t") + 1]
            ok = pane in self.live
            if ok and "-l" in args and self.on_send:
                self.on_send(args[-1])
        elif args[:1] == ["list-panes"]:
            out = "\n".join(sorted(self.live))
        elif args[:1] == ["display-message"]:
            ok = True
        else:
            ok = False
        return subprocess.CompletedProcess(args, 0 if ok else 1, stdout=out, stderr="")

    def typed(self):
        return [(a[a.index("-t") + 1], a[-1]) for a in self.calls
                if a[:1] == ["send-keys"] and "-l" in a]

    def messages(self):
        return [a for a in self.calls if a[:1] == ["display-message"] and "-p" not in a]


@pytest.fixture(autouse=True)
def fake_tmux(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(tmux_backend, "_tmux", fake)
    monkeypatch.setattr(tmux_backend, "enter_gap", lambda text: 0)
    return fake


def load_relay(state_root):
    path = str(REPO_ROOT / "bin" / "relay")
    loader = importlib.machinery.SourceFileLoader("relay_cli_rotate", path)
    spec = importlib.util.spec_from_file_location("relay_cli_rotate", path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["relay_cli_rotate"] = mod
    loader.exec_module(mod)
    mod.STATE_ROOT = state_root
    mod.LEDGER = state_root / "sessions.jsonl"
    mod._probe_model = lambda alias: (None, "disabled in tests")
    mod._cli_version = lambda: "test"
    mod._lead_live_model = lambda sid: None
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RELAY_TERMINAL", "tmux")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return tmp_path


@pytest.fixture
def relay(home):
    return load_relay(home / ".relay-tasks")


def root_of(home):
    return H.state_root(home)


def arm_pred(home, sid=PRED, backend="tmux", handle=HANDLE, **kw):
    root = root_of(home)
    fields = dict(project="webapp", cwd=str(home), tab_label="[Lead] webapp", color=[10, 20, 30],
                  model="opus", backend=backend, iterm_session=handle, tty="/dev/ttys009",
                  plugin_version="0.5.8", stop_hook_timeout=1900, started="2026-09-01T10:00:00",
                  lineage_started="2026-08-01T09:00:00")
    fields.update(kw)
    lg.write_marker(root, sid, **fields)
    lg.update_marker(root, sid, last_active=OLD_STAMP)
    return root


def write_rotation(home, predecessor=PRED, pane=PANE, expires_in=600, memo=True, **extra):
    root = root_of(home)
    memo_path = lg.rotation_dir(root) / f"{pane}-handoff.md"
    if memo:
        memo_path.parent.mkdir(parents=True, exist_ok=True)
        memo_path.write_text("# webapp\nnext steps\n")
    now = time.time()
    rm = {"predecessor": predecessor, "pane": pane, "handle": "tmux:" + pane,
          "project": "webapp", "tab_label": "[Lead] webapp", "color": [10, 20, 30],
          "model": "opus", "cwd": str(home), "lineage_started": "2026-08-01T09:00:00",
          "memo_copy": str(memo_path), "last_active_at_request": OLD_STAMP,
          "created": "x", "created_at": now, "expires_at": now + expires_in}
    rm.update(extra)
    lg.write_rotation_marker(root, pane, rm)
    return rm


def make_exec(home, sid, owner=PRED, status="busy"):
    return H.make_executor(home, sid=sid, owner_lead=owner, status=status, report=None)


def ledger_events(home):
    return H.ledger(home)


def memo_file(tmp_path):
    p = tmp_path / "memo.md"
    p.write_text("# webapp\n\nIn flight: x. Next: y.\n")
    return p


def hook_env(pane=PANE):
    return {"TMUX_PANE": pane} if pane else {}


# ---- relay handoff --here: refusals -------------------------------------------------------------

class TestHandoffHereRefuses:

    def _run(self, relay, memo, monkeypatch, sid=PRED, pane=PANE):
        if sid:
            monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
        if pane:
            monkeypatch.setenv("TMUX_PANE", pane)
        else:
            monkeypatch.delenv("TMUX_PANE", raising=False)
        launched = []
        monkeypatch.setattr(relay.subprocess, "Popen", lambda *a, **k: launched.append((a, k)))
        with pytest.raises(SystemExit) as e:
            sys.argv = ["relay", "handoff", "--here", str(memo)]
            relay.main()
        assert not launched, "a refusal must never launch the helper"
        assert not lg.rotation_dir(relay.STATE_ROOT).exists() or \
            not any(lg.rotation_dir(relay.STATE_ROOT).glob("*.json")), "no marker on refusal"
        return str(e.value)

    def test_not_a_lead(self, relay, home, monkeypatch, tmp_path):
        msg = self._run(relay, memo_file(tmp_path), monkeypatch)
        assert "isn't an armed lead" in msg

    def test_backend_iterm(self, relay, home, monkeypatch, tmp_path):
        arm_pred(home, backend="iterm", handle="w0t0p0:ABC")
        msg = self._run(relay, memo_file(tmp_path), monkeypatch)
        assert "'iterm', not tmux" in msg and "relay handoff <memo>" in msg

    def test_no_tmux_pane(self, relay, home, monkeypatch, tmp_path):
        arm_pred(home)
        msg = self._run(relay, memo_file(tmp_path), monkeypatch, pane=None)
        assert "$TMUX_PANE" in msg and "relay handoff <memo>" in msg

    def test_pane_mismatch(self, relay, home, monkeypatch, tmp_path, fake_tmux):
        arm_pred(home)
        fake_tmux.live.add("%9")
        msg = self._run(relay, memo_file(tmp_path), monkeypatch, pane="%9")
        assert "not the pane this lead armed in" in msg and "relay handoff <memo>" in msg

    def test_dead_pane(self, relay, home, monkeypatch, tmp_path, fake_tmux):
        arm_pred(home)
        fake_tmux.live.clear()
        msg = self._run(relay, memo_file(tmp_path), monkeypatch)
        assert "live tmux pane" in msg

    def test_already_pending(self, relay, home, monkeypatch, tmp_path):
        arm_pred(home)
        write_rotation(home)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", PRED)
        monkeypatch.setenv("TMUX_PANE", PANE)
        monkeypatch.setattr(relay.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
        sys.argv = ["relay", "handoff", "--here", str(memo_file(tmp_path))]
        with pytest.raises(SystemExit) as e:
            relay.main()
        assert "already pending" in str(e.value)


# ---- relay handoff --here: marker + detached helper ----------------------------------------------

class TestHandoffHereQueues:

    def test_marker_memo_and_detached_helper(self, relay, home, monkeypatch, tmp_path, capsys):
        arm_pred(home)
        make_exec(home, "exec-pin", status="busy")
        sj = root_of(home) / "exec-pin" / "session.json"
        sj.write_text(json.dumps({**json.loads(sj.read_text()), "keep": True}))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", PRED)
        monkeypatch.setenv("TMUX_PANE", PANE)
        launched = []

        class P:
            def __init__(self, argv, **kw):
                launched.append((argv, kw))
        monkeypatch.setattr(relay.subprocess, "Popen", P)
        replaced = []
        real_replace = os.replace
        monkeypatch.setattr(lg.os, "replace", lambda a, b: (replaced.append((str(a), str(b))),
                                                             real_replace(a, b))[1])
        memo = memo_file(tmp_path)
        before = time.time()
        sys.argv = ["relay", "handoff", "--here", str(memo)]
        relay.main()
        out = capsys.readouterr().out
        assert "END YOUR TURN NOW" in out

        root = root_of(home)
        rm = lg.read_rotation_marker(root, PANE)
        assert rm["predecessor"] == PRED and rm["pane"] == PANE and rm["handle"] == HANDLE
        assert rm["project"] == "webapp" and rm["tab_label"] == "[Lead] webapp"
        assert rm["color"] == [10, 20, 30] and rm["model"] == "opus"
        assert rm["lineage_started"] == "2026-08-01T09:00:00"
        assert rm["last_active_at_request"] == OLD_STAMP
        assert before <= rm["created_at"] <= time.time()
        assert rm["expires_at"] == pytest.approx(rm["created_at"] + 600)
        # atomic: written through a sibling tmp + os.replace, nothing left behind
        target = str(lg.rotation_marker_path(root, PANE))
        assert any(b == target and a.endswith(".tmp") for a, b in replaced)
        assert not list(lg.rotation_dir(root).glob("*.tmp"))
        # memo copy: authored content + same-pane aftercare, no close-predecessor step
        body = Path(rm["memo_copy"]).read_text()
        assert body.startswith("# webapp") and "successor lead in the same pane" in body
        assert "/relay:list" in body and "close-predecessor" not in body
        assert "exec-pin" in body, "pinned executors are named in the aftercare"
        assert memo.read_text() == "# webapp\n\nIn flight: x. Next: y.\n", "source untouched"
        # helper: `relay _rotate-here <pane>`, detached, output to a log beside the marker
        assert len(launched) == 1
        argv, kw = launched[0]
        assert argv[-2:] == ["_rotate-here", PANE]
        assert kw["start_new_session"] is True and kw["stdin"] == subprocess.DEVNULL
        assert Path(kw["stdout"].name) == lg.rotation_dir(root) / f"{PANE}.log"
        # the lead is untouched until its turn ends
        assert lg.is_lead(root, PRED)
        assert "lead_rotation_requested" in [e["event"] for e in ledger_events(home)]

    def test_launch_failure_drops_marker(self, relay, home, monkeypatch, tmp_path):
        arm_pred(home)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", PRED)
        monkeypatch.setenv("TMUX_PANE", PANE)

        def boom(*a, **k):
            raise OSError("no fork")
        monkeypatch.setattr(relay.subprocess, "Popen", boom)
        sys.argv = ["relay", "handoff", "--here", str(memo_file(tmp_path))]
        with pytest.raises(SystemExit) as e:
            relay.main()
        assert "failed to launch" in str(e.value)
        assert not lg.read_rotation_marker(root_of(home), PANE)
        assert lg.is_lead(root_of(home), PRED)


# ---- hooks ---------------------------------------------------------------------------------------

def marker_state(home, sid):
    root = root_of(home)
    if not lg.marker_path(root, sid).exists():
        return "gone"
    return "tombstoned" if lg.is_tombstoned(lg.read_marker(root, sid)) else "armed"


class TestSessionEndRotation:

    @pytest.mark.parametrize("drv", H.DRIVERS, ids=H.DRIVER_IDS)
    def test_matching_marker_tombstones_rotated_in_place(self, drv, home):
        arm_pred(home)
        write_rotation(home)
        run = drv(END, {"session_id": PRED, "reason": "clear"}, home, env_extra=hook_env())
        assert run.returncode == 0
        assert marker_state(home, PRED) == "tombstoned"
        assert lg.read_marker(root_of(home), PRED)["ended_reason"] == "rotated_in_place"
        assert lg.read_rotation_marker(root_of(home), PANE), "SessionEnd never consumes the marker"

    @pytest.mark.parametrize("case", ["no-marker", "other-predecessor", "expired", "no-pane"])
    def test_without_a_matching_marker_clear_still_hard_clears(self, case, home):
        arm_pred(home)
        if case == "other-predecessor":
            write_rotation(home, predecessor="someone-else")
        elif case == "expired":
            write_rotation(home, expires_in=-1)
        elif case == "no-pane":
            write_rotation(home)
        env = {} if case == "no-pane" else hook_env()
        run = H.run_hook(END, {"session_id": PRED, "reason": "clear"}, home, env_extra=env)
        assert run.returncode == 0
        assert marker_state(home, PRED) == "gone"

    def test_ordinary_clear_in_a_lead_pane_still_unarms(self, home):
        """Regression guard: a tmux lead that types /clear itself (no rotation queued) unarms,
        exactly as before — on BOTH hooks."""
        arm_pred(home)
        H.run_hook(END, {"session_id": PRED, "reason": "clear"}, home, env_extra=hook_env())
        run = H.run_hook(START, {"session_id": SUCC, "source": "clear", "cwd": str(home)}, home,
                         env_extra=hook_env())
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(home, PRED) == "gone" and marker_state(home, SUCC) == "gone"


class TestSessionStartRotation:

    def _rotate(self, home, drv=H.run_hook, env=None):
        return drv(START, {"session_id": SUCC, "source": "clear", "cwd": str(home)}, home,
                   env_extra=hook_env() if env is None else env)

    @pytest.mark.parametrize("drv", H.DRIVERS, ids=H.DRIVER_IDS)
    def test_valid_marker_arms_the_successor(self, drv, home):
        root = arm_pred(home)
        write_rotation(home)
        make_exec(home, "exec-a")
        make_exec(home, "exec-b", status="reported")
        make_exec(home, "exec-done", status="closed")
        make_exec(home, "exec-other", owner="another-lead")
        lg.mark_surfaced(root, PRED, {"exec-a:1", "exec-b:1"})
        (lg.lead_dir(root, PRED) / "plan.json").write_text('{"items": []}')
        # SessionEnd ran first (the real order): predecessor is a rotated_in_place tombstone
        drv(END, {"session_id": PRED, "reason": "clear"}, home, env_extra=hook_env())

        run = self._rotate(home, drv)
        assert run.returncode == 0
        out = json.loads(run.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        succ_memo = lg.lead_dir(root, SUCC) / "handoff.md"
        assert str(succ_memo) in ctx and succ_memo.read_text().startswith("# webapp")

        m = lg.read_marker(root, SUCC)
        assert lg.is_lead(root, SUCC)
        for k, v in {"project": "webapp", "tab_label": "[Lead] webapp", "color": [10, 20, 30],
                     "model": "opus", "backend": "tmux", "iterm_session": HANDLE,
                     "lineage_started": "2026-08-01T09:00:00", "cwd": str(home),
                     "tty": "/dev/ttys009", "rotated_from": PRED}.items():
            assert m[k] == v, k
        assert m["predecessor"] is None, "no predecessor tab to close — it was this pane"

        def owner(sid):
            return json.loads((root / sid / "session.json").read_text())["owner_lead"]
        assert owner("exec-a") == SUCC and owner("exec-b") == SUCC
        assert owner("exec-done") == PRED and owner("exec-other") == "another-lead"
        assert {"exec-a:1", "exec-b:1"} <= set(lg.load_surfaced(root, SUCC))
        assert (lg.lead_dir(root, SUCC) / "plan.json").is_file()

        p = lg.read_marker(root, PRED)
        assert lg.is_tombstoned(p) and p["superseded_by"] == SUCC and p["migrated_to"] == SUCC
        assert not lg.read_rotation_marker(root, PANE), "marker consumed"
        events = [e["event"] for e in ledger_events(home)]
        assert "lead_rotation_armed" in events and events.count("adopted") == 2

    def test_start_before_end_still_rotates(self, home):
        """Order-robust: if SessionStart ever ran before SessionEnd, the predecessor is still armed
        — the hook tombstones it itself, and the later SessionEnd (marker consumed) hard-clears
        the husk, leaving just the successor."""
        root = arm_pred(home)
        write_rotation(home)
        assert self._rotate(home).returncode == 0
        assert lg.is_lead(root, SUCC) and marker_state(home, PRED) == "tombstoned"
        H.run_hook(END, {"session_id": PRED, "reason": "clear"}, home, env_extra=hook_env())
        assert marker_state(home, PRED) == "gone" and lg.is_lead(root, SUCC)

    @pytest.mark.parametrize("case", ["expired", "wrong-predecessor", "other-pane-lead",
                                      "no-marker", "no-pane"])
    def test_invalid_marker_takes_the_hard_clear_path(self, case, home):
        root = arm_pred(home)
        if case == "expired":
            write_rotation(home, expires_in=-1)
        elif case == "wrong-predecessor":
            write_rotation(home, predecessor="ghost-sid")
        elif case == "other-pane-lead":
            arm_pred(home, sid="other-lead", handle="tmux:%99")
            write_rotation(home, predecessor="other-lead")
        elif case in ("no-pane",):
            write_rotation(home)
        run = self._rotate(home, env={} if case == "no-pane" else None)
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(home, SUCC) == "gone", "nothing armed"
        assert marker_state(home, PRED) == "armed", "an invalid marker touches no lead"
        if case in ("expired", "wrong-predecessor", "other-pane-lead"):
            assert not lg.read_rotation_marker(root, PANE), "a stale marker is deleted"
            assert "rotate_here_stale" in [e["event"] for e in ledger_events(home)]

    def test_exception_inside_fails_open_to_hard_clear(self, home, monkeypatch):
        root = arm_pred(home)
        write_rotation(home)

        def boom(*a, **k):
            raise RuntimeError("injected")
        monkeypatch.setattr(lg, "reparent_executors", boom)
        run = self._rotate(home, drv=H.run_hook_inproc)
        assert run.returncode == 0 and run.stdout == ""
        assert marker_state(home, SUCC) == "gone", "a half-armed successor is hard-cleared"
        assert not lg.read_rotation_marker(root, PANE)


# ---- relay _rotate-here ---------------------------------------------------------------------------

@pytest.fixture
def fast(relay, monkeypatch):
    monkeypatch.setattr(relay, "ROTATE_POLL_SECONDS", 0)
    monkeypatch.setattr(relay, "ROTATE_SETTLE_SECONDS", 0)
    return relay


class TestRotateHereHelper:

    def _turn_ends_on_first_poll(self, relay, home, monkeypatch):
        """The lead's Stop hook heartbeats last_active at turn end — simulate exactly that on the
        helper's first idle poll."""
        root = root_of(home)
        state = {"n": 0}
        real_sleep = time.sleep

        def sleep(s):
            state["n"] += 1
            if state["n"] == 1:
                lg.touch_lead(root, PRED)
            real_sleep(0)
        monkeypatch.setattr(relay.time, "sleep", sleep)

    def test_full_rotation_clear_then_pointer(self, fast, home, monkeypatch, fake_tmux):
        relay = fast
        root = arm_pred(home)
        make_exec(home, "exec-a")
        write_rotation(home)

        def on_send(text):
            if text == "/clear":   # Claude Code fires SessionEnd(clear) then SessionStart(clear)
                H.run_hook_inproc(END, {"session_id": PRED, "reason": "clear"}, home,
                                  env_extra=hook_env())
                H.run_hook_inproc(START, {"session_id": SUCC, "source": "clear",
                                          "cwd": str(home)}, home, env_extra=hook_env())
        fake_tmux.on_send = on_send
        self._turn_ends_on_first_poll(relay, home, monkeypatch)

        sys.argv = ["relay", "_rotate-here", PANE]
        relay.main()

        succ_memo = lg.lead_dir(root, SUCC) / "handoff.md"
        assert fake_tmux.typed() == [
            (PANE, "/clear"),
            (PANE, f"Successor lead (in-place rotation). Read {succ_memo} and continue."),
        ]
        assert lg.is_lead(root, SUCC) and not lg.is_lead(root, PRED)
        rot = [e for e in ledger_events(home) if e["event"] == "lead_rotated_in_place"]
        assert rot and rot[0]["predecessor"] == PRED and rot[0]["successor"] == SUCC \
            and rot[0]["pane"] == PANE

    def test_waits_for_the_turn_to_end_before_clearing(self, fast, home, monkeypatch, fake_tmux):
        relay = fast
        root = arm_pred(home)
        write_rotation(home)
        polls = {"n": 0}

        def sleep(s):
            # (time.sleep is global: tmux_backend's own enter beat lands here too)
            if fake_tmux.typed():
                lg.delete_rotation_marker(root, PANE)   # stand-in for the hook consuming it
                return
            polls["n"] += 1
            if polls["n"] == 3:
                lg.touch_lead(root, PRED)
        monkeypatch.setattr(relay.time, "sleep", sleep)
        cleared_at_poll = []
        fake_tmux.on_send = lambda text: cleared_at_poll.append(polls["n"]) if text == "/clear" else None
        sys.argv = ["relay", "_rotate-here", PANE]
        relay.main()
        assert fake_tmux.typed()[0] == (PANE, "/clear")
        # 3 idle polls (heartbeat moves on the 3rd) + 1 settle beat, then /clear — and never before:
        # an early /clear would freeze the count (the fake stops counting once anything is typed).
        assert cleared_at_poll == [4], "/clear only after the heartbeat moved, then the settle beat"

    def test_gives_up_when_the_turn_never_ends(self, fast, home, monkeypatch, fake_tmux):
        relay = fast
        root = arm_pred(home)
        rm = write_rotation(home)
        monkeypatch.setattr(relay, "ROTATE_IDLE_CAP_SECONDS", 0)
        sys.argv = ["relay", "_rotate-here", PANE]
        relay.main()
        assert fake_tmux.typed() == [], "nothing is typed on give-up"
        assert not lg.read_rotation_marker(root, PANE)
        assert not Path(rm["memo_copy"]).exists()
        assert lg.is_lead(root, PRED), "the lead is untouched"
        assert [e for e in ledger_events(home) if e["event"] == "rotate_here_abandoned"]
        msgs = fake_tmux.messages()
        assert msgs and msgs[0][msgs[0].index("-t") + 1] == PANE

    def test_gives_up_when_the_hook_never_consumes(self, fast, home, monkeypatch, fake_tmux):
        relay = fast
        root = arm_pred(home)
        write_rotation(home)
        self._turn_ends_on_first_poll(relay, home, monkeypatch)
        monkeypatch.setattr(relay, "ROTATE_CONSUME_CAP_SECONDS", 0)
        sys.argv = ["relay", "_rotate-here", PANE]
        relay.main()
        assert fake_tmux.typed() == [(PANE, "/clear")], "no pointer without a successor"
        assert not lg.read_rotation_marker(root, PANE)
        ab = [e for e in ledger_events(home) if e["event"] == "rotate_here_abandoned"]
        assert ab and "/relay:mode" in ab[0]["reason"]

    def test_no_marker_is_a_no_op(self, fast, home, fake_tmux):
        sys.argv = ["relay", "_rotate-here", PANE]
        fast.main()
        assert fake_tmux.calls == []


# ---- relay list after a rotation ----------------------------------------------------------------

class TestListAfterRotation:

    def test_one_lead_row_with_the_successor(self, relay, home, capsys, monkeypatch):
        root = arm_pred(home)
        write_rotation(home)
        H.run_hook(END, {"session_id": PRED, "reason": "clear"}, home, env_extra=hook_env())
        H.run_hook(START, {"session_id": SUCC, "source": "clear", "cwd": str(home)}, home,
                   env_extra=hook_env())
        assert lg.is_lead(root, SUCC)
        monkeypatch.chdir(home)
        sys.argv = ["relay", "list"]
        relay.main()
        out = capsys.readouterr().out
        assert SUCC in out and PRED not in out
        assert relay.resolve_sid("webapp") == SUCC, "the project name is not ambiguous"


class TestReparentExtraction:

    def test_bin_relay_wrapper_uses_the_shared_helper(self, relay, home, monkeypatch):
        """`_reparent_executors` is now a thin wrapper over lead_guard.reparent_executors, passing
        its own recolor + ledger — behaviour byte-identical (the existing succession tests pin the
        stamps; this pins the delegation and the recolor call)."""
        make_exec(home, "exec-a")
        recolored = []
        monkeypatch.setattr(relay, "_recolor_executor_tab", lambda s: recolored.append(s["session_id"]))
        assert relay._reparent_executors(PRED, SUCC, "webapp") == ["exec-a"]
        assert recolored == ["exec-a"]
        s = json.loads((root_of(home) / "exec-a" / "session.json").read_text())
        assert s["owner_lead"] == SUCC and s["owner_project"] == "webapp"
        ev = [e for e in ledger_events(home) if e["event"] == "adopted"]
        assert ev == [{"ts": ev[0]["ts"], "event": "adopted", "session_id": "exec-a",
                       "from_lead": PRED, "to_lead": SUCC, "forced": False}]
