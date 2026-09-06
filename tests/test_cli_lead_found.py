"""
Regression tests for gaps the LEAD found (not the bug-hunt packets) while reviewing bin/relay:

  a. `relay send --rotate`/`--upgrade` used to strand the retired session's `--when-idle` queue —
     the successor got a brand-new, empty queue.json and nothing ever delivered those packets again.
  b. A `queue_delivery_failed` ledger event used to be invisible short of reading the ledger by
     hand — `relay list`/`check` now print a red footnote naming it.
  d. An executor that hit its usage/session limit used to read as an ordinary stall (busy too
     long, no report) — `_check_one` now reports `paused` (with the reset time, if the transcript
     names one) instead, and the pause clears the moment a fresh assistant turn lands.

(c — successor naming increments -r2/-r3 rather than -r2-r2 — was already fixed on main and is
covered by tests/test_relay.py::TestSendRotate; not repeated here.)

No xfail markers: these are new coverage for new fixes, not bug-hunt repros with a filed finding.

SAFETY: every session here goes through the real `cmd_send`/`cmd_check`/`cmd_list`/`cmd_retire`
call paths, which can fall through to a REAL terminal backend (real AppleScript, a real spawned
`claude` process) the instant one entry point on `relay.iterm`/`terminal_app` is left unmocked —
this bit a draft of this very file (a real tab briefly opened before self-closing). So this file
copies tests/test_cli_lifecycle.py's `terms`/FakeTerm/`_pristine_backends` fixtures verbatim rather
than hand-picking individual `mock.patch.object` calls per test — every session sets "backend":
"iterm" explicitly, and every test that can reach a backend takes `terms`.

Run: pytest tests/test_cli_lead_found.py -q
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_relay_module(state_root):
    """Load bin/relay as a module (it has no .py extension since it's a real executable, so the
    loader must be given explicitly), with STATE_ROOT patched to an isolated tmp directory so
    tests never touch ~/.relay-tasks. Copied verbatim from tests/test_relay.py's own helper."""
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
    _orig_read_pid, _orig_read_iterm_id, _orig_read_iterm_id_at = (
        mod.read_pid, mod.read_iterm_id, mod.read_iterm_id_at)
    mod.read_pid = lambda session_id, timeout=0.5: _orig_read_pid(session_id, timeout)
    mod.read_iterm_id = lambda session_id, timeout=0.5: _orig_read_iterm_id(session_id, timeout)
    mod.read_iterm_id_at = lambda path, timeout=0.5: _orig_read_iterm_id_at(path, timeout)
    return mod


@pytest.fixture
def relay(tmp_path):
    return load_relay_module(tmp_path / ".relay-tasks")


# ── backend safety net — copied from tests/test_cli_lifecycle.py, see that file's FakeTerm
# docstring for why patching `relay.iterm` alone is not enough (term_backend(s) re-resolves the
# backend from the NAME recorded in session.json via backend.by_name(), straight past a patched
# module-level binding) ───────────────────────────────────────────────────────────────────────

BACKEND_ENTRY_POINTS = ("spawn", "send", "close", "focus", "is_alive", "rename_by_id",
                        "tty_by_id", "pid_on_tty", "title_by_id", "live_session_names")


