"""
Bug-hunt unit tests for relay's executor LIFECYCLE commands (packet bh-cli):
`restart` / `resume` (executor and crashed lead) / `adopt` / `keep` / the auto-close sweep.
`close` / `retire` / `prune` live in tests/test_cli_close_retire_prune.py.

Every assertion is anchored to a documented contract — README.md, skills/*/SKILL.md,
docs/post-0.3.27-backlog.md, or the function's own docstring — named in each test's docstring.
Where the code contradicts the contract the test asserts the CONTRACT and carries an
`xfail(strict=True)` naming the finding in tests/bughunt/cli-findings.md.

No real terminal, no real `claude`, no network, no real home directory: see FakeTerm's docstring
for why patching `relay.iterm` alone would not be enough.

Run: pytest tests/test_cli_lifecycle.py -q
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
    """desktop_nudge() shells out to terminal-notifier/osascript. RELAY_NO_NOTIFY is its own
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
        # A tab this spawn() just opened IS alive — decouples "was the PRE-relaunch session's old
        # tab still around" (what resume/restart's live-copy guard probes, and what a test sets up
        # before calling) from "_ensure_tab_label's post-spawn is_alive check on the NEW tab", which
        # shares this same flag. Without this, a test that sets `terms.alive = False` to model a
        # genuinely-dead session (BUG-cli-4) makes the retry-3-times-with-a-real-sleep label
        # verification inside _ensure_tab_label spuriously fire on every successful relaunch.
        self.alive = True
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


def git_repo(path):
    """A throwaway repo, so the git-dependent paths (record_landed_if_clean, the sweep's
    dirty-path probe, prune's checks) run against real `git status` output."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def arm_lead(relay, sid, project="proj", **kw):
    relay.lead_guard.write_marker(relay.STATE_ROOT, sid, project=project, cwd="/tmp",
                                  tab_label=f"[Lead] {project}", **kw)
    return relay.lead_guard.read_marker(relay.STATE_ROOT, sid)


def age_file(path, seconds):
    os.utime(path, (time.time() - seconds, time.time() - seconds))


# ── restart ─────────────────────────────────────────────────────────────────────────────────────

class TestRestart:
    """restart SKILL.md: "opens a fresh iTerm tab and re-runs the session's **current packet** as a
    brand-new `claude` conversation. It does NOT carry over the prior conversation … If the session
    still looks alive, relay refuses unless you pass `--force`"."""

    @pytest.fixture(autouse=True)
    def _tab_is_gone(self, terms):
        """BUG-cli-4's fix consults the tab whenever there's no pid (every "dead"/pid-None fixture
        below), mirroring the lead-side guard's own default (see TestResumeLead._lead_tab_is_gone).
        Tests that want the tab to still be alive flip this back explicitly."""
        terms.alive = False

    def test_restart_mints_a_fresh_conversation_id(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="dead", claude_session="cs-old")
        run_main(relay, "restart", "e1")
        assert terms.spawns[0]["resume_id"] is None
        new = terms.spawns[0]["session_uuid"]
        assert new and new != "cs-old"
        assert relay.read_session("e1")["claude_session"] == new

    def test_restart_re_runs_the_current_packet(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="dead", current_packet=3)
        (relay.packets_dir("e1") / "003-packet.md").write_text("GOAL — the third job.\n")
        run_main(relay, "restart", "e1")
        assert "003-packet.md" in terms.spawns[0]["prompt"]
        assert "the third job" in terms.spawns[0]["prompt"]
        assert relay.read_session("e1")["current_packet"] == 3

    def test_restart_ledgers_the_packet_number(self, relay, terms):
        make_session(relay, "e1", status="dead", current_packet=2)
        run_main(relay, "restart", "e1")
        assert [e["packet"] for e in ledger_events(relay, "restarted")] == [2]

    def test_restart_refuses_a_live_process_without_force(self, relay, terms, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "restart", "e1")
        assert "still looks alive" in str(e.value) and "--force" in str(e.value)
        assert terms.spawns == []

    def test_force_overrides_the_liveness_refusal(self, relay, terms, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid)
        run_main(relay, "restart", "e1", "--force")
        assert len(terms.spawns) == 1

    def test_restart_without_its_packet_file_is_refused(self, relay, terms):
        make_session(relay, "e1", status="dead", current_packet=1)
        (relay.packets_dir("e1") / "001-packet.md").unlink()
        with pytest.raises(SystemExit) as e:
            run_main(relay, "restart", "e1")
        assert "packet 001 not found" in str(e.value)
        assert terms.spawns == []

    def test_restart_of_an_unknown_session_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "restart", "nope")
        assert "no such session: nope" in str(e.value)

    def test_restart_re_passes_mcp_effort_and_the_agent_role(self, relay, terms):
        """_relaunch: "MCP loading is per-process, so resume and restart both need the flags
        again"; "The role rides the inline agent and is NOT restored by --resume → re-pass it"."""
        make_session(relay, "e1", status="dead", mcp="inherit", effort="high",
                     agent=relay.lead_guard.EXECUTOR_AGENT_NAME)
        run_main(relay, "restart", "e1")
        assert terms.spawns[0]["mcp_flags"] == []          # inherit → no strict flags
        assert terms.spawns[0]["effort"] == "high"
        assert terms.spawns[0]["agent_flags"]

    def test_restart_flags_change_the_set_for_the_fresh_process(self, relay, terms):
        """main()'s restart --mcp/--effort help: "change the executor's MCP set / effort level for
        the fresh process"."""
        make_session(relay, "e1", status="dead", mcp="inherit", effort="low")
        run_main(relay, "restart", "e1", "--mcp", "none", "--effort", "max")
        s = relay.read_session("e1")
        assert s["mcp"] == "none" and s["effort"] == "max"
        assert "--strict-mcp-config" in " ".join(terms.spawns[0]["mcp_flags"])

    def test_restart_of_a_launch_failed_session_is_the_documented_recovery(self, relay, terms):
        """§12 #20: "`relay restart` is the recovery: it mints a fresh conversation id in a fresh
        tab" — restart must NOT inherit resume's launch-failed refusal."""
        make_session(relay, "e1", status=relay.LAUNCH_FAILED, pid=None)
        run_main(relay, "restart", "e1")
        assert relay.read_session("e1")["status"] == "busy"

    def test_a_legacy_null_model_gets_relays_default_not_the_clis(self, relay, terms):
        """_relaunch: "a legacy/null s["model"] must not silently fall through to the CLI's personal
        default a second time" — the 2026-07-12 model-leak incident."""
        make_session(relay, "e1", status="dead", model=None)
        run_main(relay, "restart", "e1")
        assert relay.read_session("e1")["model"] == "sonnet"
        assert terms.spawns[0]["model"] == "sonnet"


