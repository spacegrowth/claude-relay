"""
Bug-hunt unit tests for relay's LEAD-side commands (packet bh-cli):
`lead-start` / `stop` / `close --self` / `route retain` / `auto`.
Succession and lead-tab addressing (`handoff`, `close-predecessor`, `nudge-lead`, `focus`,
`whoami`) live in tests/test_cli_lead_succession.py.

Every assertion is anchored to a documented contract — README.md, skills/*/SKILL.md,
docs/post-0.3.27-backlog.md, or the function's own docstring — named in each test's docstring.
Where the code contradicts the contract the test asserts the CONTRACT and carries an
`xfail(strict=True)` naming the finding in tests/bughunt/cli-findings.md.

No real terminal, no real `claude`, no network, no real home directory.

Run: pytest tests/test_cli_lead.py -q
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


def arm_lead(relay, sid, project="proj", **kw):
    relay.lead_guard.write_marker(relay.STATE_ROOT, sid, project=project, cwd="/tmp",
                                  tab_label=f"[Lead] {project}", **kw)
    return relay.lead_guard.read_marker(relay.STATE_ROOT, sid)


def cfg_write(relay, **kv):
    p = relay.lead_guard.config_path(relay.STATE_ROOT)
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(p.read_text()) if p.exists() else {}
    cur.update(kv)
    p.write_text(json.dumps(cur))


# ── lead-start ──────────────────────────────────────────────────────────────────────────────────

class TestLeadStart:
    """cmd_lead_start: "Mark THIS session as a lead (invoked by /relay:mode). The marker's
    existence is what every hook checks … Idempotent: re-running /relay:mode just refreshes the
    marker"."""

    def test_it_writes_an_armed_marker_with_project_cwd_and_label(self, relay, terms, capsys):
        run_main(relay, "lead-start", "lead-1", "--project", "webapp", "--model", "opus")
        assert relay.lead_guard.is_lead(relay.STATE_ROOT, "lead-1")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["project"] == "webapp" and m["model"] == "opus"
        assert m["tab_label"] == "[Lead] webapp"
        assert m["cwd"] == os.getcwd()
        assert m["backend"] == relay.iterm.NAME
        assert "lead mode active for session 'lead-1'" in capsys.readouterr().out

    def test_project_defaults_to_the_cwd_basename(self, relay, terms, capsys):
        """Row 92: the bare basename is only the last-resort fallback — `lead-start` says so, once,
        in its own output (`lead name: <name> (from cwd)`)."""
        run_main(relay, "lead-start", "lead-1")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["project"] == os.path.basename(os.getcwd())
        assert f"lead name: {os.path.basename(os.getcwd())} (from cwd)" in capsys.readouterr().out

    def test_a_context_derived_project_is_slugified(self, relay, terms, capsys):
        """Row 92: `/relay:mode` derives a natural-language name from context ("relay 0.5.0
        release") and passes it as `--project` — stored (and titled) as one clean slug, not the raw
        phrase, everywhere relay treats `project` as an identifier."""
        run_main(relay, "lead-start", "lead-1", "--project", "relay 0.5.0 release")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        expected = relay.slugify("relay 0.5.0 release")
        assert m["project"] == expected
        assert m["tab_label"] == f"[Lead] {expected}"
        assert f"lead name: {expected} (from --project)" in capsys.readouterr().out

    def test_an_empty_session_id_is_refused(self, relay, terms):
        """"lead-start: session id is empty (is $CLAUDE_CODE_SESSION_ID set?)"."""
        with pytest.raises(SystemExit) as e:
            run_main(relay, "lead-start", "")
        assert "session id is empty" in str(e.value)

    def test_a_rearm_preserves_started_and_predecessor(self, relay, terms):
        """cmd_lead_start: "Re-arm … must not clobber handoff/history state that only the FIRST arm
        ever sets: `predecessor` … and `started`"."""
        pred = {"session_id": "old", "tab_label": "[ex-Lead] webapp", "iterm_session": "w0:OLD"}
        arm_lead(relay, "lead-1", "webapp", started="2020-01-01T00:00:00", predecessor=pred)
        run_main(relay, "lead-start", "lead-1", "--project", "webapp")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["started"] == "2020-01-01T00:00:00"
        assert m["predecessor"] == pred

    def test_a_prearmed_successor_keeps_its_handoff_given_name(self, relay, terms, capsys):
        """Row 92 follow-up fix: a marker with BOTH `predecessor` and `project` already set (the
        shape `cmd_handoff` pre-arms) ignores `--project` on its aftercare `/relay:mode` re-arm —
        otherwise the skill's new "derive a fresh context name" instruction would silently rename
        the successor and drop its `·<id4>` disambiguation (row 75's bug shape)."""
        pred = {"session_id": "old", "tab_label": "[ex-Lead] weekly-release", "iterm_session": "w0:OLD"}
        arm_lead(relay, "lead-1", "weekly-release", predecessor=pred)
        relay.lead_guard.update_marker(relay.STATE_ROOT, "lead-1",
                                       tab_label="[Lead] weekly-release ·70e2")
        run_main(relay, "lead-start", "lead-1", "--project", "other")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["project"] == "weekly-release"
        assert m["tab_label"] == "[Lead] weekly-release ·70e2"
        err = capsys.readouterr().err
        assert "pre-armed successor keeps its handoff-given name 'weekly-release'" in err
        assert "--project 'other' ignored" in err

    def test_a_non_prearmed_marker_still_renames_on_project(self, relay, terms):
        """The matched negative: a marker with no `predecessor` at all (never went through a
        handoff) is renamed by `--project` exactly as before Fix 1."""
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "lead-start", "lead-1", "--project", "other")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["project"] == "other"
        assert m["tab_label"] == "[Lead] other"

    def test_arming_resets_the_autonomous_posture_from_config(self, relay, terms, capsys):
        """cmd_lead_start: "Autonomous posture … is stamped FRESH on every arm from config —
        deliberately NOT preserved … a re-arm puts the posture back to the account-wide default"."""
        arm_lead(relay, "lead-1", "webapp", autonomous=True, autonomous_source="command")
        run_main(relay, "lead-start", "lead-1", "--project", "webapp")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert relay.lead_guard.autonomous_state(m) == (False, "config")
        assert "AUTONOMOUS MODE ON" not in capsys.readouterr().out

    def test_arming_into_auto_announces_it(self, relay, terms, capsys):
        """"A lead that armed straight into auto must SAY so — an unannounced inverted posture is
        exactly the silent-autonomy failure §6f warns against"."""
        cfg_write(relay, autonomous_mode=True)
        run_main(relay, "lead-start", "lead-1", "--project", "webapp")
        out = capsys.readouterr().out
        assert "AUTONOMOUS MODE ON (config autonomous_mode: true)" in out
        assert "`relay auto off` reverts it" in out
        assert ledger_events(relay, "lead_started")[-1]["autonomous"] is True

    def test_a_colliding_project_name_is_auto_suffixed_and_announced(self, relay, terms, capsys):
        """unique_lead_project: "Resolve `project` to a name no other LIVE lead currently holds,
        auto-suffixing with the smallest free `-N`". `other`'s tab is alive (the `terms` fixture's
        default) — a truly live holder still creeps the suffix (row 92 only stopped a
        ghost/paused/tombstoned one from doing so). `lead-start` itself never adds the separate
        `·<id4>` tab-title disambiguator — that's `_distinct_lead_label`, used by handoff/resume."""
        arm_lead(relay, "other", "webapp")
        run_main(relay, "lead-start", "lead-1", "--project", "webapp")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["project"] == "webapp-2" and m["tab_label"] == "[Lead] webapp-2"
        assert "armed as 'webapp-2' instead" in capsys.readouterr().err

    def test_a_ghost_lead_does_not_reserve_its_name(self, relay, terms):
        """Row 92: only `_lead_liveness(m) == "live"` reserves a name now — a marker whose tab
        isn't actually alive never does, stale stamp or not."""
        arm_lead(relay, "ghost", "webapp")
        relay.lead_guard.update_marker(relay.STATE_ROOT, "ghost", last_active="2020-01-01T00:00:00")
        terms.alive = False  # its tab is genuinely gone, not just idle
        run_main(relay, "lead-start", "lead-1", "--project", "webapp")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")["project"] == "webapp"

    def test_no_rename_leaves_the_tab_title_alone(self, relay, terms):
        run_main(relay, "lead-start", "lead-1", "--project", "webapp", "--no-rename")
        assert terms.renames == []

    def test_arming_renames_the_lead_tab_by_its_own_handle(self, relay, terms, monkeypatch):
        """"Rename via /rename into the lead's OWN session, matched by its iTerm id"."""
        monkeypatch.setenv("TERM_SESSION_ID", "w9t9p9:LEAD")
        run_main(relay, "lead-start", "lead-1", "--project", "webapp")
        assert {"handle": "w9t9p9:LEAD", "name": "[Lead] webapp"} in terms.renames

    def test_lead_start_captures_and_records_tty(self, relay, terms, monkeypatch):
        """Row 91: lead-start captures the lead's own tty ONCE at arm time (from its own iTerm
        handle) and stores it on the marker, so notify_banner's tier 1 never needs a live
        AppleScript lookup for an ordinary banner. `_capture_tty`'s own AppleScript/os.ttyname
        fallback chain is covered directly in tests/test_lead_guard.py::TestCaptureTty — this only
        proves cmd_lead_start actually calls it (with the live handle) and records the result."""
        monkeypatch.setenv("TERM_SESSION_ID", "w9t9p9:LEAD")
        calls = []
        monkeypatch.setattr(relay.lead_guard, "_capture_tty",
                            lambda iterm_session: calls.append(iterm_session) or "/dev/ttys042")
        run_main(relay, "lead-start", "lead-1", "--project", "webapp")
        assert calls == ["w9t9p9:LEAD"]
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["tty"] == "/dev/ttys042"

    def test_lead_start_tty_is_none_when_capture_fails(self, relay, terms, monkeypatch):
        """A Terminal.app lead, or one where neither the AppleScript lookup nor this process's own
        controlling tty resolved — `tty` is a normal, fully-supported None, not an error."""
        monkeypatch.setattr(relay.lead_guard, "_capture_tty", lambda iterm_session: None)
        run_main(relay, "lead-start", "lead-1", "--project", "webapp")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m.get("tty") is None

    def test_lead_start_is_never_name_resolved(self, relay, terms):
        """main()'s RESOLVE_FIELDS comment: "`lead-start`'s session_id CREATES the lead namespace
        (always the raw $CLAUDE_CODE_SESSION_ID, nothing to resolve against yet)"."""
        arm_lead(relay, "aaaaaa-1111", "webapp")
        run_main(relay, "lead-start", "webapp")            # a project NAME, not a sid
        assert relay.lead_guard.is_lead(relay.STATE_ROOT, "webapp")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "aaaaaa-1111")["project"] == "webapp"


