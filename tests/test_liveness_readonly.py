"""Executor liveness on Linux/tmux, read-only `relay list`, and nudge-lead's readiness gate.

The incident (Linux box, tmux backend, relay 0.5.11, 2026-10-10): three executors spawned at once;
note-b and note-c read "stalled — process gone, no report; tab still open" from 3 s after spawn
until each reported, and every 10 s `relay list` poll (Cue, over SSH) re-appended that same line —
37 identical ledger lines. What the box showed:
  - both panes at 10:18:38: the bootstrap line, Claude Code's folder-trust dialog with its default
    "❯ No, exit" selected, then a bare shell prompt — claude had EXITED (0.5.11 typed its spawn-time
    `/rename` + Enter blindly into the dialog). "Process gone" was true at first.
  - ~/.claude.json lastStartTime: note-a 10:16:48.392, note-b 10:19:05.675, note-c 10:19:05.682 —
    the bootstrap was re-run by hand in both panes at 10:19:05, which wrote NEW pids to the
    pidfiles. relay reads the pidfile once, at spawn, so it kept polling the dead first pid
    (4126684 / 4126690) while the relaunched claudes worked — and c's last stalled line (10:19:22)
    came after its relaunched claude had started.
`ps -o lstart` itself was NOT the cause: on the box it is stable across locales and sub-second
call phases and agrees with /proc/<pid>/stat exactly (fixtures below). The fixes under test: adopt a
relaunched pid from the pidfile (_relaunched_pid), ledger a process-gone stall only on the
transition, `relay list` computes status without writing anything, and nudge-lead types only into a
pane showing Claude's input box.

Every backend here is a stub — term_backend and relay.iterm are pinned — so no test reaches a real
terminal."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import trust_prompt  # noqa: E402


def load_relay_module(state_root):
    path = str(REPO_ROOT / "bin" / "relay")
    loader = importlib.machinery.SourceFileLoader("relay_cli", path)
    spec = importlib.util.spec_from_file_location("relay_cli", path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["relay_cli"] = mod
    loader.exec_module(mod)
    mod.STATE_ROOT = state_root
    mod.LEDGER = state_root / "sessions.jsonl"
    mod._probe_model = lambda alias: (None, "disabled in tests")
    mod._cli_version = lambda: "test"
    return mod


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return load_relay_module(tmp_path / ".relay-tasks")


def run_main(relay, *argv):
    with mock.patch.object(sys, "argv", ["relay", *argv]):
        relay.main()


# ── real captured outputs ───────────────────────────────────────────────────────────────────────

# Linux box (Ubuntu 24.04, kernel 7.0.0-28, procps-ng 4.0.4, TZ America/Los_Angeles), pid 427262:
#   ps -o lstart= -p 427262   → identical under LC_ALL=C / en_US.UTF-8 / de_DE.UTF-8 / C.UTF-8 and
#                               across 15 calls spread over two seconds
#   /proc/stat btime           → 1786024568
#   /proc/427262/stat field 22 → 505747871 (starttime, clock ticks), getconf CLK_TCK → 100
LINUX_LSTART = "Sat Oct  3 19:47:26 2026"
LINUX_BTIME, LINUX_STARTTIME_TICKS, LINUX_CLK_TCK = 1786024568, 505747871, 100
# macOS (Darwin 25.5), `ps -o lstart= -p <this executor's claude>` — note the trailing blanks.
MACOS_LSTART = "Sat Oct 10 21:55:45 2026    "
MACOS_EPOCH = 1791694545  # 2026-10-10 21:55:45 PDT

# The box's pane %32 at 10:18:38 (`tmux capture-pane -p -S -60`, blank rows dropped): the trust
# dialog, default "No, exit", and the shell prompt claude's exit left behind.
_RULE = "─" * 200
BOX_TRUST_DIALOG = (
    "vamsi@vamsi-ubuntu:/tmp/cue-smoke/b$\n"
    "vamsi@vamsi-ubuntu:/tmp/cue-smoke/b$ sh /home/vamsi/.relay-tasks/note-b-101647/bootstrap.sh %32\n"
    f"{_RULE}\n"
    " Accessing workspace:\n"
    " /tmp/cue-smoke/b\n"
    " Quick safety check: Is this a project you created or one you trust? (Like your own code, a "
    "well-known open source project, or work from your team). If not, take a moment to review "
    "what's in this\n"
    " folder first.\n"
    " Claude Code'll be able to read, edit, and execute files here.\n"
    " Security guide\n"
    " ❯ No, exit\n"
    "   Yes, I trust this folder\n"
    " Enter to confirm · Esc to cancel\n"
)


@pytest.fixture
def pacific(monkeypatch):
    """Both fixtures were captured in America/Los_Angeles; ps prints local time."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class TestLstartFixtures:
    def test_linux_procps_lstart_matches_proc_stat(self, relay, pacific):
        assert relay.lstart_epoch(LINUX_LSTART) == \
            LINUX_BTIME + LINUX_STARTTIME_TICKS // LINUX_CLK_TCK

    def test_macos_lstart_with_trailing_blanks(self, relay, pacific):
        assert relay.lstart_epoch(MACOS_LSTART) == MACOS_EPOCH

    def test_one_digit_day_padding_does_not_matter(self, relay, pacific):
        assert relay.lstart_epoch("Sat Oct  3 19:47:26 2026") == \
            relay.lstart_epoch("Sat Oct 3 19:47:26 2026")

    @pytest.mark.parametrize("junk", [None, "", "garbage", "Sat Oct 99 19:47:26 2026"])
    def test_unparseable_is_none(self, relay, junk):
        assert relay.lstart_epoch(junk) is None

    def test_the_box_screen_is_the_trust_dialog(self):
        assert trust_prompt.launch_state(BOX_TRUST_DIALOG) == "trust"