# ── resume (executor) ───────────────────────────────────────────────────────────────────────────

class TestResumeExecutor:
    """resume SKILL.md: "opens a fresh iTerm tab running `claude --resume <the executor's Claude
    session id>` … so the executor comes back with its **entire conversation/context**"."""

    @pytest.fixture(autouse=True)
    def _tab_is_gone(self, terms):
        """BUG-cli-4's fix consults the tab whenever there's no pid (every "dead"/pid-None fixture
        below), mirroring the lead-side guard's own default (see TestResumeLead._lead_tab_is_gone).
        The one test that wants the tab to still be alive flips this back explicitly."""
        terms.alive = False

    def test_resume_reopens_the_same_conversation(self, relay, terms, monkeypatch):
        make_session(relay, "e1", status="dead")
        monkeypatch.setattr(relay, "_launch_survived", lambda pid, **kw: True)
        run_main(relay, "resume", "e1")
        assert terms.spawns[0]["resume_id"] == "cs-e1"
        assert terms.spawns[0]["session_uuid"] is None
        assert relay.read_session("e1")["claude_session"] == "cs-e1"

    def test_resume_ledgers_the_conversation_id(self, relay, terms, monkeypatch):
        make_session(relay, "e1", status="dead")
        monkeypatch.setattr(relay, "_launch_survived", lambda pid, **kw: True)
        run_main(relay, "resume", "e1")
        assert [e["claude_session"] for e in ledger_events(relay, "resumed")] == ["cs-e1"]

    def test_resume_without_a_captured_id_points_at_restart(self, relay, terms):
        """"If relay says there's no captured id, use `/relay:restart` instead"."""
        make_session(relay, "e1", status="dead", claude_session=None)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "resume", "e1")
        assert "no captured Claude session id" in str(e.value)
        assert "relay restart e1" in str(e.value)

    def test_resume_refuses_a_live_process_without_force(self, relay, terms, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "resume", "e1")
        assert "still looks alive" in str(e.value)
        assert terms.spawns == []

    def test_resume_refuses_a_launch_failed_session(self, relay, terms):
        """§12 #21: "A conversation id that was never created can NEVER become valid, so resuming
        it is an infinite loop of identical failures"."""
        make_session(relay, "e1", status=relay.LAUNCH_FAILED, pid=None)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "resume", "e1")
        assert "its launch never happened" in str(e.value)
        assert "relay restart e1" in str(e.value)
        assert terms.spawns == []

    def test_force_resumes_a_launch_failed_session_anyway(self, relay, terms, monkeypatch):
        make_session(relay, "e1", status=relay.LAUNCH_FAILED, pid=None)
        monkeypatch.setattr(relay, "_launch_survived", lambda pid, **kw: True)
        run_main(relay, "resume", "e1", "--force")
        assert len(terms.spawns) == 1

    def test_a_relaunch_that_dies_immediately_is_recorded_launch_failed(self, relay, terms,
                                                                        monkeypatch):
        """§12 #21: "Only report success once that pid has actually survived a grace window —
        otherwise say plainly that it died and leave the marker honest"."""
        make_session(relay, "e1", status="dead")
        monkeypatch.setattr(relay, "_launch_survived", lambda pid, **kw: False)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "resume", "e1")
        assert "FAILED" in str(e.value) and "relay restart e1" in str(e.value)
        assert relay.read_session("e1")["status"] == relay.LAUNCH_FAILED

    def test_missing_transcript_only_warns(self, relay, terms, monkeypatch, capsys):
        """conversation_transcript_exists: "this is evidence for a WARNING, never grounds to refuse
        a resume on its own"."""
        make_session(relay, "e1", status="dead")
        monkeypatch.setattr(relay, "conversation_transcript_exists", lambda cs: False)
        monkeypatch.setattr(relay, "_launch_survived", lambda pid, **kw: True)
        run_main(relay, "resume", "e1")
        assert "no Claude Code transcript exists" in capsys.readouterr().err
        assert len(terms.spawns) == 1

    def test_resume_can_widen_the_mcp_set(self, relay, terms, monkeypatch):
        """resume --mcp help: "MCP servers load per process, so a resume is when the set can
        change"."""
        make_session(relay, "e1", status="dead", mcp="none")
        monkeypatch.setattr(relay, "_launch_survived", lambda pid, **kw: True)
        run_main(relay, "resume", "e1", "--mcp")
        assert relay.read_session("e1")["mcp"] == "inherit"
        assert terms.spawns[0]["mcp_flags"] == []

    def test_resume_adopts_for_the_calling_lead(self, relay, terms, monkeypatch):
        """resume SKILL.md: "**Ownership follows the resume.**"."""
        arm_lead(relay, "lead-new", "p")
        make_session(relay, "e1", status="dead", owner_lead="lead-old")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        monkeypatch.setattr(relay, "_launch_survived", lambda pid, **kw: True)
        run_main(relay, "resume", "e1")
        assert relay.read_session("e1")["owner_lead"] == "lead-new"

    def test_resume_refuses_when_only_the_tab_says_it_is_alive(self, relay, terms):
        """cmd_resume_lead states the invariant this guard exists for — "two live copies of one
        conversation stomp on each other" — and implements it as "a pid recorded by a previous
        restore, ELSE the marker's relay-controlled tab title", calling that a mirror of "the
        executor guard". cmd_spawn itself warns that a PID may be unreadable and that then
        "aliveness/stall detection will rely on tab title only". An executor with no captured pid
        and a live tab is exactly that case, and resume must refuse it without --force."""
        make_session(relay, "e1", status="busy", pid=None)
        terms.alive = True                       # the tab is right there, running claude
        with pytest.raises(SystemExit) as e:
            run_main(relay, "resume", "e1")
        assert "still looks alive" in str(e.value)


