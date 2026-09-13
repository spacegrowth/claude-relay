"""
Bug-hunt unit tests for `relay close` / `relay retire` / `relay prune` (packet bh-cli). Split out
of tests/test_cli_lifecycle.py to keep each file under the packet's ~800-line bound.

Every assertion is anchored to a documented contract — README.md, skills/*/SKILL.md,
docs/post-0.3.27-backlog.md, or the function's own docstring — named in each test's docstring.

Run: pytest tests/test_cli_close_retire_prune.py -q
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


# ── close / retire ──────────────────────────────────────────────────────────────────────────────

class TestClose:
    """close SKILL.md: closing marks the session done and closes its tab; `--supersede` records the
    replacement; `--keep-tab` "marks closed but leaves the iTerm tab open"."""

    def test_close_marks_closed_kills_and_closes_the_tab(self, relay, terms, capsys):
        make_session(relay, "e1")
        run_main(relay, "close", "e1")
        assert relay.read_session("e1")["status"] == "closed"
        assert terms.closes and terms.closes[0]["handle"] == "w0t0p0:STUB"
        assert "closed its tab" in capsys.readouterr().out

    def test_supersede_records_the_replacement(self, relay, terms, capsys):
        make_session(relay, "e1")
        make_session(relay, "e2")
        run_main(relay, "close", "e1", "--supersede", "e2")
        s = relay.read_session("e1")
        assert s["status"] == "superseded" and s["superseded_by"] == "e2"
        assert [e["superseded_by"] for e in ledger_events(relay, "superseded")] == ["e2"]

    def test_keep_tab_leaves_the_tab_open_and_retitles_it(self, relay, terms):
        """§4: "a tab that OUTLIVES its session must say so" — retitled `[closed] <name>`, and
        "ALWAYS by recorded HANDLE (rename_by_id), never by label match"."""
        make_session(relay, "e1")
        run_main(relay, "close", "e1", "--keep-tab")
        assert terms.closes == []
        assert {"handle": "w0t0p0:STUB", "name": "[closed] e1"} in terms.renames
        assert relay.read_session("e1")["tab_label"] == "[closed] e1"

    def test_a_lingering_tab_is_retitled(self, relay, terms, capsys):
        """"Terminal.app ignores scripted window-close on some macOS versions … Retitle it so it at
        least identifies itself while it lingers"."""
        make_session(relay, "e1")
        terms.close_ok = False
        terms.alive = True
        run_main(relay, "close", "e1")
        assert relay.read_session("e1")["tab_label"] == "[closed] e1"
        assert "Cmd-W it if it lingers" in capsys.readouterr().out

    def test_a_self_closing_tab_is_not_reported_as_lingering(self, relay, terms, capsys):
        """"iTerm auto-closes a session's tab when its exec'd process dies — close() then finds
        nothing, which is success, not a lingering tab"."""
        make_session(relay, "e1")
        terms.close_ok = False
        terms.alive = False
        run_main(relay, "close", "e1")
        assert "tab closed itself" in capsys.readouterr().out

    def test_close_stamps_every_packets_report_surfaced(self, relay, terms, monkeypatch):
        """#42: "close stamps ALL packet reports" — otherwise a closed executor whose older packets
        were never stamped re-announces forever."""
        arm_lead(relay, "lead-1", "p")
        make_session(relay, "e1", owner_lead="lead-1", current_packet=3)
        for n in (1, 2, 3):
            (relay.packets_dir("e1") / f"{n:03d}-report.md").write_text("done\n")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-1")
        run_main(relay, "close", "e1")
        surfaced = relay.lead_guard.load_surfaced(relay.STATE_ROOT, "lead-1")
        assert {"e1:1", "e1:2", "e1:3"} <= set(surfaced)

    def test_close_of_an_unknown_session_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "close", "nope")
        assert "no such session: nope" in str(e.value)

    def test_bare_close_without_self_explains_both_uses(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "close")
        assert "--self" in str(e.value)


