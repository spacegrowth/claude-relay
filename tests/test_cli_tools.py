"""
Bug-hunt unit tests for relay's read-only / diagnostic commands (packet bh-cli):
`lint` / `stats` / `doctor` / `board`, plus ledger integrity and corrupt-state resilience across
every multi-session command.

Every assertion is anchored to a documented contract — README.md, skills/*/SKILL.md,
docs/post-0.3.27-backlog.md, or the function's own docstring — named in each test's docstring.
Where the code contradicts the contract the test asserts the CONTRACT and carries an
`xfail(strict=True)` naming the finding in tests/bughunt/cli-findings.md.

`relay doctor`'s live probes shell out to the installed `claude`; every one of them is stubbed
here (`_probe_claude` / `_cli_version` / `subprocess.run`), so no test ever runs the real binary.

Run: pytest tests/test_cli_tools.py -q
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


def arm_lead(relay, sid, project="proj", **kw):
    relay.lead_guard.write_marker(relay.STATE_ROOT, sid, project=project, cwd="/tmp",
                                  tab_label=f"[Lead] {project}", **kw)


def corrupt_session(relay, sid="broken", text="{ not json"):
    """A session dir that `all_session_ids()` finds but `read_session()` cannot parse — a
    hand-edited or half-written session.json, which the docstrings below say must never take a
    command down."""
    d = relay.session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(text)
    return d


# ── relay lint ──────────────────────────────────────────────────────────────────────────────────

class TestLint:
    """README Commands: "advisory packet checks (MCP mentioned but not declared, big reading on
    200K, asks to commit/ask, no Preconditions, haiku/opus packet-shape hints, front-loaded
    reading…); the same checks print at spawn/send". cmd_lint: "Exit 1 under --strict when any
    warn-level finding"."""

    def _lint(self, relay, tmp_path, body, *flags):
        p = tmp_path / "p.md"
        p.write_text(body)
        return run_main(relay, "lint", str(p), *flags)

    def test_a_clean_packet_reports_no_findings(self, relay, terms, tmp_path, capsys):
        self._lint(relay, tmp_path,
                   "GOAL — add the missing branch to the parser and cover it with a test.\n\n"
                   "## Work\nEdit `lib/parse.py` and add the branch; extend `tests/test_parse.py`.\n\n"
                   "## Preconditions\n- the checkout is pulled\n")
        assert "✓ packet lint: no findings" in capsys.readouterr().out

    def test_a_very_short_packet_is_warned_about(self, relay, terms, tmp_path, capsys):
        self._lint(relay, tmp_path, "fix it\n\n## Preconditions\n- ok\n")
        assert "[short-packet]" in capsys.readouterr().out

    def test_a_missing_preconditions_section_is_warned_about(self, relay, terms, tmp_path, capsys):
        self._lint(relay, tmp_path,
                   "GOAL — do the bounded thing described here at some length.\n\n"
                   "## Work\nEdit the module and add the missing branch, then run the suite.\n")
        assert "[no-preconditions]" in capsys.readouterr().out

    def test_a_mentioned_but_undeclared_mcp_server_is_warned_about(self, relay, terms, tmp_path,
                                                                   capsys):
        self._lint(relay, tmp_path,
                   "GOAL — sync the Linear issue states into the dashboard, then verify.\n\n"
                   "## Work\nRead the Linear board and reconcile it with the local list.\n\n"
                   "## Preconditions\n- ok\n")
        assert "[mcp-mentioned-not-declared]" in capsys.readouterr().out

    @pytest.mark.xfail(strict=True, reason="BUG-cli-6: the `mcp-unparsable` lint finding can "
                                           "never fire — normalize_mcp_spec turns any non-empty "
                                           "value into an allowlist instead of None")
    def test_an_unparsable_mcp_line_is_warned_about(self, relay, terms, tmp_path, capsys):
        """lint_packet declares the finding and its message names the contract:
        `"MCP: line present but its value isn't none/inherit/a,b"` (lib/lead_guard.py, the
        `mcp_line and mcp_spec is None` branch). It is unreachable: `normalize_mcp_spec` returns
        None for NOTHING — every non-empty string falls through to `split(",")` and comes back as
        an allowlist — so `mcp_spec is None` implies `mcp_line is None`. A genuinely malformed
        line is silently read as a list of junk server names, and `relay lint` without
        `--worktree` (which is how the README documents it) has no known-server set to catch them
        with either, so it reports nothing at all. (Locus is lib/lead_guard.py; filed here because
        this is `relay lint`'s user-visible behaviour.)"""
        self._lint(relay, tmp_path,
                   "GOAL — do the bounded thing described here at some length.\n\n"
                   "MCP: yes please if you can\n\n## Preconditions\n- ok\n")
        assert "[mcp-unparsable]" in capsys.readouterr().out

    def test_an_unparsable_effort_line_is_warned_about(self, relay, terms, tmp_path, capsys):
        """#41: "lint flags unparsable values" — "the CLI would ignore it"."""
        self._lint(relay, tmp_path,
                   "GOAL — do the bounded thing described here at some length.\n\n"
                   "EFFORT: turbo\n\n## Preconditions\n- ok\n")
        assert "[effort-unparsable]" in capsys.readouterr().out

    def test_an_unparsable_context_line_is_warned_about(self, relay, terms, tmp_path, capsys):
        self._lint(relay, tmp_path,
                   "GOAL — do the bounded thing described here at some length.\n\n"
                   "CONTEXT: huge\n\n## Preconditions\n- ok\n")
        assert "[context-unparsable]" in capsys.readouterr().out

    def test_a_packet_that_tells_the_executor_to_commit_is_warned_about(self, relay, terms,
                                                                        tmp_path, capsys):
        """"executors stage only (commit/push are denied); phrase it as 'stage for review'"."""
        self._lint(relay, tmp_path,
                   "GOAL — add the branch, then git commit the result on a new branch.\n\n"
                   "## Preconditions\n- ok\n")
        assert "[asks-to-commit]" in capsys.readouterr().out

    def test_a_packet_that_tells_the_executor_to_ask_is_warned_about(self, relay, terms, tmp_path,
                                                                     capsys):
        """"executors never ask in the tab; phrase it as 'stop and report the blocker'"."""
        self._lint(relay, tmp_path,
                   "GOAL — do the bounded thing; if the schema is unclear, ask the user first.\n\n"
                   "## Preconditions\n- ok\n")
        assert "[asks-to-ask]" in capsys.readouterr().out

    def test_a_1m_context_line_on_haiku_is_warned_about(self, relay, terms, tmp_path, capsys):
        self._lint(relay, tmp_path,
                   "GOAL — do the bounded thing described here at some length.\n\n"
                   "CONTEXT: 1m\n\n## Preconditions\n- ok\n", "--model", "haiku")
        assert "[context-1m-on-haiku]" in capsys.readouterr().out

    def test_strict_exits_1_on_a_warn_level_finding(self, relay, terms, tmp_path):
        with pytest.raises(SystemExit) as e:
            self._lint(relay, tmp_path, "fix it\n", "--strict")
        assert e.value.code == 1

    def test_strict_exits_0_when_only_info_findings(self, relay, terms, tmp_path, capsys):
        """"Advisory only — prints, never blocks"; only warn-level findings fail --strict."""
        self._lint(relay, tmp_path,
                   "GOAL — investigate why the parser drops the trailing branch, then fix it.\n\n"
                   "## Work\nStart from `lib/parse.py` and `tests/test_parse.py`.\n\n"
                   "## Preconditions\n- the checkout is pulled\n", "--strict")
        out = capsys.readouterr().out
        assert "[shape-opus]" in out and "⚠" not in out

    def test_json_emits_the_findings_as_data(self, relay, terms, tmp_path, capsys):
        self._lint(relay, tmp_path, "fix it\n", "--json")
        data = json.loads(capsys.readouterr().out)
        assert {"level", "code", "message"} <= set(data[0])
        assert "short-packet" in {f["code"] for f in data}

    def test_a_worktree_resolves_referenced_files(self, relay, terms, tmp_path, capsys):
        """`--worktree W`: "resolve referenced files / configured MCP servers against this dir"."""
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "big.py").write_bytes(b"x" * 700_000)
        p = tmp_path / "p.md"
        p.write_text("GOAL — rewrite the module.\n\n## Required reading\n- `big.py`\n\n"
                     "CONTEXT: 200k\n\n## Preconditions\n- ok\n")
        run_main(relay, "lint", str(p), "--worktree", str(wt))
        assert "[context-200k-big-reading]" in capsys.readouterr().out

    def test_lint_never_blocks_a_spawn(self, relay, terms, tmp_path, capsys):
        """"Advisory; the ⚠/ℹ lines print at spawn/send" — a warn must not stop the launch."""
        p = tmp_path / "p.md"
        p.write_text("fix it\n")
        run_main(relay, "spawn", str(tmp_path), "t", str(p), "--name", "e1")
        assert relay.read_session("e1")["status"] == "busy"
        assert "lint[short-packet]" in capsys.readouterr().out