# ── liveness: a relaunched claude is adopted from the pidfile ──────────────────────────────────

def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


@pytest.fixture
def live_proc():
    p = subprocess.Popen(["sleep", "60"])
    time.sleep(0.05)
    yield p
    p.kill()
    p.wait()


@pytest.fixture
def tab(relay, monkeypatch):
    """Pin term_backend AND relay.iterm/iterm_backend to one stub. `alive` = is the tab open."""
    stub = SimpleNamespace(NAME="tmux", alive=True)
    stub.is_alive = lambda label, handle=None, pid=None: stub.alive
    monkeypatch.setattr(relay, "term_backend", lambda s: stub)
    monkeypatch.setattr(relay, "iterm", stub)
    monkeypatch.setattr(relay, "iterm_backend", stub, raising=False)
    return stub


def _session(relay, sid, pid, status="busy", pid_started="Thu Jan  1 00:00:00 2026", report=False):
    relay.write_session(sid, {
        "session_id": sid, "current_packet": 1, "status": status, "topic": "t", "worktree": "/w",
        "tab_label": f"[Exec] {sid}", "iterm_session": f"tmux:%{abs(hash(sid)) % 90 + 1}",
        "backend": "tmux", "pid": pid, "pid_started": pid_started, "claude_session": None,
        "owner_lead": None, "busy_since": relay.now(), "busy_since_epoch": time.time(),
        "updated": relay.now(),
    })
    d = relay.packets_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "001-packet.md").write_text("do it")
    if report:
        (d / "001-report.md").write_text("Done.\nStatus: clean\n")


def _ledger(relay):
    if not relay.LEDGER.exists():
        return []
    return [json.loads(ln) for ln in relay.LEDGER.read_text().splitlines() if ln.strip()]