class TestRetire:
    """retire SKILL.md: retire "closes the session (as `superseded` by its seed, tab and all — same
    teardown as `/relay:close`) and writes a **successor seed** to its state dir: an index of every
    packet it was sent, with each report's outcome line, `Status`, `Risk flags` and `UNVERIFIED`"."""

    def test_the_seed_indexes_every_packet_and_its_tldr(self, relay, terms):
        make_session(relay, "e1", current_packet=2, topic="charts", scope="ui",
                     model="claude-sonnet-5", mcp="none", report=False)
        (relay.packets_dir("e1") / "001-packet.md").write_text("GOAL — first job.\n")
        (relay.packets_dir("e1") / "001-report.md").write_text(
            "Landed the first job.\nStatus: clean-with-caveats\nRisk flags: touched the ledger\n"
            "UNVERIFIED: the DST path\n")
        (relay.packets_dir("e1") / "002-packet.md").write_text("GOAL — second job.\n")
        run_main(relay, "retire", "e1")
        seed = (relay.session_dir("e1") / relay.SEED_FILENAME).read_text()
        assert "### 001 — GOAL — first job." in seed
        assert "- Outcome: Landed the first job." in seed
        assert "- Status: clean-with-caveats" in seed
        assert "- Risk flags: touched the ledger" in seed
        assert "- UNVERIFIED: the DST path" in seed
        assert "### 002 — GOAL — second job." in seed
        assert "NO REPORT" in seed              # packet 002 was never reported
        assert "Packets worked: 2 (1 reported, 1 unreported)" in seed

    def test_retire_closes_as_superseded_by_seed(self, relay, terms):
        make_session(relay, "e1")
        run_main(relay, "retire", "e1")
        s = relay.read_session("e1")
        assert s["status"] == "superseded"
        assert s["superseded_by"] == relay.SEED_RETIRED_BY_SEED
        assert terms.closes                      # same teardown as close
        assert ledger_events(relay, "retired")[-1]["packets"] == 1

    def test_a_later_spawn_seed_finds_it_by_sid(self, relay, terms, tmp_path):
        """"You can also pass a path to the seed file instead of the retired session's id"."""
        make_session(relay, "e1", topic="charts")
        (relay.packets_dir("e1") / "001-packet.md").write_text("GOAL — the charts work.\n")
        run_main(relay, "retire", "e1")
        run_main(relay, "spawn", str(tmp_path), "charts", write_packet(tmp_path),
                 "--name", "e2", "--seed", "e1")
        body = (relay.packets_dir("e2") / "001-packet.md").read_text()
        assert "Successor seed — e1" in body and "the charts work" in body

    def test_retiring_a_busy_unreported_session_is_refused(self, relay, terms, live_pid):
        """"retiring now kills that work unreported, and the seed cannot summarise a report that
        was never written"."""
        make_session(relay, "e1", status="busy", pid=live_pid, report=False)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "retire", "e1")
        assert "still busy on packet 001 and has not reported" in str(e.value)
        assert "--force" in str(e.value)
        assert not (relay.session_dir("e1") / relay.SEED_FILENAME).exists()

    def test_force_retires_a_busy_session_and_seeds_it_as_no_report(self, relay, terms, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid, report=False)
        run_main(relay, "retire", "e1", "--force")
        seed = (relay.session_dir("e1") / relay.SEED_FILENAME).read_text()
        assert "NO REPORT" in seed
        assert ledger_events(relay, "retired")[-1]["forced"] is True

    def test_retiring_twice_is_refused_and_prints_the_seed_path(self, relay, terms):
        make_session(relay, "e1")
        run_main(relay, "retire", "e1")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "retire", "e1")
        assert "already retired" in str(e.value)
        assert relay.SEED_FILENAME in str(e.value)
        assert "--seed e1" in str(e.value)

    def test_a_never_sent_session_still_gets_an_honest_seed(self, relay, terms):
        make_session(relay, "e1", current_packet=1, report=False)
        (relay.packets_dir("e1") / "001-packet.md").unlink()
        run_main(relay, "retire", "e1")
        seed = (relay.session_dir("e1") / relay.SEED_FILENAME).read_text()
        assert "retired before it was ever sent a packet" in seed

    def test_keep_tab_retires_without_closing_the_tab(self, relay, terms):
        make_session(relay, "e1")
        run_main(relay, "retire", "e1", "--keep-tab")
        assert terms.closes == []
        assert relay.read_session("e1")["superseded_by"] == relay.SEED_RETIRED_BY_SEED

    def test_retire_of_an_unknown_session_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "retire", "nope")
        assert "no such session: nope" in str(e.value)