# ── resume (crashed lead) ───────────────────────────────────────────────────────────────────────

class TestResumeLead:
    """resume SKILL.md: "`/relay:resume <session-id>` also brings back a **crashed lead** … relay
    reopens its own tab, refreshes the marker's `last_active` … and writes no executor state"."""

    @pytest.fixture(autouse=True)
    def _lead_tab_is_gone(self, terms):
        """A crashed lead is one whose tab is gone; the restore refuses otherwise (two tests below
        exercise that refusal explicitly by turning aliveness back on)."""
        terms.alive = False

    def test_a_lead_sid_routes_to_the_lead_restore(self, relay, terms):
        arm_lead(relay, "lead-1", "webapp", model="opus")
        run_main(relay, "resume", "lead-1")
        assert terms.spawns[0]["resume_id"] == "lead-1"
        assert terms.spawns[0]["label"] == "[Lead] webapp"
        assert relay.read_session("lead-1") is None          # no executor state written
        assert [e["project"] for e in ledger_events(relay, "lead_resumed")] == ["webapp"]

    def test_the_restore_nudge_tells_it_to_re_verify_arming(self, relay, terms):
        arm_lead(relay, "lead-1", "webapp")
        run_main(relay, "resume", "lead-1")
        prompt = terms.spawns[0]["prompt"]
        assert "lead-start" in prompt and "$CLAUDE_CODE_SESSION_ID" in prompt
        assert "/relay:list" in prompt

    def test_the_refreshed_marker_keeps_started_and_predecessor(self, relay, terms):
        """cmd_resume_lead: started is "PRESERVED: it means 'when this lead began'"; predecessor is
        "the only record of the handoff-zombie tab `relay close-predecessor` closes"."""
        pred = {"session_id": "old", "tab_label": "[ex-Lead] webapp", "iterm_session": "w0t0p0:OLD"}
        arm_lead(relay, "lead-1", "webapp", started="2020-01-01T00:00:00", predecessor=pred)
        run_main(relay, "resume", "lead-1")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["started"] == "2020-01-01T00:00:00"
        assert m["predecessor"] == pred

    def test_the_restore_resets_autonomous_to_the_safe_posture(self, relay, terms):
        """cmd_resume_lead: autonomous is "RESET to the safe wait-for-human posture, never
        preserved (§6f …). Left at False rather than read from `autonomous_mode` config on
        purpose"."""
        arm_lead(relay, "lead-1", "webapp", autonomous=True, autonomous_source="command")
        cfg = relay.lead_guard.config_path(relay.STATE_ROOT)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"autonomous_mode": True}))
        run_main(relay, "resume", "lead-1")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert relay.lead_guard.autonomous_state(m) == (False, "config")

    def test_the_restore_re_stamps_backend_and_captures_the_new_handle(self, relay, terms):
        """§1/#1: `backend` is "re-stamped, not preserved … we're not guessing, we opened the tab"."""
        arm_lead(relay, "lead-1", "webapp", backend="terminal")
        run_main(relay, "resume", "lead-1")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["backend"] == relay.iterm.NAME
        assert m["iterm_session"] == "w0t0p0:STUB"
        assert m["tab_label"] == "[Lead] webapp"

    def test_a_live_lead_tab_refuses_the_restore(self, relay, terms):
        """"Refuse to open a SECOND live copy of the same conversation"."""
        arm_lead(relay, "lead-1", "webapp")
        terms.alive = True
        with pytest.raises(SystemExit) as e:
            run_main(relay, "resume", "lead-1")
        assert "still looks alive" in str(e.value)
        assert terms.spawns == []

    def test_force_restores_a_lead_that_looks_alive(self, relay, terms):
        arm_lead(relay, "lead-1", "webapp")
        terms.alive = True
        run_main(relay, "resume", "lead-1", "--force")
        assert len(terms.spawns) == 1

    def test_an_unknown_id_that_is_neither_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "resume", "nothing-at-all")
        assert "no such session" in str(e.value)