# ── stop / close --self ─────────────────────────────────────────────────────────────────────────

class TestStopAndCloseSelf:
    """cmd_stop: "Step down from lead mode for THIS session (unarm). A discoverable alias for
    `relay close --self`"."""

    def test_stop_clears_the_marker_and_ledgers(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "stop", "lead-1")
        assert not relay.lead_guard.is_lead(relay.STATE_ROOT, "lead-1")
        assert [e["session_id"] for e in ledger_events(relay, "lead_stepped_down")] == ["lead-1"]
        assert "gate and auto-wake are off" in capsys.readouterr().out

    def test_stopping_a_non_lead_is_a_no_op_that_says_so(self, relay, terms, capsys):
        run_main(relay, "stop", "not-a-lead")
        assert "is not in lead mode — nothing to stop" in capsys.readouterr().out
        assert ledger_events(relay, "lead_stepped_down") == []

    def test_close_self_is_the_same_step_down(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "close", "--self", "lead-1")
        assert not relay.lead_guard.is_lead(relay.STATE_ROOT, "lead-1")
        assert "stepped down from lead mode" in capsys.readouterr().out


# ── route retain ────────────────────────────────────────────────────────────────────────────────

class TestRoute:
    """route SKILL.md: "This opens a short grace window (default 120s) during which inline edits
    pass ungated, and records one durable `retained` event (with your reason) in the shared
    ledger"."""

    def test_retain_opens_the_configured_window_and_ledgers_the_reason(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "route", "retain", "finalising an executor's staged diff",
                 "--session", "lead-1")
        assert relay.lead_guard.in_grace(relay.STATE_ROOT, "lead-1")
        rec = ledger_events(relay, "retained")
        assert len(rec) == 1 and rec[0]["reason"] == "finalising an executor's staged diff"
        assert "retain window open for 120s" in capsys.readouterr().out

    def test_the_window_length_comes_from_config(self, relay, terms, capsys):
        """README Config: `grace_seconds` — "how long /relay:route retain opens the edit window"."""
        cfg_write(relay, grace_seconds=45)
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "route", "retain", "reason", "--session", "lead-1")
        assert "retain window open for 45s" in capsys.readouterr().out

    def test_the_window_expires(self, relay, terms):
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "route", "retain", "reason", "--session", "lead-1")
        assert not relay.lead_guard.in_grace(relay.STATE_ROOT, "lead-1",
                                             now_ts=time.time() + 121)

    def test_the_window_is_per_session(self, relay, terms):
        arm_lead(relay, "lead-1", "a")
        arm_lead(relay, "lead-2", "b")
        run_main(relay, "route", "retain", "reason", "--session", "lead-1")
        assert not relay.lead_guard.in_grace(relay.STATE_ROOT, "lead-2")

    def test_a_non_lead_session_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "route", "retain", "reason", "--session", "not-a-lead")
        assert "is not in lead mode" in str(e.value)
        assert ledger_events(relay, "retained") == []

    def test_an_empty_session_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "route", "retain", "reason", "--session", "")
        assert "session id is empty" in str(e.value)


