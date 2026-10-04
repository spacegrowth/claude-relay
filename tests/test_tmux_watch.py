"""
Seeing an executor under tmux (bin/relay + hooks): the screen verdict behind `relay check`'s
`screen:` line and `relay list`'s stuck:* status, `relay peek`, the pane.log transcript, and the
lead poller's instant wake (stop_lead_watch's `wait-for` tick + executor_escalation's signal).
Every tmux call is mocked — the real-tmux half lives in tests/test_e2e_tmux.py.
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from test_relay import load_relay_module  # noqa: E402
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

sys.path.insert(0, str(TESTS.parent / "scripts"))
import tmux_backend  # noqa: E402


@pytest.fixture
def relay(tmp_path):
    return load_relay_module(tmp_path / ".relay-tasks")


# ── captured-screen fixtures ──────────────────────────────────────────────────────────────────────

BASH_PERMISSION = """\
⏺ I'll push the branch now.

⏺ Bash(git push origin wt/x)
  ⎿  Running…

╭──────────────────────────────────────────────────────────────────────────────╮
│ Bash command                                                                 │
│                                                                              │
│   git push origin wt/x                                                       │
│   Push the feature branch                                                    │
│                                                                              │
│ Do you want to proceed?                                                      │
│ ❯ 1. Yes                                                                     │
│   2. Yes, and don't ask again for git push commands in /Users/v/wt           │
│   3. No, and tell Claude what to do differently (esc)                        │
╰──────────────────────────────────────────────────────────────────────────────╯
"""

EDIT_PERMISSION = """\
 Edit file
 bin/relay
 ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  12 -    old line
  12 +    new line
 Do you want to make this edit to relay?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No, and tell Claude what to do differently (esc)

 Esc to cancel · Tab to add additional instructions
"""

SELECTION = """\
☐ Approach

Which layout should the split use?

❯ 1. Side by side
     Panes split horizontally
  2. Stacked
     Panes split vertically
  3. Type something.

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

SHELL_AFTER_EXIT = """\
⏺ Report written to /Users/v/.relay-tasks/e/packets/001-report.md.

Resume this session with:
claude --resume 0f2a9c4e-0000-4000-8000-000000000000
vamsi@box:~/development/wt$
"""

WORKING = """\
⏺ Running the targeted tests.

✻ Pondering… (42s · ↑ 1.3k tokens · esc to interrupt)

────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

UNSENT = """\
⏺ Done. Idle, awaiting the lead's review.

╭──────────────────────────────────────────────────────────────────────────────╮
│ > Task — GOAL: fix the thing. Read and follow the work packet at             │
│ /Users/v/.relay-tasks/e/packets/002-packet.md — it contains your task,       │
│ GATES, and REPORT FORMAT.                                                    │
╰──────────────────────────────────────────────────────────────────────────────╯
  ? for shortcuts
"""

# The pointer was SUBMITTED: it renders up in the scrollback, and the box below is empty.
IDLE_SUBMITTED = """\
> Read and follow the work packet at /Users/v/.relay-tasks/e/packets/002-packet.md — it contains
  your task.

⏺ Done. Idle, awaiting the lead's review.

────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
"""

# The model's own prose that happens to contain ONE prompt phrase — not a dialog.
PROSE = """\
⏺ Do you want to also rotate the logs? I left that out of scope.