class TestRelaunchedPid:
    def test_relaunch_is_adopted_and_the_stall_clears(self, relay, tab, live_proc):
        _session(relay, "e1", _dead_pid(), status="stalled")
        relay.pid_path("e1").write_text(f"{live_proc.pid}\n")   # a re-run bootstrap's `echo $$`
        s = relay._check_one("e1")
        assert s["status"] == "busy"
        assert s["pid"] == live_proc.pid
        assert s["pid_started"] == relay.pid_start_time(live_proc.pid)
        assert relay.read_session("e1")["pid"] == live_proc.pid      # persisted by check
        assert not [e for e in _ledger(relay) if e["event"] == "stalled"]

    def test_read_only_check_adopts_in_memory_only(self, relay, tab, live_proc):
        dead = _dead_pid()
        _session(relay, "e1", dead, status="stalled")
        relay.pid_path("e1").write_text(f"{live_proc.pid}\n")
        assert relay._check_one("e1", persist=False)["status"] == "busy"
        assert relay.read_session("e1")["pid"] == dead

    def test_a_recycled_pid_is_not_adopted(self, relay, tab, live_proc):
        _session(relay, "e1", _dead_pid())
        pf = relay.pid_path("e1")
        pf.write_text(f"{live_proc.pid}\n")
        old = time.time() - 3600       # file written an hour before this process started
        os.utime(pf, (old, old))
        assert relay._relaunched_pid("e1", relay.read_session("e1")) is None
        assert relay._check_one("e1")["status"] == "stalled"

    def test_the_same_dead_pid_in_the_pidfile_is_not_a_relaunch(self, relay, tab):
        dead = _dead_pid()
        _session(relay, "e1", dead)
        relay.pid_path("e1").write_text(f"{dead}\n")
        assert relay._relaunched_pid("e1", relay.read_session("e1")) is None

    @pytest.mark.parametrize("content", [None, "", "not-a-pid"])
    def test_missing_or_garbage_pidfile(self, relay, tab, content):
        _session(relay, "e1", _dead_pid())
        if content is not None:
            relay.pid_path("e1").write_text(content)
        assert relay._relaunched_pid("e1", relay.read_session("e1")) is None

    def test_a_live_recorded_pid_is_untouched(self, relay, tab, live_proc):
        _session(relay, "e1", live_proc.pid, pid_started=relay.pid_start_time(live_proc.pid))
        relay.pid_path("e1").write_text(f"{_dead_pid()}\n")
        s = relay._check_one("e1")
        assert s["status"] == "busy" and s["pid"] == live_proc.pid


class TestStalledLedgeredOnTransitionOnly:
    def test_repeated_checks_write_one_stalled_line(self, relay, tab):
        _session(relay, "e1", _dead_pid())
        for _ in range(4):
            assert relay._check_one("e1")["status"] == "stalled"
        stalled = [e for e in _ledger(relay) if e["event"] == "stalled"]
        assert len(stalled) == 1
        assert stalled[0]["reason"] == "process gone, no report; tab still open"   # format kept

    def test_tab_closed_still_goes_dead_once(self, relay, tab):
        _session(relay, "e1", _dead_pid())
        tab.alive = False
        assert relay._check_one("e1")["status"] == "dead"
        assert relay._check_one("e1")["status"] == "dead"
        assert [e["event"] for e in _ledger(relay)] == ["closed"]


# ── relay list is read-only ─────────────────────────────────────────────────────────────────────

def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class TestListIsReadOnly:
    def _world(self, relay, tab, monkeypatch):
        """Three executors whose fresh status differs from the stored `busy` — each one a write
        `relay check` would make (stalled, reported + report_seen, dead/closed)."""
        _session(relay, "e-stalled", _dead_pid())
        _session(relay, "e-reported", _dead_pid(), report=True)
        _session(relay, "e-dead", _dead_pid())
        dead_handle = relay.read_session("e-dead")["iterm_session"]
        tab.is_alive = lambda label, handle=None, pid=None: handle != dead_handle
        relay.append_ledger("spawned", session_id="e-stalled")   # a non-empty ledger to compare

    def test_list_json_leaves_ledger_and_session_files_byte_identical(self, relay, tab,
                                                                     monkeypatch, capsys):
        self._world(relay, tab, monkeypatch)
        before = _snapshot(relay.STATE_ROOT)
        run_main(relay, "list", "--all", "--json")
        payload = json.loads(capsys.readouterr().out)
        assert _snapshot(relay.STATE_ROOT) == before
        status = {r["session_id"]: r["status"] for r in payload["executors"]}
        # ...yet the statuses it shows are the fresh ones, not the stored `busy`.
        assert status == {"e-stalled": "stalled", "e-reported": "reported", "e-dead": "dead"}

    def test_list_table_writes_nothing_either(self, relay, tab, monkeypatch, capsys):
        self._world(relay, tab, monkeypatch)
        before = _snapshot(relay.STATE_ROOT)
        run_main(relay, "list", "--all")
        capsys.readouterr()
        assert _snapshot(relay.STATE_ROOT) == before

    def test_check_still_records_the_transitions(self, relay, tab, monkeypatch, capsys):
        """The contrast that proves the fixture exercises real writes: `check` persists."""
        self._world(relay, tab, monkeypatch)
        run_main(relay, "check", "--all", "--json")
        capsys.readouterr()
        events = [(e["event"], e["session_id"]) for e in _ledger(relay)]
        assert ("stalled", "e-stalled") in events
        assert ("reported", "e-reported") in events
        assert ("closed", "e-dead") in events
        assert relay.read_session("e-stalled")["status"] == "stalled"