@pytest.fixture(autouse=True)
def _pristine_backends():
    """Hard guarantee that no test leaves a stub on a REAL backend module (they're imported once
    per process and shared by every relay module a test loads)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import iterm as _iterm
    import terminal_app as _terminal_app
    snapshot = [(m, n, getattr(m, n)) for m in (_iterm, _terminal_app)
                for n in BACKEND_ENTRY_POINTS if hasattr(m, n)]
    try:
        yield
    finally:
        for mod, name, original in snapshot:
            setattr(mod, name, original)


@pytest.fixture(autouse=True)
def _no_desktop_notifications():
    """desktop_nudge() shells out to terminal-notifier/osascript — RELAY_NO_NOTIFY is its
    documented kill-switch."""
    saved = os.environ.get("RELAY_NO_NOTIFY")
    os.environ["RELAY_NO_NOTIFY"] = "1"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("RELAY_NO_NOTIFY", None)
        else:
            os.environ["RELAY_NO_NOTIFY"] = saved


class FakeTerm:
    """Recorder standing in for a terminal backend's entry points. Installed onto BOTH real
    backend modules by the `terms` fixture below."""

    def __init__(self):
        self.spawns, self.sends, self.closes = [], [], []
        self.send_ok = True
        self.close_ok = True
        self.alive = False   # default DEAD: every session below is retired/reported already, not
                              # a live tab a real command would need to find
        self.spawn_error = None

    def spawn(self, **kw):
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawns.append(kw)
        if kw.get("pidfile"):
            Path(kw["pidfile"]).parent.mkdir(parents=True, exist_ok=True)
            Path(kw["pidfile"]).write_text("4242")
        if kw.get("iterm_id_file"):
            Path(kw["iterm_id_file"]).parent.mkdir(parents=True, exist_ok=True)
            Path(kw["iterm_id_file"]).write_text("w0t0p0:STUB")
        # A tab this spawn() just opened IS alive — see test_cli_lifecycle.py's FakeTerm.spawn for
        # why (_ensure_tab_label's post-spawn is_alive check shares this same flag with the
        # pre-spawn "was the old tab still around" probes; without this a real ~4.5s retry-sleep
        # fires on every successful spawn since self.alive defaults False here).
        self.alive = True
        return {"ok": True, "session_id": "w0t0p0:STUB"}

    def send(self, label, prompt, handle=None, pid=None):
        self.sends.append({"label": label, "prompt": prompt, "handle": handle, "pid": pid})
        return self.send_ok

    def close(self, label, handle=None, pid=None):
        self.closes.append({"label": label, "handle": handle, "pid": pid})
        return self.close_ok

    def focus(self, label, handle=None, pid=None):
        return True

    def is_alive(self, label, handle=None, pid=None):
        return self.alive

    def rename_by_id(self, handle, new_name):
        return True

    def tty_by_id(self, handle):
        return None

    def pid_on_tty(self, tty, binary_suffix=None):
        return None

    def title_by_id(self, handle):
        return None

    def live_session_names(self):
        return set()


@pytest.fixture
def terms(relay):
    """Stub every terminal entry point on BOTH backend modules plus the module-level `iterm`
    binding, and neutralise the other real-world escapes a spawn/relaunch makes."""
    fake = FakeTerm()
    mods = []
    for m in (relay.iterm, relay.iterm_backend, relay.backend.by_name("iterm"),
              relay.backend.by_name("terminal")):
        if m is not None and m not in mods:
            mods.append(m)
    with mock.patch.object(relay, "auto_trust"), \
         mock.patch.object(relay, "_launch_background_label_assert"), \
         mock.patch.object(relay, "_kill_and_wait"):
        stack = []
        for m in mods:
            for name in BACKEND_ENTRY_POINTS:
                if hasattr(m, name):
                    p = mock.patch.object(m, name, getattr(fake, name))
                    p.start()
                    stack.append(p)
        try:
            yield fake
        finally:
            for p in reversed(stack):
                p.stop()


def _ledger_events(relay, event=None):
    if not relay.LEDGER.exists():
        return []
    out = []
    for line in relay.LEDGER.read_text().splitlines():
        d = json.loads(line)
        if event is None or d.get("event") == event:
            out.append(d)
    return out


def _session(relay, tmp_path, sid, **over):
    """A session.json in the shape cmd_spawn writes — "backend": "iterm" explicit, same convention
    tests/test_cli_lifecycle.py's make_session uses, so term_backend(s) always resolves to a
    module the `terms` fixture actually patched, never the ambient ("ohatever this invocation
    would pick") ANY_BACKEND ambiguity that let a call slip through unmocked."""
    s = {"session_id": sid, "owner_lead": None, "owner_project": None,
         "worktree": str(tmp_path), "topic": "t", "scope": "t", "tab_label": f"[Exec] {sid}",
         "model": "sonnet", "mcp": None, "keep": False, "pid": None, "pid_started": None,
         "iterm_session": "w0t0p0:STUB", "backend": "iterm", "claude_session": f"cs-{sid}",
         "status": "reported", "current_packet": 1, "busy_since": relay.now(),
         "busy_since_epoch": time.time(), "superseded_by": None,
         "created": relay.now(), "updated": relay.now()}
    s.update(over)
    relay.write_session(sid, s)
    relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
    (relay.packets_dir(sid) / "001-packet.md").write_text("first")
    if s["status"] == "reported":
        (relay.packets_dir(sid) / "001-report.md").write_text(
            "Done.\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\nChanged: x")
    return s


# ── (a) rotate/upgrade moves the --when-idle queue onto the successor ───────────────────────────

class TestRotateMovesQueue:
    def _rotate(self, relay, terms, sid, nxt):
        relay.cmd_send(SimpleNamespace(session_id=sid, packet=str(nxt), rotate=True))

    def test_rotate_moves_the_queue_to_the_successor_in_order(self, relay, terms, tmp_path):
        sid = "q1"
        _session(relay, tmp_path, sid, owner_lead="lead-1")
        relay.enqueue_packet(sid, "queued packet ONE body", "one.md")
        relay.enqueue_packet(sid, "queued packet TWO body", "two.md")
        nxt = tmp_path / "n.md"
        nxt.write_text("# next\n\n## Preconditions\n- ok\n\ndo more")
        self._rotate(relay, terms, sid, nxt)

        new_sid = f"{sid}-r2"
        assert relay.read_queue(sid) == []  # nothing left stranded on the retired session
        new_queue = relay.read_queue(new_sid)
        assert [Path(i["body_path"]).read_text() for i in new_queue] == [
            "queued packet ONE body", "queued packet TWO body"]
        # New ids are the SUCCESSOR's own (1, 2, ...), not a copy of the old ones.
        assert [i["id"] for i in new_queue] == [1, 2]

    def test_rotate_ledgers_queue_moved_per_entry(self, relay, terms, tmp_path):
        sid = "q1"
        _session(relay, tmp_path, sid, owner_lead="lead-1")
        relay.enqueue_packet(sid, "one", "one.md")
        relay.enqueue_packet(sid, "two", "two.md")
        nxt = tmp_path / "n.md"
        nxt.write_text("# next\n\n## Preconditions\n- ok\n\ndo more")
        self._rotate(relay, terms, sid, nxt)

        moved = _ledger_events(relay, "queue_moved")
        assert [m["queue_id"] for m in moved] == [1, 2]
        assert all(m["session_id"] == sid and m["successor"] == f"{sid}-r2" for m in moved)

    def test_an_empty_queue_moves_nothing_and_ledgers_nothing(self, relay, terms, tmp_path):
        """No queue at all must not print/ledger a spurious move — this is a real gap fix, not a
        new source of noise on the (overwhelmingly common) rotate-with-no-queue path."""
        sid = "q1"
        _session(relay, tmp_path, sid, owner_lead="lead-1")
        nxt = tmp_path / "n.md"
        nxt.write_text("# next\n\n## Preconditions\n- ok\n\ndo more")
        self._rotate(relay, terms, sid, nxt)
        assert _ledger_events(relay, "queue_moved") == []
        assert relay.read_queue(f"{sid}-r2") == []


# ── (b) a stuck queue's failure is visible in `list`/`check`, not only the ledger ────────────────

class TestQueueDeliveryFailedFootnote:
    def _stuck_queue(self, relay, sid, error="session is heavy"):
        """The exact on-disk shape `deliver_queued` leaves behind after a failed delivery attempt:
        the head item put BACK with `last_error` set (bin/relay's deliver_queued docstring)."""
        item = relay.enqueue_packet(sid, "body", "src.md")
        item["last_error"] = error
        relay.write_queue(sid, [item])

    def test_list_prints_the_red_footnote_naming_the_error(self, relay, terms, tmp_path, capsys):
        _session(relay, tmp_path, "e1")
        self._stuck_queue(relay, "e1", error="session is heavy — refusing")
        relay.cmd_list(SimpleNamespace(json=False, lead=None, all=True, closed=False))
        out = capsys.readouterr().out
        assert "e1: 1 queued packet(s) could not be delivered — session is heavy — refusing" in out

    def test_check_prints_the_red_footnote_too(self, relay, terms, tmp_path, capsys):
        _session(relay, tmp_path, "e1")
        self._stuck_queue(relay, "e1", error="tab unreachable")
        # `check` re-attempts delivery before printing (deliver_queued's whole point) — force that
        # retry to keep failing the same way a genuinely heavy session would, so the queue is
        # still stuck (last_error still set) by the time the footnote is printed. This must NOT
        # reach a real send/spawn either way — `terms` guarantees that regardless.
        with mock.patch.object(relay, "_is_heavy", return_value=True), \
             mock.patch.object(relay, "_usage_for_session",
                               return_value={"last_prompt": 999, "prompt": 999, "output": 10,
                                             "requests": 3}):
            relay.cmd_check(SimpleNamespace(session_id="e1", all=False, json=False))
        out = capsys.readouterr().out
        assert "e1: 1 queued packet(s) could not be delivered —" in out
        assert terms.sends == [] and terms.spawns == []  # never reached a real delivery attempt

    def test_a_queue_with_no_failure_prints_no_footnote(self, relay, terms, tmp_path, capsys):
        """An ordinary pending queue (awaiting idle, never yet failed) must not be painted red —
        only a HEAD item that has actually failed a delivery attempt is a problem worth a footnote."""
        _session(relay, tmp_path, "e1")
        relay.enqueue_packet("e1", "body", "src.md")  # no last_error — never attempted / clean
        relay.cmd_list(SimpleNamespace(json=False, lead=None, all=True, closed=False))
        assert "could not be delivered" not in capsys.readouterr().out

    def test_cancelling_the_stuck_item_clears_the_footnote(self, relay, terms, tmp_path, capsys):
        _session(relay, tmp_path, "e1")
        self._stuck_queue(relay, "e1")
        relay.cmd_queue(SimpleNamespace(session_id="e1", cancel="all"))
        capsys.readouterr()
        relay.cmd_list(SimpleNamespace(json=False, lead=None, all=True, closed=False))
        assert "could not be delivered" not in capsys.readouterr().out


# ── (d) a usage-limit hit reads as `paused`, not `stalled` ───────────────────────────────────────

def _write_transcript(relay, monkeypatch, tmp_path, claude_session, text):
    """A minimal, real-shaped Claude Code transcript: one assistant turn whose text content is
    `text` — enough for `_last_assistant_text` to find it. Same CLAUDE_CONFIG_DIR-env technique
    tests/test_relay.py's own transcript fixtures use."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    d = tmp_path / "cfg" / "projects" / "-p"
    d.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"type": "assistant", "message": {"id": "m1",
        "content": [{"type": "text", "text": text}]}})
    (d / f"{claude_session}.jsonl").write_text(line + "\n")


class TestUsageLimitPause:
    def _busy_exec(self, relay, tmp_path, sid="e1", claude_session="cs-e"):
        _session(relay, tmp_path, sid, claude_session=claude_session, status="busy",
                 busy_since_epoch=1.0)  # ~1970 — long enough ago to be past the stall threshold
                                        # (0.0 itself is falsy, so _busy_elapsed would ignore it
                                        # and fall back to `busy_since`, which is "now")

    def test_check_one_reports_paused_with_the_reset_time(self, relay, terms, monkeypatch, tmp_path):
        self._busy_exec(relay, tmp_path)
        _write_transcript(relay, monkeypatch, tmp_path, "cs-e",
                          "You've hit your session limit. Try again at 4:20pm.")
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            s = relay._check_one("e1")
        assert s["status"] == "paused"
        assert s["pause_text"] == "paused (limit, resets 4:20pm)"

    def test_usage_limit_case_insensitive_and_the_other_wording(self, relay, terms, monkeypatch,
                                                                 tmp_path):
        self._busy_exec(relay, tmp_path)
        _write_transcript(relay, monkeypatch, tmp_path, "cs-e", "USAGE LIMIT reached, sorry.")
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            s = relay._check_one("e1")
        assert s["status"] == "paused"

    def test_a_mid_sentence_mention_does_not_pause(self, relay, terms, monkeypatch, tmp_path):
        """Gap (d) fix (a): the match must anchor to the START of the last assistant text (after
        stripping whitespace) — an executor whose last message merely DISCUSSES usage limits
        mid-sentence (this repo's own executors do) must never read as paused."""
        self._busy_exec(relay, tmp_path)
        _write_transcript(relay, monkeypatch, tmp_path, "cs-e",
                          "By the way, if you ever hit a usage limit mid-task, just resume later.")
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            s = relay._check_one("e1")
        assert s["status"] != "paused"
        assert not s.get("pause_text")

    def test_usage_limit_pattern_is_overridable_via_config(self, relay, terms, monkeypatch,
                                                            tmp_path):
        """Gap (d) fix (b): `usage_limit_pattern` in lead/config.json overrides the built-in
        regex. Read straight off the config file rather than through `lead_guard.load_config`
        (which only merges keys already declared in LEAD_DEFAULTS) — see
        `relay._usage_limit_pattern`'s docstring for why."""
        self._busy_exec(relay, tmp_path)
        cfg_path = relay.lead_guard.config_path(relay.STATE_ROOT)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"usage_limit_pattern": r"^custom limit wording"}))
        _write_transcript(relay, monkeypatch, tmp_path, "cs-e",
                          "Custom limit wording, resets soon.")
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            s = relay._check_one("e1")
        assert s["status"] == "paused"

    def test_no_reset_time_present_still_pauses(self, relay, terms, monkeypatch, tmp_path):
        self._busy_exec(relay, tmp_path)
        _write_transcript(relay, monkeypatch, tmp_path, "cs-e", "You've hit your session limit.")
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            s = relay._check_one("e1")
        assert s["status"] == "paused" and s["pause_text"] == "paused (limit)"

    def test_a_transcript_whose_limit_message_sits_in_the_last_256kib_is_detected(
            self, relay, terms, monkeypatch, tmp_path):
        """`_last_assistant_text` reads only the tail (`max(0, size - 256*1024)` onward) — real
        transcripts here reach tens of MB. Padding the transcript with enough EARLIER assistant
        turns to push it well past 256 KiB, with the real usage-limit message as the LAST line,
        proves the tail-only read still finds it."""
        self._busy_exec(relay, tmp_path)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        d = tmp_path / "cfg" / "projects" / "-p"
        d.mkdir(parents=True, exist_ok=True)
        pad_line = json.dumps({"type": "assistant", "message": {"id": "pad",
            "content": [{"type": "text", "text": "Working on the packet. " * 40}]}})
        limit_line = json.dumps({"type": "assistant", "message": {"id": "m-last",
            "content": [{"type": "text",
                         "text": "You've hit your session limit. Try again at 4:20pm."}]}})
        transcript = d / "cs-e.jsonl"
        transcript.write_text("\n".join([pad_line] * 2000 + [limit_line]) + "\n")
        assert transcript.stat().st_size > 256 * 1024  # the point of the test: bigger than the tail
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            s = relay._check_one("e1")
        assert s["status"] == "paused"
        assert s["pause_text"] == "paused (limit, resets 4:20pm)"

    def test_an_ordinary_long_running_turn_still_stalls(self, relay, terms, monkeypatch, tmp_path):
        """The pause path must not swallow the EXISTING stall detection for a session that is
        simply slow, not rate-limited."""
        self._busy_exec(relay, tmp_path)
        _write_transcript(relay, monkeypatch, tmp_path, "cs-e", "Still working on it...")
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            s = relay._check_one("e1")
        assert s["status"] == "stalled"

    def test_pause_clears_on_the_next_assistant_message(self, relay, terms, monkeypatch, tmp_path):
        self._busy_exec(relay, tmp_path)
        _write_transcript(relay, monkeypatch, tmp_path, "cs-e",
                          "You've hit your session limit. Try again at 4:20pm.")
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            paused = relay._check_one("e1")
        assert paused["status"] == "paused"
        # The limit resets and the executor produces a fresh, ordinary turn — now the LAST
        # assistant message in the transcript, so the pause must clear.
        _write_transcript(relay, monkeypatch, tmp_path, "cs-e", "Back to work on the packet.")
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            resumed = relay._check_one("e1")
        assert resumed["status"] != "paused"
        assert not resumed.get("pause_text")

    def test_relay_list_shows_the_full_paused_status_untruncated(self, relay, terms, monkeypatch,
                                                                  tmp_path, capsys):
        self._busy_exec(relay, tmp_path)
        _write_transcript(relay, monkeypatch, tmp_path, "cs-e",
                          "You've hit your session limit. Try again at 4:20pm.")
        with mock.patch.object(relay, "session_pid_alive", return_value=True):
            relay.cmd_list(SimpleNamespace(json=False, lead=None, all=True, closed=False))
        out = capsys.readouterr().out
        assert "paused (limit, resets 4:20pm)" in out