────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
"""

NEEDLES = ("Read and follow the work packet at", "002-packet.md")


@pytest.mark.parametrize("screen,command,verdict,excerpt_has", [
    (BASH_PERMISSION, "node", "prompt", "Do you want to proceed?"),
    (EDIT_PERMISSION, "claude", "prompt", "Do you want to make this edit to relay?"),
    (SELECTION, "2.0.14", "prompt", "Enter to select"),
    (SHELL_AFTER_EXIT, "bash", "crashed", "[bash] vamsi@box:~/development/wt$"),
    (SHELL_AFTER_EXIT, "-zsh", "crashed", "[zsh]"),
    (WORKING, "node", "working", "esc to interrupt"),
    (UNSENT, "node", "unsent", "Task — GOAL: fix the thing."),
    (IDLE_SUBMITTED, "node", "unknown", ""),
    (PROSE, "node", "unknown", ""),
    ("", None, "unknown", ""),
], ids=["bash-permission", "edit-permission", "selection", "shell-after-exit", "login-shell",
        "working", "unsent", "idle-submitted", "prose", "blank"])
def test_classify_screen_table(relay, screen, command, verdict, excerpt_has):
    got, excerpt = relay._classify_screen(screen, command, NEEDLES)
    assert got == verdict, (got, excerpt)
    assert excerpt_has in excerpt
    assert "\n" not in excerpt and len(excerpt) <= 100


def test_a_versioned_native_binary_is_not_a_crash(relay):
    """Native Claude Code runs as a binary named after its version — only a SHELL means exited."""
    assert relay._classify_screen(WORKING, "2.1.3", NEEDLES)[0] == "working"


def test_unsent_needs_the_current_packets_pointer(relay):
    s = {"current_packet": 7}
    assert relay._classify_screen(UNSENT, "node", ("007-packet.md",))[0] == "unknown"
    assert "007-packet.md" in relay._screen_needles(s)


# ── _screen_verdict: tmux only ────────────────────────────────────────────────────────────────────

def _tmux_session(relay, sid="exec-1", status="busy", **extra):
    s = {"session_id": sid, "status": status, "current_packet": 2, "backend": "tmux",
         "iterm_session": "tmux:%3", "tab_label": "[Exec] e", "topic": "e", "scope": "e",
         "worktree": "/nonexistent/wt"}
    s.update(extra)
    relay.write_session(sid, s)
    return s


@pytest.fixture
def screen(monkeypatch):
    """Patch the tmux backend's two reads; returns a dict the test fills in."""
    st = {"screen": BASH_PERMISSION, "command": "node", "calls": []}

    def capture(handle, lines=40):
        st["calls"].append(("capture", handle, lines))
        return st["screen"]

    def current_command(handle):
        st["calls"].append(("command", handle))
        return st["command"]
    monkeypatch.setattr(tmux_backend, "capture", capture)
    monkeypatch.setattr(tmux_backend, "current_command", current_command)
    return st


def test_screen_verdict_reads_a_tmux_pane(relay, screen):
    s = _tmux_session(relay)
    assert relay.backend.by_name("tmux") is tmux_backend
    v = relay._screen_verdict(s)
    assert v == {"verdict": "prompt", "excerpt": "Do you want to proceed?"}
    assert ("capture", "tmux:%3", 40) in screen["calls"]


@pytest.mark.parametrize("extra", [{"backend": "iterm", "iterm_session": "w0t0p0:x"},
                                   {"backend": "terminal", "iterm_session": "twid:3"},
                                   {"iterm_session": None}])
def test_screen_verdict_is_none_off_tmux(relay, screen, extra):
    s = _tmux_session(relay, **extra)
    assert relay._screen_verdict(s) is None
    assert screen["calls"] == []


def test_screen_verdict_is_none_when_the_pane_is_gone(relay, screen):
    screen["screen"] = None
    assert relay._screen_verdict(_tmux_session(relay)) is None


# ── relay check / relay list surfaces ─────────────────────────────────────────────────────────────

def _quiet_sweeps(relay, monkeypatch):
    monkeypatch.setattr(relay, "deliver_queued", lambda *a, **k: None)
    monkeypatch.setattr(relay, "auto_close_sweep", lambda *a, **k: [])
    monkeypatch.setattr(relay, "refresh_live_board", lambda *a, **k: None)
    monkeypatch.setattr(relay, "_armed_lead_caller", lambda *a, **k: None)
    monkeypatch.setattr(relay, "_usage_for_session", lambda *a, **k: None)
    monkeypatch.setattr(relay, "_check_one", lambda sid: dict(relay.read_session(sid)))


def test_check_prints_the_screen_line_for_a_tmux_session(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay)
    relay.cmd_check(SimpleNamespace(all=False, session_id="exec-1", json=False))
    out = capsys.readouterr().out
    assert "screen: prompt — Do you want to proceed?" in out


def test_check_json_carries_the_screen(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay)
    relay.cmd_check(SimpleNamespace(all=False, session_id="exec-1", json=True))
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["screen"] == {"verdict": "prompt", "excerpt": "Do you want to proceed?"}