# ── nudge-lead: the readiness gate ──────────────────────────────────────────────────────────────

class TestNudgeLeadReadinessGate:
    HANDLE = {"tmux": "tmux:%5", "iterm": "w0t0p0:LEAD"}

    def _lead(self, relay, backend):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="alpha",
                                      tab_label="[Lead] alpha", backend=backend,
                                      iterm_session=self.HANDLE[backend])

    @pytest.fixture(params=["tmux", "iterm"])
    def bk(self, request, relay):
        """The marker's recorded backend module with every keystroke/probe entry point stubbed."""
        mod = relay.backend.by_name(request.param)
        state = {"screen": "ready", "alive": True}
        with mock.patch.object(mod, "screen_state", lambda h: state["screen"]), \
             mock.patch.object(mod, "send", return_value=True) as send, \
             mock.patch.object(mod, "press_enter", return_value=True) as enter, \
             mock.patch.object(mod, "is_alive", lambda *a, **k: state["alive"]):
            self._lead(relay, request.param)
            yield SimpleNamespace(name=request.param, state=state, send=send, enter=enter)

    @pytest.mark.parametrize("screen", ["trust", "starting", "shell"])
    def test_not_ready_types_nothing(self, relay, bk, screen):
        bk.state["screen"] = screen
        with pytest.raises(SystemExit) as ei:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="wake up"))
        msg = str(ei.value)
        assert msg.startswith("not-ready:") and "relay typed nothing" in msg
        if screen == "trust":
            assert "folder-trust prompt" in msg
        bk.send.assert_not_called()
        bk.enter.assert_not_called()

    def test_unreadable_screen_on_a_live_tab_is_not_ready(self, relay, bk):
        bk.state["screen"] = None
        with pytest.raises(SystemExit) as ei:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        assert str(ei.value).startswith("not-ready:") and "can't be read" in str(ei.value)
        bk.send.assert_not_called()

    def test_unreadable_screen_and_dead_tab_is_no_live_tab(self, relay, bk):
        bk.state["screen"], bk.state["alive"] = None, False
        with pytest.raises(SystemExit) as ei:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        assert str(ei.value).startswith("no-live-tab:")
        bk.send.assert_not_called()

    def test_ready_including_the_busy_spinner_sends(self, relay, bk, capsys):
        bk.state["screen"] = "ready"   # trust_prompt.input_ready: input box OR "esc to interrupt"
        relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="wake up"))
        bk.send.assert_called_once_with("[Lead] alpha", "wake up", self.HANDLE[bk.name], None)
        assert "nudged:" in capsys.readouterr().out

    def test_spinner_screen_reads_ready(self):
        assert trust_prompt.launch_state("✻ Thinking… (12s · esc to interrupt)\n") == "ready"


class TestSendConfirmedEnterRetryGate:
    """The Enter retry inside _send_confirmed is a keystroke too: never into a pane that stopped
    showing the input box since the line was typed."""

    def _run(self, relay, tmp_path, monkeypatch, screen):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text('{"type":"assistant"}\n')
        old = time.time() - 600                      # not mid-turn
        os.utime(transcript, (old, old))
        monkeypatch.setattr(relay, "_find_transcript_path", lambda cs: transcript)
        monkeypatch.setattr(relay, "SUBMIT_CONFIRM_SECONDS", 0.05)
        monkeypatch.setattr(relay, "SUBMIT_RETRY_CONFIRM_SECONDS", 0.05)
        bk = SimpleNamespace(NAME="tmux", send=mock.Mock(return_value=True),
                             press_enter=mock.Mock(return_value=True),
                             screen_state=lambda h: screen)
        return bk, relay._send_confirmed(bk, "[Lead] a", "hello", "tmux:%5", None, "cs-1")

    def test_no_enter_into_a_dialog(self, relay, tmp_path, monkeypatch):
        bk, result = self._run(relay, tmp_path, monkeypatch, "starting")
        assert result == (True, False)
        bk.press_enter.assert_not_called()

    def test_enter_retry_when_ready(self, relay, tmp_path, monkeypatch):
        bk, result = self._run(relay, tmp_path, monkeypatch, "ready")
        assert result == (True, False)               # never seen in the transcript
        bk.press_enter.assert_called_once()