# ── relay stats ─────────────────────────────────────────────────────────────────────────────────

class TestStats:
    """README "relay stats": one row per packet, joining (model, effort) to ROUNDS / VERDICT /
    STATUS; "Closed/superseded/dead sessions are included — that's where the history is";
    "`--lead <sid>` scopes to that lead's executors plus unowned ones"; "`--since DAYS` filters on
    the packet's send ledger timestamp (a packet with no recorded send time is never filtered
    out)"."""

    def _packet(self, relay, sid, n, gist="GOAL — the job.", report=None):
        (relay.packets_dir(sid) / f"{n:03d}-packet.md").write_text(gist + "\n")
        if report is not None:
            (relay.packets_dir(sid) / f"{n:03d}-report.md").write_text(report)

    def test_a_row_per_packet_with_model_effort_and_report_status(self, relay, terms, capsys):
        make_session(relay, "e1", model="claude-sonnet-5", effort="high", report=False)
        self._packet(relay, "e1", 1, report="Did it.\nStatus: clean\n")
        run_main(relay, "stats", "--json")
        data = json.loads(capsys.readouterr().out)
        assert data["rows"] == [{"session_id": "e1", "packet": "001", "model": "sonnet",
                                 "effort": "high", "rounds": "-", "verdict": "-",
                                 "status": "clean"}]

    def test_closed_and_superseded_sessions_are_included(self, relay, terms, capsys):
        for sid, status in (("e1", "closed"), ("e2", "superseded"), ("e3", "dead")):
            make_session(relay, sid, status=status, report=False)
            self._packet(relay, sid, 1, report="Did it.\nStatus: clean\n")
        run_main(relay, "stats", "--json")
        data = json.loads(capsys.readouterr().out)
        assert {r["session_id"] for r in data["rows"]} == {"e1", "e2", "e3"}

    def test_the_last_verify_verdict_wins(self, relay, terms, capsys):
        """"**Verdict** = the last `report_verify` ledger event for that session+packet"."""
        make_session(relay, "e1", report=False)
        self._packet(relay, "e1", 1, report="Did it.\nStatus: clean\n")
        relay.append_ledger("report_verify", session_id="e1", packet=1, verdict="MISMATCH")
        relay.append_ledger("report_verify", session_id="e1", packet=1, verdict="COUNTS-MATCH")
        run_main(relay, "stats", "--json")
        assert json.loads(capsys.readouterr().out)["rows"][0]["verdict"] == "COUNTS-MATCH"

    def test_rounds_counts_packets_up_to_the_landing_boundary(self, relay, terms, capsys):
        """"counting packets sent to the same session strictly after this one, up to the lead's
        next real commit boundary — the earlier of a real `auto_commit` ledger event … or a
        `landed` event"."""
        make_session(relay, "e1", current_packet=3, report=False)
        for n in (1, 2, 3):
            self._packet(relay, "e1", n, report="Did it.\nStatus: clean\n")
        relay.append_ledger("packet_sent", session_id="e1", packet=1)
        time.sleep(1.05)
        relay.append_ledger("packet_sent", session_id="e1", packet=2)
        relay.append_ledger("packet_sent", session_id="e1", packet=3)
        time.sleep(1.05)
        relay.append_ledger("landed", session_id="e1", packet=1)
        run_main(relay, "stats", "--json")
        rows = {r["packet"]: r for r in json.loads(capsys.readouterr().out)["rows"]}
        assert rows["001"]["rounds"] == 2

    def test_rounds_is_a_dash_when_no_boundary_follows(self, relay, terms, capsys):
        """"Returns None — render '-', never a fabricated number — when … neither event follows"."""
        make_session(relay, "e1", report=False)
        self._packet(relay, "e1", 1, report="Did it.\nStatus: clean\n")
        relay.append_ledger("packet_sent", session_id="e1", packet=1)
        run_main(relay, "stats", "--json")
        assert json.loads(capsys.readouterr().out)["rows"][0]["rounds"] == "-"

    def test_an_auto_commit_blocked_event_is_not_a_boundary(self, relay, terms, capsys):
        """"a real `auto_commit` (a CLEARED auto-commit — never `auto_commit_blocked`, which is not
        a commit)"."""
        make_session(relay, "e1", report=False)
        self._packet(relay, "e1", 1, report="Did it.\nStatus: clean\n")
        relay.append_ledger("packet_sent", session_id="e1", packet=1)
        time.sleep(1.05)
        relay.append_ledger("auto_commit_blocked", session_id="e1", packet=1)
        run_main(relay, "stats", "--json")
        assert json.loads(capsys.readouterr().out)["rows"][0]["rounds"] == "-"

    def test_only_exactly_clean_counts_toward_pct_clean(self, relay, terms, capsys):
        """"% exactly `Status: clean` (`clean-with-caveats` does not count)"."""
        make_session(relay, "e1", current_packet=2, model="claude-sonnet-5", report=False)
        self._packet(relay, "e1", 1, report="Did it.\nStatus: clean\n")
        self._packet(relay, "e1", 2, report="Did it.\nStatus: clean-with-caveats\n")
        run_main(relay, "stats", "--json")
        summary = json.loads(capsys.readouterr().out)["summary"]
        assert len(summary) == 1 and summary[0]["packets"] == 2 and summary[0]["pct_clean"] == 50.0

    def test_the_summary_groups_by_model_and_effort(self, relay, terms, capsys):
        make_session(relay, "a", model="claude-haiku-5", effort="low", report=False)
        make_session(relay, "b", model="claude-sonnet-5", effort="high", report=False)
        for sid in ("a", "b"):
            self._packet(relay, sid, 1, report="Did it.\nStatus: clean\n")
        run_main(relay, "stats", "--json")
        summary = json.loads(capsys.readouterr().out)["summary"]
        assert {(g["model"], g["effort"]) for g in summary} == {("haiku", "low"), ("sonnet", "high")}

    def test_a_session_with_no_packets_is_skipped(self, relay, terms, capsys):
        make_session(relay, "e1", report=False)
        (relay.packets_dir("e1") / "001-packet.md").unlink()
        run_main(relay, "stats", "--json")
        data = json.loads(capsys.readouterr().out)
        assert data["rows"] == [] and data["sessions"] == []

    def test_a_packet_with_no_report_shows_a_dash_status(self, relay, terms, capsys):
        make_session(relay, "e1", report=False)
        self._packet(relay, "e1", 1)
        run_main(relay, "stats", "--json")
        assert json.loads(capsys.readouterr().out)["rows"][0]["status"] == "-"

    def test_a_malformed_report_degrades_to_a_dash_status(self, relay, terms, capsys):
        """_report_tldr: "Missing fields come back None — a malformed report degrades to a thinner
        seed entry, never an error"."""
        make_session(relay, "e1", report=False)
        self._packet(relay, "e1", 1, report="\x00\x01 not a report at all, no TL;DR anywhere\n")
        run_main(relay, "stats", "--json")
        assert json.loads(capsys.readouterr().out)["rows"][0]["status"] == "-"

    def test_since_filters_on_the_send_timestamp(self, relay, terms, capsys):
        make_session(relay, "old", report=False)
        self._packet(relay, "old", 1, report="Did it.\nStatus: clean\n")
        relay.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(relay.LEDGER, "a") as f:
            f.write(json.dumps({"ts": "2020-01-01T00:00:00", "event": "packet_sent",
                                "session_id": "old", "packet": 1}) + "\n")
        run_main(relay, "stats", "--since", "7", "--json")
        assert json.loads(capsys.readouterr().out)["rows"] == []

    def test_an_unknown_send_time_is_never_filtered_out(self, relay, terms, capsys):
        """"a packet with no recorded send time is never filtered out"."""
        make_session(relay, "e1", report=False)
        self._packet(relay, "e1", 1, report="Did it.\nStatus: clean\n")
        run_main(relay, "stats", "--since", "1", "--json")
        assert len(json.loads(capsys.readouterr().out)["rows"]) == 1

    def test_lead_scoping_keeps_unowned_executors(self, relay, terms, capsys):
        """"`--lead <sid>` scopes to that lead's executors plus unowned ones"."""
        arm_lead(relay, "lead-1", "webapp")
        make_session(relay, "mine", owner_lead="lead-1", report=False)
        make_session(relay, "unowned", owner_lead=None, report=False)
        make_session(relay, "theirs", owner_lead="lead-2", report=False)
        for sid in ("mine", "unowned", "theirs"):
            self._packet(relay, sid, 1, report="Did it.\nStatus: clean\n")
        run_main(relay, "stats", "--lead", "lead-1", "--json")
        assert {r["session_id"] for r in json.loads(capsys.readouterr().out)["rows"]} == \
            {"mine", "unowned"}

    def test_stats_writes_nothing(self, relay, terms, capsys):
        """"It reads ONLY what's already on disk … it writes nothing"."""
        make_session(relay, "e1", report=False)
        self._packet(relay, "e1", 1, report="Did it.\nStatus: clean\n")
        before = relay.read_session("e1")
        relay.append_ledger("packet_sent", session_id="e1", packet=1)
        ledger_before = relay.LEDGER.read_text()
        run_main(relay, "stats", "--json")
        assert relay.read_session("e1") == before
        assert relay.LEDGER.read_text() == ledger_before

    def test_the_table_renders_a_summary_and_a_token_trailer(self, relay, terms, capsys):
        make_session(relay, "e1", model="claude-sonnet-5", effort="high", report=False)
        self._packet(relay, "e1", 1, report="Did it.\nStatus: clean\n")
        run_main(relay, "stats")
        out = capsys.readouterr().out
        assert "SESSION" in out and "VERDICT" in out
        assert "SUMMARY (by model, effort)" in out
        assert "e1: 1 packet(s), tokens - , tok/pkt (avg) -" in out

    def test_an_empty_state_says_so(self, relay, terms, capsys):
        run_main(relay, "stats")
        assert "(no packets recorded)" in capsys.readouterr().out

    @pytest.mark.xfail(strict=True, reason="BUG-cli-5: `stats --lead` is not routed through "
                                           "resolve_sid, so a lead's project name silently scopes "
                                           "to nothing instead of to that lead")
    def test_lead_scoping_accepts_a_project_name(self, relay, terms, capsys):
        """README, right under the command table (which lists `relay stats [--lead SID]`):
        "Anywhere a command above takes a session id, you can pass the executor's name …, a lead's
        project name, or a unique prefix of either's id." `list --lead` and `board --lead` ARE in
        main()'s RESOLVE_FIELDS; `stats --lead` — the same kind of value, documented with the same
        sentence — is not, and an unresolved name matches no owner_lead, so the command prints an
        EMPTY table rather than failing: a silent wrong answer."""
        arm_lead(relay, "11111111-2222-3333-4444-555555555555", "webapp")
        make_session(relay, "mine", owner_lead="11111111-2222-3333-4444-555555555555", report=False)
        self._packet(relay, "mine", 1, report="Did it.\nStatus: clean\n")
        run_main(relay, "stats", "--lead", "webapp", "--json")
        assert [r["session_id"] for r in json.loads(capsys.readouterr().out)["rows"]] == ["mine"]