def test_check_prints_no_screen_line_off_tmux(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay, backend="iterm", iterm_session="w0t0p0:x")
    relay.cmd_check(SimpleNamespace(all=False, session_id="exec-1", json=False))
    assert "screen:" not in capsys.readouterr().out
    assert screen["calls"] == []


def test_check_skips_a_closed_session(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay, status="closed")
    relay.cmd_check(SimpleNamespace(all=False, session_id="exec-1", json=False))
    assert "screen:" not in capsys.readouterr().out


def _list(relay, capsys):
    relay.cmd_list(SimpleNamespace(lead=None, all=False, json=False, closed=False, all_leads=False))
    return capsys.readouterr().out


@pytest.mark.parametrize("scr,cmd,want", [(BASH_PERMISSION, "node", "stuck:prompt"),
                                          (SHELL_AFTER_EXIT, "zsh", "stuck:crashed")],
                         ids=["prompt", "crashed"])
def test_list_shows_stuck_regardless_of_the_stall_threshold(relay, screen, monkeypatch, capsys,
                                                            scr, cmd, want):
    _quiet_sweeps(relay, monkeypatch)
    screen["screen"], screen["command"] = scr, cmd
    import time as _t
    _tmux_session(relay, busy_since_epoch=_t.time() - 60)   # busy 1 minute — far under 45
    assert want in _list(relay, capsys)


def test_list_keeps_busy_when_the_screen_is_working(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    screen["screen"] = WORKING
    import time as _t
    _tmux_session(relay, busy_since_epoch=_t.time() - 60)
    out = _list(relay, capsys)
    assert "stuck:" not in out and "busy 1m" in out


def test_list_never_reads_a_non_tmux_pane(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay, backend="iterm", iterm_session="w0t0p0:x")
    assert "stuck:" not in _list(relay, capsys)
    assert screen["calls"] == []


# ── relay peek ─────────────────────────────────────────────────────────────────────────────────────

def test_peek_prints_the_screen(relay, screen, capsys):
    _tmux_session(relay)
    screen["screen"] = WORKING + "\n\n\n"
    relay.cmd_peek(SimpleNamespace(session_id="exec-1", lines=12, log=False))
    out = capsys.readouterr().out
    assert "esc to interrupt" in out and not out.endswith("\n\n")
    assert ("capture", "tmux:%3", 12) in screen["calls"]


def test_peek_off_tmux_says_it_needs_tmux(relay, screen, capsys):
    _tmux_session(relay, backend="iterm", iterm_session="w0t0p0:x")
    with pytest.raises(SystemExit) as e:
        relay.cmd_peek(SimpleNamespace(session_id="exec-1", lines=40, log=False))
    assert "needs the tmux backend" in str(e.value) and "\n" not in str(e.value)
    assert screen["calls"] == []


def test_peek_log_tails_pane_log_without_escapes(relay, capsys):
    _tmux_session(relay)
    log = relay.pane_log_path("exec-1")
    log.write_bytes(b"".join(b"\x1b[1mrow %d\x1b[0m\r\n" % i for i in range(100)))
    relay.cmd_peek(SimpleNamespace(session_id="exec-1", lines=3, log=True))
    assert capsys.readouterr().out.splitlines() == ["row 97", "row 98", "row 99"]


def test_peek_log_missing(relay):
    _tmux_session(relay)
    with pytest.raises(SystemExit) as e:
        relay.cmd_peek(SimpleNamespace(session_id="exec-1", lines=3, log=True))
    assert "no pane log" in str(e.value)


# ── pane.log: start at spawn, rotate at spawn/send, config switch ─────────────────────────────────

class _PipeSpy:
    NAME = "tmux"

    def __init__(self):
        self.calls = []

    def pipe_pane(self, handle, path):
        self.calls.append(("pipe", handle, path))
        return True

    def unpipe_pane(self, handle):
        self.calls.append(("unpipe", handle))
        return True


def test_pane_log_is_piped(relay):
    _tmux_session(relay)
    bk = _PipeSpy()
    assert relay._pane_log_maintain(bk, "exec-1", "tmux:%3") is True
    assert bk.calls == [("pipe", "tmux:%3", str(relay.pane_log_path("exec-1")))]


def test_pane_log_rotates_past_the_cap(relay, monkeypatch):
    _tmux_session(relay)
    monkeypatch.setattr(relay, "PANE_LOG_MAX_BYTES", 10)
    log = relay.pane_log_path("exec-1")
    log.write_text("x" * 11)
    (log.parent / "pane.log.1").write_text("older")
    bk = _PipeSpy()
    relay._pane_log_maintain(bk, "exec-1", "tmux:%3")
    assert (log.parent / "pane.log.1").read_text() == "x" * 11 and not log.exists()
    assert bk.calls == [("unpipe", "tmux:%3"), ("pipe", "tmux:%3", str(log))]


def test_pane_log_under_the_cap_is_left_alone(relay, monkeypatch):
    _tmux_session(relay)
    monkeypatch.setattr(relay, "PANE_LOG_MAX_BYTES", 10)
    log = relay.pane_log_path("exec-1")
    log.write_text("x" * 10)
    bk = _PipeSpy()
    relay._pane_log_maintain(bk, "exec-1", "tmux:%3")
    assert log.read_text() == "x" * 10 and bk.calls[0][0] == "pipe"


def test_pane_log_config_off(relay, tmp_path):
    _tmux_session(relay)
    cfg = tmp_path / ".relay-tasks" / "lead" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"tmux_pane_log": False}))
    bk = _PipeSpy()
    assert relay._pane_log_maintain(bk, "exec-1", "tmux:%3") is False
    assert bk.calls == []