# ── adopt ───────────────────────────────────────────────────────────────────────────────────────

class TestAdopt:
    """README Troubleshooting: "use `relay adopt <sid>` to re-point ownership without sending
    anything"; cmd_adopt refuses a caller that "isn't an armed lead"."""

    def test_adopt_claims_an_executor_of_a_retired_lead(self, relay, terms, monkeypatch, capsys):
        arm_lead(relay, "lead-new", "p")
        make_session(relay, "e1", owner_lead="lead-gone")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        run_main(relay, "adopt", "e1")
        s = relay.read_session("e1")
        assert s["owner_lead"] == "lead-new" and s["owner_project"] == "p"
        assert "adopted 'e1' from retired lead" in capsys.readouterr().out

    def test_adopt_claims_an_unowned_executor(self, relay, terms, monkeypatch, capsys):
        arm_lead(relay, "lead-new", "p")
        make_session(relay, "e1", owner_lead=None)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        run_main(relay, "adopt", "e1")
        assert relay.read_session("e1")["owner_lead"] == "lead-new"
        assert "adopted unowned 'e1'" in capsys.readouterr().out

    def test_adopt_refuses_a_caller_that_is_not_an_armed_lead(self, relay, terms, monkeypatch):
        make_session(relay, "e1")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "not-a-lead")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "adopt", "e1")
        assert "isn't an armed lead" in str(e.value)
        assert relay.read_session("e1")["owner_lead"] is None

    def test_adopt_will_not_take_from_a_live_lead_without_force(self, relay, terms, monkeypatch,
                                                                capsys):
        arm_lead(relay, "lead-new", "p")
        arm_lead(relay, "lead-live", "q")
        (relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-live") / "pid").write_text(str(os.getpid()))
        make_session(relay, "e1", owner_lead="lead-live")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "adopt", "e1")
        assert e.value.code == 1
        assert relay.read_session("e1")["owner_lead"] == "lead-live"
        assert "--force" in capsys.readouterr().err

    def test_force_takes_it_from_a_live_lead_and_ledgers_that(self, relay, terms, monkeypatch):
        arm_lead(relay, "lead-new", "p")
        arm_lead(relay, "lead-live", "q")
        (relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-live") / "pid").write_text(str(os.getpid()))
        make_session(relay, "e1", owner_lead="lead-live")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        run_main(relay, "adopt", "e1", "--force")
        assert relay.read_session("e1")["owner_lead"] == "lead-new"
        rec = ledger_events(relay, "adopted")
        assert rec[-1]["forced"] is True and rec[-1]["from_lead"] == "lead-live"

    def test_adopt_of_an_unknown_session_is_refused(self, relay, terms, monkeypatch):
        arm_lead(relay, "lead-new", "p")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "adopt", "nope")
        assert "no such session: nope" in str(e.value)