# ── relay doctor ────────────────────────────────────────────────────────────────────────────────

class TestDoctor:
    """README Troubleshooting: doctor proves "the installed Claude Code still behaves the way
    relay's launch flags assume". Its live probes are stubbed here — the unit suite must never run
    the real binary."""

    @pytest.fixture
    def probes(self, relay, monkeypatch):
        """Stub every outward call cmd_doctor makes: the `claude --version` subprocess and the
        four `_probe_claude` probes."""
        calls = []

        def fake_run(argv, *a, **kw):
            calls.append(argv)
            # `git log --oneline` in the commit-deny probe must come back EMPTY (no commit was
            # created) — that is what "denied" looks like.
            out = "" if argv and argv[0] == "git" else "1.2.3 (Claude Code)"
            return subprocess.CompletedProcess(argv, 0, out, "")
        monkeypatch.setattr(relay.subprocess, "run", fake_run)
        state = {"probe": lambda prompt, extra=(), **kw: ({"mcp_servers": [], "tools": ["Bash"],
                                                           "model": "claude-sonnet-5"},
                                                          "GATES=YES\nHARNESS=YES", "")}
        monkeypatch.setattr(relay, "_probe_claude",
                            lambda prompt, extra_flags=(), **kw: state["probe"](prompt, extra_flags,
                                                                               **kw))
        return SimpleNamespace(calls=calls, state=state)

    def test_offline_runs_only_the_plumbing_checks(self, relay, terms, probes, capsys):
        """`--offline`: "plumbing checks only, no claude calls"."""
        run_main(relay, "doctor", "--offline", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["strict MCP → zero servers"]["status"] == "SKIP"
        assert checks["executor agent applies"]["status"] == "SKIP"
        assert checks["model aliases (sonnet, sonnet[1m])"]["status"] == "SKIP"
        assert checks["state root writable"]["status"] == "PASS"
        assert checks["config loads"]["status"] == "PASS"

    def test_the_plumbing_checks_see_this_plugin_checkout(self, relay, terms, probes, capsys):
        run_main(relay, "doctor", "--offline", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["executor agent file"]["status"] == "PASS"
        assert checks["plugin hooks present"]["status"] == "PASS"
        assert relay.lead_guard.EXECUTOR_AGENT_FILE in checks["executor agent file"]["detail"]

    def test_an_unwritable_state_root_fails_and_exits_1(self, relay, terms, probes, monkeypatch):
        monkeypatch.setattr(relay.Path, "write_text",
                            lambda self, *a, **k: (_ for _ in ()).throw(OSError("read-only")))
        with pytest.raises(SystemExit) as e:
            run_main(relay, "doctor", "--offline", "--json")
        assert e.value.code == 1

    def test_strict_mcp_passes_when_the_probe_loads_zero_servers(self, relay, terms, probes, capsys):
        run_main(relay, "doctor", "--quick", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["strict MCP → zero servers"]["status"] == "PASS"

    def test_strict_mcp_fails_when_a_server_still_loads(self, relay, terms, probes, capsys):
        probes.state["probe"] = lambda p, extra=(), **kw: (
            {"mcp_servers": [{"name": "linear"}], "tools": ["Bash"]}, "GATES=YES\nHARNESS=YES", "")
        with pytest.raises(SystemExit):
            run_main(relay, "doctor", "--quick", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["strict MCP → zero servers"]["status"] == "FAIL"
        assert "linear" in checks["strict MCP → zero servers"]["detail"]

    def test_the_agent_check_fails_when_the_agent_tool_is_still_present(self, relay, terms, probes,
                                                                        capsys):
        """#31: the executor agent removes the `Agent` tool; doctor is what notices a CLI update
        that stopped honouring that."""
        probes.state["probe"] = lambda p, extra=(), **kw: (
            {"mcp_servers": [], "tools": ["Bash", "Agent"]}, "GATES=YES\nHARNESS=YES", "")
        with pytest.raises(SystemExit):
            run_main(relay, "doctor", "--quick", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["executor agent applies"]["status"] == "FAIL"
        assert "Agent=PRESENT" in checks["executor agent applies"]["detail"]

    def test_the_agent_check_fails_when_the_gates_prompt_is_missing(self, relay, terms, probes,
                                                                    capsys):
        probes.state["probe"] = lambda p, extra=(), **kw: (
            {"mcp_servers": [], "tools": ["Bash"]}, "GATES=NO\nHARNESS=YES", "")
        with pytest.raises(SystemExit):
            run_main(relay, "doctor", "--quick", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["executor agent applies"]["status"] == "FAIL"

    def test_a_probe_that_returns_no_init_event_fails_with_its_error(self, relay, terms, probes,
                                                                     capsys):
        probes.state["probe"] = lambda p, extra=(), **kw: (None, "", "connection refused")
        with pytest.raises(SystemExit):
            run_main(relay, "doctor", "--quick", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["strict MCP → zero servers"]["status"] == "FAIL"
        assert "no init event connection refused" in checks["strict MCP → zero servers"]["detail"]

    def test_quick_skips_the_slow_probes(self, relay, terms, probes, capsys):
        """`--quick`: "skip the slower probes (commit deny, [1m] alias)"."""
        run_main(relay, "doctor", "--quick", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["git commit denied under skip-perms"]["status"] == "SKIP"
        assert checks["model aliases (sonnet, sonnet[1m])"]["status"] == "SKIP"

    def test_the_model_alias_check_wants_a_1m_flavour_back(self, relay, terms, probes, capsys):
        seen = []

        def probe(prompt, extra=(), **kw):
            seen.append(kw.get("model"))
            model = "claude-sonnet-5[1m]" if kw.get("model") == "sonnet[1m]" else "claude-sonnet-5"
            return {"mcp_servers": [], "tools": ["Bash"], "model": model}, "GATES=YES\nHARNESS=YES", ""
        probes.state["probe"] = probe
        run_main(relay, "doctor", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["model aliases (sonnet, sonnet[1m])"]["status"] == "PASS"
        assert "sonnet[1m]" in seen

    def test_the_model_alias_check_fails_when_1m_does_not_take(self, relay, terms, probes, capsys):
        """README Config: "`[1m]` always rides a full id" — a CLI that drops the suffix silently
        downgrades every 1M executor, which is exactly what this check exists to catch."""
        probes.state["probe"] = lambda p, extra=(), **kw: (
            {"mcp_servers": [], "tools": ["Bash"], "model": "claude-sonnet-5"},
            "GATES=YES\nHARNESS=YES", "")
        with pytest.raises(SystemExit):
            run_main(relay, "doctor", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["model aliases (sonnet, sonnet[1m])"]["status"] == "FAIL"

    def test_the_model_alias_cache_is_reported(self, relay, terms, probes, capsys):
        """README Config: the alias resolution is "cached per CLI version in
        `~/.relay-tasks/models.json`, shown in `relay doctor`"."""
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "models.json").write_text(json.dumps({"test": {"sonnet": "claude-sonnet-5"}}))
        run_main(relay, "doctor", "--offline", "--json")
        checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)}
        assert checks["model alias cache (this CLI version)"]["detail"] == "sonnet→claude-sonnet-5"

    def test_all_passing_exits_0_and_says_so(self, relay, terms, probes, capsys):
        run_main(relay, "doctor", "--offline")
        assert "relay doctor: all checks passed" in capsys.readouterr().out

    def test_a_failure_is_listed_by_name_in_the_footer(self, relay, terms, probes, capsys,
                                                       monkeypatch):
        probes.state["probe"] = lambda p, extra=(), **kw: (
            {"mcp_servers": [{"name": "linear"}], "tools": ["Bash"]}, "GATES=YES\nHARNESS=YES", "")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "doctor", "--quick")
        assert e.value.code == 1
        assert "✗ 1 failing: strict MCP → zero servers" in capsys.readouterr().out


# ── relay board ─────────────────────────────────────────────────────────────────────────────────

class TestBoard:
    """README "The board": "one HTML page for everything: leads → executors → packet timelines
    (gist, outcome, TL;DR), status, launch, tokens, warnings, copyable commands"."""

    def test_json_emits_the_board_data(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp")
        make_session(relay, "e1", owner_lead="lead-1")
        run_main(relay, "board", "--json")
        data = json.loads(capsys.readouterr().out)
        assert {"leads", "executors"} <= set(data)
        assert [e["session_id"] for e in data["executors"]] == ["e1"]
        assert [m["session_id"] for m in data["leads"]] == ["lead-1"]

    def test_the_packet_timeline_carries_the_gist_and_the_tldr(self, relay, terms, capsys):
        make_session(relay, "e1", report=(
            "Split the layout behind a flag; 9 new tests, suite green, staged.\n"
            "Status: clean\nRisk flags: none\nUNVERIFIED: none\n"))
        (relay.packets_dir("e1") / "001-packet.md").write_text("GOAL — split the layout.\n")
        run_main(relay, "board", "--json")
        pkt = json.loads(capsys.readouterr().out)["executors"][0]["packets"][0]
        assert pkt["gist"] == "GOAL — split the layout."
        assert pkt["tldr"]["status"] == "clean"
        assert pkt["tldr"]["outcome"].startswith("Split the layout behind a flag")

    def test_out_writes_a_self_contained_page(self, relay, terms, tmp_path, capsys):
        make_session(relay, "e1")
        out = tmp_path / "board.html"
        run_main(relay, "board", "--out", str(out))
        html = out.read_text()
        assert html.lstrip().lower().startswith("<!doctype html")
        assert "e1" in html
        assert "http://" not in html and "https://" not in html.split("<style")[0]

    def test_it_defaults_to_the_state_root(self, relay, terms, capsys):
        make_session(relay, "e1")
        run_main(relay, "board")
        assert (relay.STATE_ROOT / "board.html").is_file()

    def test_lead_scoping_keeps_unowned_executors(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp")
        make_session(relay, "mine", owner_lead="lead-1")
        make_session(relay, "unowned", owner_lead=None)
        make_session(relay, "theirs", owner_lead="lead-2")
        run_main(relay, "board", "--lead", "lead-1", "--json")
        data = json.loads(capsys.readouterr().out)
        assert {e["session_id"] for e in data["executors"]} == {"mine", "unowned"}

    def test_an_empty_state_still_renders(self, relay, terms, capsys):
        run_main(relay, "board", "--json")
        data = json.loads(capsys.readouterr().out)
        assert data["executors"] == [] and data["leads"] == []


# ── ledger integrity ────────────────────────────────────────────────────────────────────────────

class TestLedgerIntegrity:
    """append_ledger is the one durable history relay keeps: "the sessions.jsonl ledger (the
    durable history)" (cmd_prune), read back by `_ledger_by_session`, which is "Read-only — never
    used to derive a WRITE"."""

    def test_every_mutation_appends_one_well_formed_line(self, relay, terms, tmp_path, live_pid):
        wt = tmp_path / "wt"
        wt.mkdir()
        run_main(relay, "spawn", str(wt), "topic", write_packet(tmp_path), "--name", "e1")
        mark_reported(relay, "e1")
        run_main(relay, "send", "e1", write_packet(tmp_path))
        mark_reported(relay, "e1")
        run_main(relay, "keep", "e1")
        run_main(relay, "retire", "e1")
        for line in relay.LEDGER.read_text().splitlines():
            rec = json.loads(line)                       # every line parses on its own
            assert set(rec) >= {"ts", "event"}
            assert relay._ts_epoch(rec["ts"]) is not None
        events = [json.loads(l)["event"] for l in relay.LEDGER.read_text().splitlines()]
        assert events[:3] == ["spawned", "packet_sent", "packet_sent"]
        assert "keep" in events and "retired" in events

    def test_concurrent_writers_never_interleave_a_line(self, relay):
        """append_ledger writes one `json.dumps(rec) + "\\n"` per open("a") — the property every
        reader depends on when two triggers (a lead's `check` and an executor's Stop hook) write
        in the same second."""
        import threading
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        payload = "x" * 400

        def writer(tag):
            for i in range(150):
                relay.append_ledger("stress", session_id=tag, i=i, payload=payload)

        threads = [threading.Thread(target=writer, args=(f"s{n}",)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = relay.LEDGER.read_text().splitlines()
        assert len(lines) == 900
        for line in lines:
            rec = json.loads(line)                        # no line is a splice of two records
            assert rec["payload"] == payload
        by_sid = relay._ledger_by_session()
        assert sorted(by_sid) == [f"s{n}" for n in range(6)]
        assert all(len(v) == 150 for v in by_sid.values())

    def test_a_truncated_last_line_is_tolerated(self, relay):
        """A crash mid-append leaves a half-written last line; every earlier record must still be
        readable — the ledger is the durable history, not a best-effort log."""
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        relay.append_ledger("spawned", session_id="e1")
        relay.append_ledger("packet_sent", session_id="e1", packet=1)
        with open(relay.LEDGER, "a") as f:
            f.write('{"ts": "2026-01-01T00:00:00", "event": "packet_sen')
        by_sid = relay._ledger_by_session()
        assert [e["event"] for e in by_sid["e1"]] == ["spawned", "packet_sent"]

    def test_blank_and_unparsable_lines_are_skipped(self, relay):
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        relay.append_ledger("spawned", session_id="e1")
        with open(relay.LEDGER, "a") as f:
            f.write("\n\n   \nnot json at all\n")
        relay.append_ledger("closed", session_id="e1")
        assert [e["event"] for e in relay._ledger_by_session()["e1"]] == ["spawned", "closed"]

    def test_records_without_a_session_id_are_grouped_out(self, relay):
        """`lead_handoff` carries from_lead/to_lead but no session_id — it must not land under a
        session key of its own."""
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        relay.append_ledger("lead_handoff", from_lead="a", to_lead="b")
        assert relay._ledger_by_session() == {}

    def test_a_missing_ledger_reads_as_empty(self, relay):
        assert relay._ledger_by_session() == {}

    def test_ledger_order_is_file_order(self, relay):
        """"each session's list kept in file (== chronological) order"."""
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        for n in range(5):
            relay.append_ledger("packet_sent", session_id="e1", packet=n)
        assert [e["packet"] for e in relay._ledger_by_session()["e1"]] == [0, 1, 2, 3, 4]


# ── corrupt state files ─────────────────────────────────────────────────────────────────────────

class TestCorruptSessionJson:
    """`_busy_elapsed`'s docstring states the standing contract for hand-edited state:
    "Never throws — one hand-edited or legacy session.json must not take down `relay list`/`check
    --all`." `read_queue` states the same for its own file: "a hand-edited queue.json must never
    take down `relay list`/`check`." One unreadable session.json is exactly that case."""

    @pytest.mark.parametrize("argv", [
        ("list",), ("check", "--all"), ("prune", "--dry-run"), ("stats", "--json"),
        ("board", "--json"), ("whoami", "some-unknown-uuid"),
    ], ids=["list", "check-all", "prune", "stats", "board", "whoami"])
    @pytest.mark.xfail(strict=True, reason="BUG-cli-1: one unparsable session.json raises "
                                           "JSONDecodeError out of read_session and takes down "
                                           "every multi-session command")
    def test_one_broken_session_does_not_take_the_command_down(self, relay, terms, argv):
        make_session(relay, "good")
        corrupt_session(relay)
        try:
            run_main(relay, *argv)
        except SystemExit:
            pass          # a clean refusal is fine; an unhandled traceback is not

    def test_an_empty_session_json_is_the_same_hazard(self, relay, terms):
        """A half-written file (the O_TRUNC window of a non-atomic write) reads as "" — the same
        JSONDecodeError, from the same line."""
        corrupt_session(relay, "half-written", text="")
        with pytest.raises(json.JSONDecodeError):
            relay.read_session("half-written")

    def test_a_corrupt_queue_file_really_does_read_as_empty(self, relay, terms):
        """The contract read_queue DOES honour — quoted above — for contrast with session.json."""
        make_session(relay, "e1")
        relay.queue_path("e1").write_text("{ not json")
        assert relay.read_queue("e1") == []

    def test_a_corrupt_config_falls_back_to_defaults(self, relay, terms):
        """lead_guard.load_config: "missing/corrupt file → pure defaults. Never throws"."""
        p = relay.lead_guard.config_path(relay.STATE_ROOT)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not json")
        assert relay.lead_guard.load_config(relay.STATE_ROOT)["grace_seconds"] == 120

    def test_a_corrupt_marker_reads_as_no_lead(self, relay, terms):
        d = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1")
        d.mkdir(parents=True, exist_ok=True)
        relay.lead_guard.marker_path(relay.STATE_ROOT, "lead-1").write_text("{ not json")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1") == {}