def test_pane_log_is_tmux_only(relay):
    bk = _PipeSpy()
    bk.NAME = "iterm"
    assert relay._pane_log_maintain(bk, "exec-1", "w0t0p0:x") is False
    assert relay._pane_log_maintain(_PipeSpy(), "exec-1", None) is False


def test_tmux_pane_log_default_is_on():
    assert lg.LEAD_DEFAULTS["tmux_pane_log"] is True


# ── instant wake: the lead poller's tick, and the executor's signal ───────────────────────────────

STOP, ESCALATE = "stop_lead_watch.py", "executor_escalation.py"


def _poller_case(tmp_path, monkeypatch, backend):
    """An armed lead (marker backend `backend`) with one busy executor. The fake wait/sleep land
    the report on their FIRST call, so the poller wakes on the next check either way."""
    H.arm_lead(tmp_path, "lead-1", project="proj", backend=backend)
    H.write_config(tmp_path, poll_seconds=20, poll_interval=3)
    H.make_executor(tmp_path, report=None, status="busy")
    report = H.state_root(tmp_path) / "exec-1" / "packets" / "001-report.md"
    seen = {"wait": [], "sleep": []}

    def fake_wait(channel, timeout):
        seen["wait"].append((channel, timeout))
        report.write_text("landed via wait-for\n")
        return "signalled"

    import time as _time
    real_sleep = _time.sleep

    def fake_sleep(sec):
        seen["sleep"].append(sec)
        if sec == 3:
            report.write_text("landed via sleep\n")
        else:
            real_sleep(sec)
    monkeypatch.setattr(tmux_backend, "wait", fake_wait)
    monkeypatch.setattr(_time, "sleep", fake_sleep)
    return seen


def test_the_poller_waits_on_tmux_wait_for_under_tmux(tmp_path, monkeypatch):
    seen = _poller_case(tmp_path, monkeypatch, "tmux")
    run = H.run_hook_inproc(STOP, H.stop_payload(tmp_path), tmp_path)
    assert run.returncode == H.WAKE and "landed via wait-for" in run.stderr
    assert seen["wait"] == [("relay-wake-lead-1", 3)]
    assert 3 not in seen["sleep"]


@pytest.mark.parametrize("backend", ["iterm", "terminal", None])
def test_the_poller_sleeps_off_tmux(tmp_path, monkeypatch, backend):
    seen = _poller_case(tmp_path, monkeypatch, backend)
    run = H.run_hook_inproc(STOP, H.stop_payload(tmp_path), tmp_path)
    assert run.returncode == H.WAKE and "landed via sleep" in run.stderr
    assert seen["wait"] == [] and 3 in seen["sleep"]   # interval sleep ran (Linux adds a 1 ms one first)