# ── keep ────────────────────────────────────────────────────────────────────────────────────────

class TestKeep:
    """README Auto-close: the sweep never touches "pinned ones (`relay keep <sid>` / `spawn
    --keep`; `keep --off` unpins)"."""

    def test_keep_pins_and_ledgers(self, relay, terms, capsys):
        make_session(relay, "e1")
        run_main(relay, "keep", "e1")
        assert relay.read_session("e1")["keep"] is True
        assert ledger_events(relay, "keep")[-1]["keep"] is True
        assert "leave it alone" in capsys.readouterr().out

    def test_keep_off_unpins(self, relay, terms, capsys):
        make_session(relay, "e1", keep=True)
        run_main(relay, "keep", "e1", "--off")
        assert relay.read_session("e1")["keep"] is False
        assert "park it once finished" in capsys.readouterr().out

    def test_keep_on_an_unknown_session_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "keep", "nope")
        assert "no such session: nope" in str(e.value)


# ── the auto-close sweep ────────────────────────────────────────────────────────────────────────

class TestAutoCloseSweep:
    """README "Auto-close": parks on **landed** (claimed files clean, after a 2-minute grace) or
    **idle** (`auto_close_idle_minutes`); both "require that the owning lead has **already seen the
    report**"; never touches busy/stalled sessions, ones with a queue, unowned ones, or pinned
    ones; "A heavy session … is **retired** (seed written) instead of closed"."""

    def _finished(self, relay, tmp_path, sid="e1", lead="lead-1", claims=True, age=300, **over):
        wt = git_repo(tmp_path / f"wt-{sid}")
        arm_lead(relay, lead, "p")
        make_session(relay, sid, owner_lead=lead, status="reported", worktree=str(wt), **over)
        rp = relay.packets_dir(sid) / "001-report.md"
        rp.write_text("Did it.\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\n\n"
                      "## What changed\n" + ("- `seed.txt:1` touched\n" if claims else "- nothing\n"))
        age_file(rp, age)
        return wt

    def test_landed_report_is_parked(self, relay, terms, tmp_path):
        self._finished(relay, tmp_path)
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-1", ["e1:1"])
        acted = relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1")
        assert acted == [("e1", "close", "landed")]
        assert relay.read_session("e1")["status"] == "closed"
        assert relay.read_session("e1")["auto_closed"] == "landed"

    def test_an_unsurfaced_report_is_never_parked(self, relay, terms, tmp_path):
        """"relay never parks a report nobody looked at"."""
        self._finished(relay, tmp_path)
        assert relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1") == []
        assert relay.read_session("e1")["status"] == "reported"

    def test_a_pinned_session_is_never_parked(self, relay, terms, tmp_path):
        self._finished(relay, tmp_path, keep=True)
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-1", ["e1:1"])
        assert relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1") == []

    def test_an_unowned_session_is_never_parked(self, relay, terms, tmp_path):
        """auto_close_sweep: "unowned: no lead could have seen the report → never auto-park"."""
        self._finished(relay, tmp_path)
        s = relay.read_session("e1"); s["owner_lead"] = None; relay.write_session("e1", s)
        assert relay.auto_close_sweep("test", sids=["e1"]) == []

    def test_a_queued_packet_holds_the_session_open(self, relay, terms, tmp_path):
        self._finished(relay, tmp_path)
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-1", ["e1:1"])
        relay.enqueue_packet("e1", "GOAL — later job.\n", "src")
        assert relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1") == []

    def test_a_dirty_claimed_path_blocks_the_landed_reason(self, relay, terms, tmp_path):
        wt = self._finished(relay, tmp_path)
        (wt / "seed.txt").write_text("edited\n")     # the claimed path is still dirty
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-1", ["e1:1"])
        assert relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1") == []

    def test_a_dirty_claimed_path_still_parks_on_the_idle_timer(self, relay, terms, tmp_path):
        wt = self._finished(relay, tmp_path, age=7200)
        (wt / "seed.txt").write_text("edited\n")
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-1", ["e1:1"])
        acted = relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1")
        assert acted and acted[0][2].startswith("idle ")

    def test_a_zero_idle_timer_turns_the_timer_path_off(self, relay, terms, tmp_path):
        """README Config: `auto_close_idle_minutes` "(default 60; 0 turns the timer off)"."""
        wt = self._finished(relay, tmp_path, age=7200)
        (wt / "seed.txt").write_text("edited\n")
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-1", ["e1:1"])
        cfg = relay.lead_guard.config_path(relay.STATE_ROOT)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"auto_close_idle_minutes": 0}))
        assert relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1") == []

    def test_auto_close_off_disables_the_sweep_entirely(self, relay, terms, tmp_path):
        self._finished(relay, tmp_path)
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-1", ["e1:1"])
        cfg = relay.lead_guard.config_path(relay.STATE_ROOT)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"auto_close": False}))
        assert relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1") == []

    def test_a_heavy_session_is_retired_with_a_seed_instead_of_closed(self, relay, terms, tmp_path,
                                                                      monkeypatch):
        self._finished(relay, tmp_path)
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-1", ["e1:1"])
        monkeypatch.setattr(relay, "_is_heavy", lambda *a, **k: True)
        acted = relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1")
        assert acted == [("e1", "retire", "landed")]
        assert (relay.session_dir("e1") / relay.SEED_FILENAME).is_file()
        assert relay.read_session("e1")["superseded_by"] == relay.SEED_RETIRED_BY_SEED

    def test_landing_is_ledgered_before_the_park(self, relay, terms, tmp_path):
        """README: "the ledger records `auto_closed` (and, right before it, a `landed` event when
        the reason is "landed")"."""
        self._finished(relay, tmp_path)
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-1", ["e1:1"])
        relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-1")
        names = [e["event"] for e in ledger_events(relay) if e.get("session_id") == "e1"]
        assert names.index("landed") < names.index("auto_closed")

    def test_the_sweep_never_raises_on_a_broken_session(self, relay, terms, tmp_path):
        """auto_close_sweep: "Deterministic, best-effort, never raises". BUG-cli-1 fixed:
        read_session now reads a corrupt session.json as None (same convention as read_queue's
        own corrupt-file handling) instead of raising, so the per-sid `if not s: continue` guard
        absorbs it silently — no exception ever reaches the inner except, hence no
        `auto_close_error` ledger entry for this shape of corruption."""
        d = relay.session_dir("broken"); d.mkdir(parents=True, exist_ok=True)
        (d / "session.json").write_text("{ not json")
        assert relay.auto_close_sweep("test", sids=["broken"]) == []
        assert not ledger_events(relay, "auto_close_error")

    def test_lead_scoped_sweep_ignores_another_leads_executor(self, relay, terms, tmp_path):
        self._finished(relay, tmp_path, sid="e1", lead="lead-A")
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-A", ["e1:1"])
        assert relay.auto_close_sweep("test", sids=["e1"], lead_sid="lead-B") == []
        assert relay.read_session("e1")["status"] == "reported"

    def test_check_all_does_not_park_another_leads_executor(self, relay, terms, tmp_path,
                                                            monkeypatch):
        """README "Auto-close": "The sweep runs on `relay check`, `relay list`, and every lead
        turn-end (the Stop hook), **scoped to the lead's own executors**." Only the Stop-hook path
        (cmd_auto_close_sweep) passes `lead_sid`; cmd_check and cmd_list call
        `auto_close_sweep(..., sids=...)` with no owner scope at all."""
        self._finished(relay, tmp_path, sid="e1", lead="lead-B")
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-B", ["e1:1"])
        arm_lead(relay, "lead-A", "a")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-A")   # lead A is the one running check
        run_main(relay, "check", "--all")
        assert relay.read_session("e1")["status"] == "reported"


