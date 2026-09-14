"""
Bug-hunt unit tests for `relay send --when-idle`, its delivery path (`deliver_queued` /
`_deliver-queued`), and the `relay queue` command (packet bh-cli). Split out of
tests/test_cli_spawn_send.py to keep each file under the packet's ~800-line bound.

Every assertion is anchored to a documented contract — README.md, skills/send/SKILL.md,
docs/post-0.3.27-backlog.md (#18), or the function's own docstring — named in each test's
docstring.

Run: pytest tests/test_cli_queue.py -q
"""
import contextlib
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


def load_relay_module(state_root):
    """Load bin/relay as a module (no .py extension, so the loader is given explicitly) with
    STATE_ROOT patched to an isolated tmp dir — the same helper tests/test_relay.py uses, copied
    per the packet rather than imported."""
    path = str(REPO_ROOT / "bin" / "relay")
    loader = importlib.machinery.SourceFileLoader("relay_cli", path)
    spec = importlib.util.spec_from_file_location("relay_cli", path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["relay_cli"] = mod
    loader.exec_module(mod)
    mod.STATE_ROOT = state_root
    mod.LEDGER = state_root / "sessions.jsonl"
    # Never probe the REAL claude CLI for a model alias (lead_guard "model alias resolution").
    mod._probe_model = lambda alias: (None, "disabled in tests")
    mod._cli_version = lambda: "test"
    # read_pid/read_iterm_id/read_iterm_id_at poll a file for up to 5s by default — test-side only,
    # shrink the DEFAULT to 0.5s (an explicit timeout from any caller is untouched); see
    # tests/test_relay.py::load_relay_module for the full rationale.
    _orig_read_pid, _orig_read_iterm_id, _orig_read_iterm_id_at = (
        mod.read_pid, mod.read_iterm_id, mod.read_iterm_id_at)
    mod.read_pid = lambda session_id, timeout=0.5: _orig_read_pid(session_id, timeout)
    mod.read_iterm_id = lambda session_id, timeout=0.5: _orig_read_iterm_id(session_id, timeout)
    mod.read_iterm_id_at = lambda path, timeout=0.5: _orig_read_iterm_id_at(path, timeout)
    return mod


BACKEND_ENTRY_POINTS = ("spawn", "send", "close", "focus", "is_alive", "rename_by_id",
                        "tty_by_id", "pid_on_tty", "title_by_id", "live_session_names")


@pytest.fixture(autouse=True)
def _pristine_backends():
    """Hard guarantee that no test leaves a stub on a REAL backend module.

    The backend modules are imported once per process and shared by every relay module a test
    loads, so a stub that outlives its test silently rewrites the world for every later test file
    (observed: `tests/test_relay.py::TestResumeLead` reading a leaked `is_alive` as "the lead tab
    is alive"). This fixture is autouse and takes no other fixture, so pytest sets it up before
    anything else and tears it down last — after `terms`, and after any `monkeypatch` a test used
    on a backend module, whichever order those two happen to unwind in.
    """
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
    """desktop_nudge() shells out to osascript. RELAY_NO_NOTIFY is its own
    documented kill-switch — set it so no test can put a banner on the human's screen. Env is
    handled by hand rather than via `monkeypatch`, so that requesting `monkeypatch` here does not
    force it to be the FIRST fixture set up (and so the LAST torn down) in every test — which is
    what let a test's own `monkeypatch.setattr(<backend module>, …)` outlive `terms`."""
    saved = {k: os.environ.get(k) for k in ("RELAY_NO_NOTIFY", "CLAUDE_CODE_SESSION_ID")}
    os.environ["RELAY_NO_NOTIFY"] = "1"
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def relay(tmp_path):
    return load_relay_module(tmp_path / ".relay-tasks")


class FakeTerm:
    """Recorder standing in for a terminal backend's entry points.

    Installed onto BOTH real backend modules (see the `terms` fixture): patching `relay.iterm`
    alone is not enough, because `term_backend(s)` re-resolves the backend from the NAME recorded
    in session.json via `backend.by_name()` and would reach the REAL module straight past it.
    """

    def __init__(self):
        self.spawns, self.sends, self.closes, self.focuses, self.renames = [], [], [], [], []
        self.ops = []          # (operation, backend name) — which MODULE each call went through
        self.send_ok = True
        self.close_ok = True
        self.focus_ok = True
        self.alive = True
        self.alive_by = {}     # backend name -> is_alive override, for cross-backend tests
        self.spawn_error = None
        self.titles = {}
        self._name = None      # which backend module the CURRENT call came through

    @contextlib.contextmanager
    def _as(self, name):
        prev, self._name = self._name, name
        try:
            yield
        finally:
            self._name = prev

    def for_backend(self, name):
        """The entry points as installed on ONE backend module. Every call lands on this same
        recorder (so a test's later `terms.send_ok = False` is always honoured) but is tagged with
        that module's name in `ops` — which lets a test assert WHICH backend an operation went
        through without patching the modules itself (patching them from a test leaks; see
        `_pristine_backends`)."""
        def bind(method):
            def call(*a, **kw):
                with self._as(name):
                    return method(*a, **kw)
            return call
        return SimpleNamespace(**{n: bind(getattr(self, n)) for n in BACKEND_ENTRY_POINTS})

    # -- spawn: writes the two capture files a real bootstrap shell writes, so read_pid /
    #    read_iterm_id return at once instead of burning their 5s poll.
    def spawn(self, **kw):
        self.ops.append(("spawn", self._name))
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawns.append(kw)
        if kw.get("pidfile"):
            Path(kw["pidfile"]).parent.mkdir(parents=True, exist_ok=True)
            Path(kw["pidfile"]).write_text("4242")
        if kw.get("iterm_id_file"):
            Path(kw["iterm_id_file"]).parent.mkdir(parents=True, exist_ok=True)
            Path(kw["iterm_id_file"]).write_text("w0t0p0:STUB")
        return {"ok": True, "session_id": "w0t0p0:STUB"}

    def send(self, label, prompt, handle=None, pid=None):
        self.ops.append(("send", self._name))
        self.sends.append({"label": label, "prompt": prompt, "handle": handle, "pid": pid})
        return self.send_ok

    def close(self, label, handle=None, pid=None):
        self.ops.append(("close", self._name))
        self.closes.append({"label": label, "handle": handle, "pid": pid})
        return self.close_ok

    def focus(self, label, handle=None, pid=None):
        self.ops.append(("focus", self._name))
        self.focuses.append({"label": label, "handle": handle, "pid": pid})
        return self.focus_ok

    def is_alive(self, label, handle=None, pid=None):
        return self.alive_by.get(self._name, self.alive)

    def rename_by_id(self, handle, new_name):
        self.renames.append({"handle": handle, "name": new_name})
        return True

    def tty_by_id(self, handle):
        return None

    def pid_on_tty(self, tty, binary_suffix=None):
        return None

    def title_by_id(self, handle):
        return self.titles.get(handle)

    def live_session_names(self):
        return set(self.titles.values())


@pytest.fixture
def terms(relay):
    """Stub every terminal entry point on BOTH backend modules plus the module-level `iterm`
    binding, and neutralise the two other real-world escapes a spawn/relaunch makes: writing the
    human's ~/.claude.json (auto_trust) and Popen-ing a detached `_ensure-label` subprocess."""
    fake = FakeTerm()
    mods = []
    for m in (relay.iterm, relay.iterm_backend, relay.backend.by_name("iterm"),
              relay.backend.by_name("terminal")):
        if m is not None and m not in mods:
            mods.append(m)
    with mock.patch.object(relay, "auto_trust"), \
         mock.patch.object(relay, "_launch_background_label_assert"):
        stack = []
        for m in mods:
            bound = fake.for_backend(getattr(m, "NAME", None))
            for name in BACKEND_ENTRY_POINTS:
                if hasattr(m, name):
                    p = mock.patch.object(m, name, getattr(bound, name))
                    p.start()
                    stack.append(p)
        try:
            yield fake
        finally:
            for p in reversed(stack):
                p.stop()


@pytest.fixture
def live_pid():
    """A real, alive pid that is NOT this test process.

    Sessions under test are killed for real by the close/retire teardown (`_kill_and_wait`
    SIGTERMs `s["pid"]`), so using `os.getpid()` to make a session "look busy" would have the
    suite kill itself. A throwaway `sleep` child is both genuinely alive and safe to signal."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield proc.pid
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def write_packet(tmp_path, name="p.md", body=None):
    """A packet file that passes the advisory lint cleanly (GOAL first line + Preconditions), so a
    test asserting on stdout isn't reading nag lines it didn't ask for."""
    body = body if body is not None else (
        "GOAL — do the bounded thing described here.\n\n"
        "## Work\nEdit the module and add the missing branch, then run the suite.\n\n"
        "## Preconditions\n- the checkout is pulled\n")
    p = tmp_path / name
    p.write_text(body)
    return str(p)


DEFAULT_REPORT = ("Did the bounded thing; suite green, staged.\n"
                  "Status: clean\n"
                  "Risk flags: none\n"
                  "UNVERIFIED: none\n"
                  "Changed: one module\n")


def make_session(relay, sid, report=None, **over):
    """A session.json in the shape cmd_spawn writes, so lifecycle commands read a realistic record.

    A `reported` session also gets its report file on disk: `_check_one` recomputes liveness from
    the WORLD (report file / pid / tab), not from the stored status, so a "reported" record with no
    report would immediately be re-derived as stalled or dead. Pass report="..." for custom text,
    or report=False to deliberately create that inconsistent state."""
    s = {"session_id": sid, "owner_lead": None, "owner_project": None,
         "worktree": str(relay.STATE_ROOT.parent), "topic": sid, "scope": sid,
         "tab_label": f"[Exec] {sid}", "model": "claude-sonnet-5", "mcp": "none", "keep": False,
         "context": "200k", "agent": None, "effort": None, "pid": None, "pid_started": None,
         "iterm_session": "w0t0p0:STUB", "backend": "iterm", "claude_session": f"cs-{sid}",
         "status": "reported", "current_packet": 1, "busy_since": relay.now(),
         "busy_since_epoch": time.time(), "superseded_by": None,
         "created": relay.now(), "updated": relay.now()}
    s.update(over)
    relay.write_session(sid, s)
    relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
    n = int(s["current_packet"])
    for i in range(1, n + 1):   # the packets a real session would have on disk (next_packet_number)
        (relay.packets_dir(sid) / f"{i:03d}-packet.md").write_text(f"GOAL — packet {i}.\n")
    if report is None:
        report = DEFAULT_REPORT if s["status"] == "reported" else False
    if report:
        (relay.packets_dir(sid) / f"{int(s['current_packet']):03d}-report.md").write_text(report)
    return s


def mark_reported(relay, sid, text=None):
    """Move a session to a REAL `reported` state: the report file for its current packet plus the
    status flip. `_check_one` re-derives status from the world, so flipping the field alone would
    read back as `stalled`."""
    s = relay.read_session(sid)
    n = int(s["current_packet"])
    (relay.packets_dir(sid) / f"{n:03d}-report.md").write_text(text or DEFAULT_REPORT)
    s["status"], s["pid"] = "reported", None
    relay.write_session(sid, s)
    return s


def ledger_events(relay, event=None):
    if not relay.LEDGER.exists():
        return []
    out = []
    for line in relay.LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if event is None or d.get("event") == event:
            out.append(d)
    return out


def run_main(relay, *argv):
    """Drive the real argparse tree. `main()` reads sys.argv (it takes no parameters), so the CLI
    surface — including main()'s universal resolve_sid pass over sid-bearing fields — can only be
    exercised through it."""
    with mock.patch.object(sys, "argv", ["relay", *argv]):
        relay.main()


# ── the --when-idle queue (#18) ─────────────────────────────────────────────────────────────────

class TestWhenIdleQueue:
    """send SKILL.md: "The packet is persisted and delivered automatically the moment that session
    next goes idle … Queued packets deliver oldest-first, **one per idle transition** … It does not
    soften the other refusals — `superseded` and `launch-failed` still refuse"."""

    def _busy(self, relay, live_pid, sid="e1"):
        return make_session(relay, sid, status="busy", pid=live_pid, pid_started=None)

    def test_busy_target_queues_instead_of_refusing(self, relay, terms, tmp_path, capsys, live_pid):
        self._busy(relay, live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        items = relay.read_queue("e1")
        assert len(items) == 1 and items[0]["id"] == 1
        assert "queued as #1, delivers when it next goes idle" in capsys.readouterr().out

    def test_the_body_is_copied_to_disk_at_queue_time(self, relay, terms, tmp_path, live_pid):
        """The queue block's own comment: "The BODY is copied to disk at queue time (the source .md
        may be edited or deleted before delivery)"."""
        self._busy(relay, live_pid)
        pkt = tmp_path / "p.md"
        pkt.write_text("GOAL — original text.\n\n## Preconditions\n- ok\n")
        run_main(relay, "send", "e1", str(pkt), "--when-idle")
        pkt.unlink()
        stored = Path(relay.read_queue("e1")[0]["body_path"])
        assert "original text" in stored.read_text()

    def test_an_already_idle_session_sends_immediately(self, relay, terms, tmp_path):
        """"--when-idle on a session that is *already* idle just sends immediately"."""
        make_session(relay, "e1", status="reported")
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        assert relay.read_queue("e1") == []
        assert len(terms.sends) == 1

    def test_when_idle_does_not_soften_superseded(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="superseded", superseded_by="e2")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        assert "superseded" in str(e.value)
        assert relay.read_queue("e1") == []

    def test_when_idle_does_not_soften_launch_failed(self, relay, terms, tmp_path):
        make_session(relay, "e1", status=relay.LAUNCH_FAILED)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        assert relay.LAUNCH_FAILED in str(e.value)
        assert relay.read_queue("e1") == []

    def test_queued_delivery_cannot_widen_mcp_and_says_why(self, relay, terms, tmp_path, live_pid):
        """send SKILL.md: "Not possible via `--when-idle` (queued delivery types into the live
        process) — send it plainly once idle"."""
        self._busy(relay, live_pid)
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nMCP: inherit\n\n## Preconditions\n- ok\n")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", pkt, "--when-idle")
        assert "cannot widen its MCP set" in str(e.value)
        assert relay.read_queue("e1") == []

    def test_delivery_is_fifo_and_exactly_one_per_idle_transition(self, relay, terms, tmp_path, live_pid):
        self._busy(relay, live_pid)
        for n in (1, 2, 3):
            run_main(relay, "send", "e1", write_packet(tmp_path, f"p{n}.md",
                     f"GOAL — packet number {n}.\n\n## Preconditions\n- ok\n"), "--when-idle")
        mark_reported(relay, "e1")
        item = relay.deliver_queued("e1", trigger="test")
        assert item["id"] == 1
        assert [i["id"] for i in relay.read_queue("e1")] == [2, 3]
        assert len(terms.sends) == 1                     # only ONE injected this transition

    def test_a_busy_session_delivers_nothing(self, relay, terms, tmp_path, live_pid):
        self._busy(relay, live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        assert relay.deliver_queued("e1", trigger="test") is None
        assert len(relay.read_queue("e1")) == 1

    def test_a_refused_delivery_puts_the_item_back_at_the_head(self, relay, terms, tmp_path, live_pid):
        """deliver_queued: "a queue that can't deliver … puts its item BACK at the head with the
        error recorded, so nothing is silently swallowed"."""
        self._busy(relay, live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        relay.write_session("e1", {**relay.read_session("e1"), "status": "superseded",
                                   "superseded_by": "e2", "pid": None})
        assert relay.deliver_queued("e1", trigger="test") is None
        items = relay.read_queue("e1")
        assert len(items) == 1 and "superseded" in items[0]["last_error"]
        assert len(ledger_events(relay, "queue_delivery_failed")) == 1

    def test_a_repeated_identical_failure_is_ledgered_only_once(self, relay, terms, tmp_path, live_pid):
        """"Ledger the failure once per distinct error, not once per poll"."""
        self._busy(relay, live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        relay.write_session("e1", {**relay.read_session("e1"), "status": "superseded",
                                   "superseded_by": "e2", "pid": None})
        for _ in range(3):
            relay.deliver_queued("e1", trigger="test")
        assert len(ledger_events(relay, "queue_delivery_failed")) == 1

    def test_a_held_lock_blocks_a_second_concurrent_deliverer(self, relay, terms, tmp_path, live_pid):
        """_queue_lock: "the two delivery triggers … can fire within the same second on the same
        idle transition, and delivering the same packet twice would inject mid-turn"."""
        self._busy(relay, live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        mark_reported(relay, "e1")
        held = relay._queue_lock("e1")
        assert held is not None
        assert relay.deliver_queued("e1", trigger="second") is None
        assert len(relay.read_queue("e1")) == 1
        held.unlink()
        assert relay.deliver_queued("e1", trigger="first") is not None

    def test_a_stale_lock_is_broken_and_retaken(self, relay, terms, tmp_path):
        """"A lock older than `stale_seconds` is broken and re-taken, so a killed deliverer can't
        wedge the queue forever"."""
        make_session(relay, "e1")
        lock = relay.session_dir("e1") / "queue.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("999999")
        os.utime(lock, (time.time() - 10_000, time.time() - 10_000))
        assert relay._queue_lock("e1", stale_seconds=120) is not None

    def test_delivery_ledgers_queue_delivered_with_the_packet_number(self, relay, terms, tmp_path, live_pid):
        self._busy(relay, live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        mark_reported(relay, "e1")
        relay.deliver_queued("e1", trigger="stop-hook")
        rec = ledger_events(relay, "queue_delivered")
        assert len(rec) == 1
        assert rec[0]["packet"] == 2 and rec[0]["trigger"] == "stop-hook" and rec[0]["remaining"] == 0

    def test_delivery_applies_the_gates_footer_at_delivery_time(self, relay, terms, tmp_path, live_pid):
        """The queue block's comment: "the GATES/REPORT FORMAT footer is applied at DELIVERY time by
        the normal send path, so a queued packet is never stamped with a stale footer"."""
        self._busy(relay, live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        assert "(relay — do not remove" not in Path(relay.read_queue("e1")[0]["body_path"]).read_text()
        mark_reported(relay, "e1")
        relay.deliver_queued("e1", trigger="test")
        assert "(relay — do not remove" in (relay.packets_dir("e1") / "002-packet.md").read_text()

    def test_deliver_queued_subcommand_never_raises(self, relay, terms, tmp_path, capsys):
        """cmd_deliver_queued: "Fail-open like everything else that hook calls — a queue problem
        must never disturb the executor's own Stop behavior"."""
        run_main(relay, "_deliver-queued", "no-such-session")   # must not raise
        with mock.patch.object(relay, "deliver_queued", side_effect=RuntimeError("boom")):
            run_main(relay, "_deliver-queued", "no-such-session")
        assert "deliver-queued: boom" in capsys.readouterr().err


class TestQueueCommand:
    def test_show_lists_ids_summaries_and_body_paths(self, relay, terms, tmp_path, capsys, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path, "p.md",
                 "GOAL — rewire the chart legend.\n\n## Preconditions\n- ok\n"), "--when-idle")
        run_main(relay, "queue", "e1")
        out = capsys.readouterr().out
        assert "1 queued packet(s), delivered oldest-first" in out
        assert "#1" in out and "rewire the chart legend" in out
        assert "001-queued.md" in out

    def test_empty_queue_says_so(self, relay, terms, capsys):
        make_session(relay, "e1")
        run_main(relay, "queue", "e1")
        assert "nothing queued" in capsys.readouterr().out

    def test_cancel_by_id(self, relay, terms, tmp_path, capsys, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid)
        for n in (1, 2):
            run_main(relay, "send", "e1", write_packet(tmp_path, f"p{n}.md",
                     f"GOAL — job {n}.\n\n## Preconditions\n- ok\n"), "--when-idle")
        run_main(relay, "queue", "e1", "--cancel", "1")
        assert [i["id"] for i in relay.read_queue("e1")] == [2]
        assert [e["queue_id"] for e in ledger_events(relay, "queue_cancelled")] == [1]
        assert "cancelled 1 queued packet(s)" in capsys.readouterr().out

    def test_cancel_all(self, relay, terms, tmp_path, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid)
        for n in (1, 2):
            run_main(relay, "send", "e1", write_packet(tmp_path, f"p{n}.md",
                     f"GOAL — job {n}.\n\n## Preconditions\n- ok\n"), "--when-idle")
        run_main(relay, "queue", "e1", "--cancel")
        assert relay.read_queue("e1") == []
        assert not relay.queue_path("e1").exists()

    def test_cancelling_an_already_delivered_packet_finds_nothing_to_cancel(self, relay, terms,
                                                                            tmp_path, capsys, live_pid):
        """deliver_queued takes the item "off the queue BEFORE the attempt", so once delivered it
        is no longer cancellable — `relay queue --cancel` must say so, not silently pretend."""
        make_session(relay, "e1", status="busy", pid=live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        mark_reported(relay, "e1")
        relay.deliver_queued("e1", trigger="test")
        capsys.readouterr()
        run_main(relay, "queue", "e1", "--cancel", "1")
        assert "nothing queued — nothing to cancel" in capsys.readouterr().out
        assert ledger_events(relay, "queue_cancelled") == []

    def test_cancelling_an_unknown_id_lists_what_is_queued(self, relay, terms, tmp_path, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "queue", "e1", "--cancel", "7")
        assert "no queued packet #7" in str(e.value) and "#1" in str(e.value)

    def test_cancel_with_a_non_numeric_id_is_refused(self, relay, terms, tmp_path, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--when-idle")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "queue", "e1", "--cancel", "oldest")
        assert "takes a queue id or 'all'" in str(e.value)

    def test_queue_on_an_unknown_session_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "queue", "nope")
        assert "no such session: nope" in str(e.value)

    def test_a_corrupt_queue_file_reads_as_empty(self, relay, terms, capsys):
        """read_queue: "A missing/corrupt file reads as empty — a hand-edited queue.json must never
        take down `relay list`/`check`"."""
        make_session(relay, "e1")
        relay.queue_path("e1").write_text("{ not json")
        run_main(relay, "queue", "e1")
        assert "nothing queued" in capsys.readouterr().out