def test_the_poller_falls_back_to_sleep_when_wait_for_errors(tmp_path, monkeypatch):
    seen = _poller_case(tmp_path, monkeypatch, "tmux")
    monkeypatch.setattr(tmux_backend, "wait",
                        lambda ch, t: seen["wait"].append((ch, t)) or "error")
    run = H.run_hook_inproc(STOP, H.stop_payload(tmp_path), tmp_path)
    assert run.returncode == H.WAKE and "landed via sleep" in run.stderr
    assert seen["wait"] == [("relay-wake-lead-1", 3)] and 3 in seen["sleep"]


@pytest.mark.parametrize("drv", H.DRIVERS, ids=H.DRIVER_IDS)
def test_a_tmux_executor_signals_its_lead_when_its_report_is_there(drv, tmp_path):
    H.arm_lead(tmp_path, "lead-1")
    H.make_executor(tmp_path, owner_lead="lead-1", backend="tmux")
    stub = H.stub_bin(tmp_path)
    drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",), stub=stub)
    calls = [c for c in H.stub_calls(stub[1]) if "/tmux " in c]
    assert any(c.endswith("tmux wait-for -S relay-wake-lead-1") for c in calls), calls


@pytest.mark.parametrize("drv", H.DRIVERS, ids=H.DRIVER_IDS)
@pytest.mark.parametrize("case", ["no-report", "iterm", "unowned"])
def test_no_signal_without_a_report_off_tmux_or_unowned(drv, tmp_path, case):
    H.arm_lead(tmp_path, "lead-1")
    H.make_executor(tmp_path, owner_lead=None if case == "unowned" else "lead-1",
                    backend="iterm" if case == "iterm" else "tmux",
                    report=None if case == "no-report" else "done.\n")
    stub = H.stub_bin(tmp_path)
    drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",), stub=stub)
    assert not [c for c in H.stub_calls(stub[1]) if "wait-for" in c]


# ── wiring: spawn and send both keep the pane piped (the helper itself no-ops off tmux) ───────────

def test_spawn_starts_the_pane_log(relay, tmp_path, monkeypatch):
    from unittest import mock
    packet = tmp_path / "p.md"
    packet.write_text("do the thing\n\n## Preconditions\n- none\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    seen = []
    monkeypatch.setattr(relay, "_pane_log_maintain", lambda bk, sid, h: seen.append((sid, h)))
    with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: None), \
         mock.patch.object(relay, "auto_trust"), \
         mock.patch.object(relay, "read_pid", return_value=123), \
         mock.patch.object(relay, "read_iterm_id", return_value="tmux:%9"), \
         mock.patch.object(relay, "_ensure_tab_label", return_value=True):
        relay.cmd_spawn(SimpleNamespace(packet=str(packet), topic="foo", name="pl", worktree=str(wt),
                                        model=None, model_override=None, skip_perms=None, pane=None,
                                        lead=None, scope=None))
    assert ("pl", "tmux:%9") in seen


def test_send_checks_the_pane_log_before_typing(relay, tmp_path, monkeypatch):
    from unittest import mock
    sid = "e1"
    relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
    relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
        "tab_label": "relay-" + sid, "model": None, "pid": os.getpid(),
        "iterm_session": "w0t0p0:OLD", "claude_session": "cs-x", "status": "busy",
        "current_packet": 1, "busy_since": relay.now(), "created": relay.now(),
        "updated": relay.now()})
    (relay.packets_dir(sid) / "001-packet.md").write_text("first packet")
    (relay.packets_dir(sid) / "001-report.md").write_text("done")
    packet = tmp_path / "next.md"
    packet.write_text("# Follow-up\n\n## Preconditions\n- none\n\nDo the next thing.")
    order = []
    monkeypatch.setattr(relay, "_pane_log_maintain", lambda bk, s, h: order.append(("log", s, h)))
    monkeypatch.setattr(relay, "_send_confirmed",
                        lambda *a, **k: order.append(("send",)) or (True, True))
    with mock.patch.object(relay.iterm, "is_alive", return_value=True):
        relay.cmd_send(SimpleNamespace(session_id=sid, packet=str(packet)))
    assert order[:2] == [("log", sid, "w0t0p0:OLD"), ("send",)]