# ── auto ────────────────────────────────────────────────────────────────────────────────────────

class TestAuto:
    """auto SKILL.md / cmd_auto: "State lives in the lead's own marker, so it is per-session and
    resets to the `autonomous_mode` config value on every fresh arm … `status` is read-only (no
    ledger event); `on`/`off` each log one durable ledger event"."""

    def test_on_flips_the_posture_and_ledgers_it(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "auto", "on", "--session", "lead-1")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert relay.lead_guard.autonomous_state(m) == (True, "command")
        rec = ledger_events(relay, "auto_mode_on")
        assert len(rec) == 1 and rec[0]["project"] == "webapp" and rec[0]["was"] is False
        assert "autonomous mode ON for lead 'webapp'" in capsys.readouterr().out

    def test_off_flips_it_back_and_ledgers_it(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp", autonomous=True, autonomous_source="command")
        run_main(relay, "auto", "off", "--session", "lead-1")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert relay.lead_guard.autonomous_state(m) == (False, "command")
        assert len(ledger_events(relay, "auto_mode_off")) == 1
        assert "back to announce-and-wait" in capsys.readouterr().out

    def test_status_is_read_only(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "auto", "status", "--session", "lead-1")
        assert ledger_events(relay, "auto_mode_on") == []
        assert ledger_events(relay, "auto_mode_off") == []
        assert "autonomous mode is OFF for lead 'webapp'" in capsys.readouterr().out

    def test_status_names_the_origin_of_the_posture(self, relay, terms, capsys):
        """auto SKILL.md: "`status` → report the posture *and its origin* (set by command this
        session, vs. inherited from the `autonomous_mode` config default)"."""
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "auto", "status", "--session", "lead-1")
        assert "from config autonomous_mode: false" in capsys.readouterr().out
        run_main(relay, "auto", "on", "--session", "lead-1")
        capsys.readouterr()
        run_main(relay, "auto", "status", "--session", "lead-1")
        assert "set by `relay auto` this session" in capsys.readouterr().out

    def test_turning_it_on_states_the_separate_commit_gate(self, relay, terms, capsys):
        """auto SKILL.md: "**Committing executor work is gated separately** (#16 phase 2) — turning
        auto on does *not* by itself license a commit … all five conditions"."""
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "auto", "on", "--session", "lead-1")
        out = capsys.readouterr().out
        assert "COMMITTING an executor's work now has its own gate" in out
        assert "--for-autocommit" in out and "--in-plan" in out and "--diff-reviewed" in out

    def test_the_posture_is_per_session(self, relay, terms):
        arm_lead(relay, "lead-1", "a")
        arm_lead(relay, "lead-2", "b")
        run_main(relay, "auto", "on", "--session", "lead-1")
        m2 = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-2")
        assert relay.lead_guard.autonomous_state(m2)[0] is False

    def test_a_non_lead_session_is_refused(self, relay, terms):
        """"This only works in a lead session (`/relay:mode` first); it exits with an error
        otherwise"."""
        for action in ("on", "off", "status"):
            with pytest.raises(SystemExit) as e:
                run_main(relay, "auto", action, "--session", "not-a-lead")
            assert "is not in lead mode" in str(e.value)

    def test_an_empty_session_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "auto", "status", "--session", "")
        assert "session id is empty" in str(e.value)