# ── prune ───────────────────────────────────────────────────────────────────────────────────────

class TestPrune:
    """cmd_prune: "Delete the state dirs of terminal-status sessions (closed/superseded/dead) whose
    last update is older than --days, AND dead lead markers … Nothing else is touched: live
    sessions, live leads, and the sessions.jsonl ledger (the durable history) all stay"."""

    def _aged(self, relay, sid, status, days):
        s = make_session(relay, sid, status=status, report=False)
        s["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.localtime(time.time() - days * 86400))
        relay.write_session(sid, s)

    def test_only_terminal_statuses_are_pruned(self, relay, terms, capsys):
        for status in ("closed", "superseded", "dead", "busy", "reported", "stalled",
                       relay.LAUNCH_FAILED):
            self._aged(relay, f"e-{status}", status, 30)
        run_main(relay, "prune", "--days", "7")
        left = set(relay.all_session_ids())
        assert left == {"e-busy", "e-reported", "e-stalled", f"e-{relay.LAUNCH_FAILED}"}

    def test_a_recent_terminal_session_is_kept(self, relay, terms, capsys):
        self._aged(relay, "old", "closed", 30)
        self._aged(relay, "new", "closed", 1)
        run_main(relay, "prune", "--days", "7")
        assert relay.all_session_ids() == ["new"]

    def test_dry_run_deletes_nothing_and_says_would_prune(self, relay, terms, capsys):
        self._aged(relay, "old", "closed", 30)
        run_main(relay, "prune", "--days", "7", "--dry-run")
        assert relay.all_session_ids() == ["old"]
        out = capsys.readouterr().out
        assert "would prune 1 session(s)" in out and "old (closed)" in out
        assert ledger_events(relay, "pruned") == []

    def test_an_unparsable_updated_stamp_counts_as_ancient(self, relay, terms):
        """"unparseable/missing → treat as ancient, prunable"."""
        s = make_session(relay, "old", status="closed", report=False)
        s["updated"] = "not-a-timestamp"
        relay.write_session("old", s)
        run_main(relay, "prune", "--days", "7")
        assert relay.all_session_ids() == []

    def test_the_ledger_survives_a_prune(self, relay, terms):
        """"the sessions.jsonl ledger (the durable history) [stays]"."""
        self._aged(relay, "old", "closed", 30)
        relay.append_ledger("spawned", session_id="old")
        run_main(relay, "prune", "--days", "7")
        assert ledger_events(relay, "spawned")
        assert [e["session_id"] for e in ledger_events(relay, "pruned")] == ["old"]

    def test_a_ghost_lead_is_pruned(self, relay, terms, capsys):
        """"dead lead markers ("ghosts": tabs closed/crashed without `/relay:stop`…)"."""
        arm_lead(relay, "ghost", "gone")
        relay.lead_guard.update_marker(relay.STATE_ROOT, "ghost",
                                       last_active="2020-01-01T00:00:00")
        terms.alive = False
        run_main(relay, "prune", "--days", "7")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "ghost") == {}
        assert "[lead] gone" in capsys.readouterr().out

    def test_the_calling_lead_is_never_pruned(self, relay, terms, monkeypatch):
        """"never prune the calling lead, even if it somehow probes dead"."""
        arm_lead(relay, "me", "mine")
        relay.lead_guard.update_marker(relay.STATE_ROOT, "me", last_active="2020-01-01T00:00:00")
        terms.alive = False
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "me")
        run_main(relay, "prune", "--days", "7")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "me")

    def test_a_live_lead_is_never_pruned(self, relay, terms):
        """"a lead you're actively using is never pruned" (README)."""
        arm_lead(relay, "live", "here")
        relay.lead_guard.update_marker(relay.STATE_ROOT, "live", last_active="2020-01-01T00:00:00")
        (relay.lead_guard.lead_dir(relay.STATE_ROOT, "live") / "pid").write_text(str(os.getpid()))
        run_main(relay, "prune", "--days", "7")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "live")

    def test_a_recently_active_lead_is_never_pruned(self, relay, terms):
        arm_lead(relay, "recent", "here")
        terms.alive = False
        run_main(relay, "prune", "--days", "7")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "recent")

    def _paused_and_stale(self, relay, sid, project, live_pid=None):
        """A tombstoned (paused) lead marker, aged past any cutoff. `live_pid`, when given, makes
        `_lead_alive` read True purely off the pid file — the exact shape backlog row 77 names:
        the conversation process ended (tombstoned) but something keeping the tab's PID entry alive
        (or, in real life, the tab itself still open) means the OLD liveness guard alone would have
        kept this lead forever."""
        arm_lead(relay, sid, project)
        relay.lead_guard.tombstone_lead(relay.STATE_ROOT, sid, reason="exit", notify=False)
        relay.lead_guard.update_marker(relay.STATE_ROOT, sid, last_active="2020-01-01T00:00:00")
        if live_pid is not None:
            (relay.lead_guard.lead_dir(relay.STATE_ROOT, sid) / "pid").write_text(str(live_pid))

    def test_a_stale_paused_lead_is_pruned_even_though_its_tab_still_probes_alive(
            self, relay, terms, capsys, live_pid):
        """Row 77: "`relay prune` skips paused leads at ANY age" because `_lead_alive` reads a
        paused lead's still-open tab as alive — the tab-liveness probe must be skipped entirely for
        a paused lead, judging staleness on the timestamp alone, same as a ghost."""
        self._paused_and_stale(relay, "paused-old", "gone-for-good", live_pid=live_pid)
        run_main(relay, "prune", "--days", "7")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "paused-old") == {}
        out = capsys.readouterr().out
        assert "[lead, paused] gone-for-good" in out
        events = ledger_events(relay, "lead_pruned")
        assert len(events) == 1 and events[0]["session_id"] == "paused-old"
        assert events[0]["paused"] is True

    def test_a_paused_lead_newer_than_the_cutoff_is_kept(self, relay, terms, capsys):
        """A paused lead is not indiscriminately swept — only one whose stamp is actually older
        than --days loses its resumability."""
        arm_lead(relay, "paused-fresh", "still-resumable")
        relay.lead_guard.tombstone_lead(relay.STATE_ROOT, "paused-fresh", reason="exit",
                                        notify=False)
        run_main(relay, "prune", "--days", "7")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "paused-fresh")
        assert "[lead, paused]" not in capsys.readouterr().out
        assert ledger_events(relay, "lead_pruned") == []

    def test_dry_run_lists_a_stale_paused_lead_and_clears_nothing(self, relay, terms, capsys):
        self._paused_and_stale(relay, "paused-old", "dry-project")
        run_main(relay, "prune", "--days", "7", "--dry-run")
        out = capsys.readouterr().out
        assert "would prune" in out and "[lead, paused] dry-project" in out
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "paused-old")
        assert ledger_events(relay, "lead_pruned") == []

    def test_the_calling_lead_is_never_pruned_even_when_paused_and_stale(self, relay, terms,
                                                                         monkeypatch):
        """"never prune the calling lead" holds even for its own paused-and-stale marker — a
        session cannot be both the caller AND already gone."""
        self._paused_and_stale(relay, "me", "mine")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "me")
        run_main(relay, "prune", "--days", "7")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "me")
        assert ledger_events(relay, "lead_pruned") == []

    def test_nothing_to_prune_says_so(self, relay, terms, capsys):
        self._aged(relay, "new", "closed", 1)
        run_main(relay, "prune", "--days", "7")
        assert "nothing to prune" in capsys.readouterr().out