class TestAutoCloseSweepCommand:
    """cmd_auto_close_sweep is the lead Stop hook's trigger: "Refreshes liveness for the lead's own
    executors, then sweeps. Fail-open; prints one line per action"."""

    def _finished(self, relay, tmp_path, sid, lead):
        wt = git_repo(tmp_path / f"wt-{sid}")
        arm_lead(relay, lead, lead)
        make_session(relay, sid, owner_lead=lead, status="reported", worktree=str(wt))
        rp = relay.packets_dir(sid) / "001-report.md"
        rp.write_text("Did it.\nStatus: clean\n\n## What changed\n- `seed.txt:1` touched\n")
        age_file(rp, 9999)
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, lead, [f"{sid}:1"])
        return wt

    def test_it_parks_the_named_leads_executor_and_prints_one_line(self, relay, terms, tmp_path,
                                                                   capsys):
        self._finished(relay, tmp_path, "e1", "lead-A")
        run_main(relay, "_auto-close-sweep", "--lead", "lead-A")
        assert relay.read_session("e1")["status"] == "closed"
        assert "auto-closed 'e1' (landed)" in capsys.readouterr().out

    def test_it_leaves_another_leads_executor_alone(self, relay, terms, tmp_path):
        self._finished(relay, tmp_path, "e1", "lead-A")
        self._finished(relay, tmp_path, "e2", "lead-B")
        run_main(relay, "_auto-close-sweep", "--lead", "lead-A")
        assert relay.read_session("e1")["status"] == "closed"
        assert relay.read_session("e2")["status"] == "reported"

    def test_it_skips_terminal_status_sessions(self, relay, terms, tmp_path):
        """cmd_auto_close_sweep only refreshes liveness for sessions not already dead/superseded/
        closed/launch-failed."""
        make_session(relay, "gone", status="closed", owner_lead="lead-A")
        arm_lead(relay, "lead-A", "a")
        run_main(relay, "_auto-close-sweep", "--lead", "lead-A")
        assert relay.read_session("gone")["status"] == "closed"

    def test_it_is_fail_open_on_broken_state(self, relay, terms, capsys):
        """"Fail-open … a queue problem must never disturb the executor's own Stop behavior" —
        the hook discards stdout, so a raise here would be an invisible turn-end failure."""
        d = relay.session_dir("broken"); d.mkdir(parents=True, exist_ok=True)
        (d / "session.json").write_text("{ not json")
        run_main(relay, "_auto-close-sweep", "--lead", "lead-A")   # must not raise
