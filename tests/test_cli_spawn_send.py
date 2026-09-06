"""
Bug-hunt unit tests for `relay spawn` and `relay send` (packet bh-cli). The `--when-idle`
queue half lives in tests/test_cli_queue.py; close/retire/prune in
tests/test_cli_close_retire_prune.py.

Every assertion here is anchored to a documented contract — README.md, skills/*/SKILL.md,
docs/post-0.3.27-backlog.md, or the function's own docstring — named in each test's docstring.
Where the code contradicts the contract the test asserts the CONTRACT and carries an
`xfail(strict=True)` naming the finding in tests/bughunt/cli-findings.md.

Nothing here touches a real terminal, a real `claude`, the network, or the real home directory:
the `terms` fixture stubs every backend entry point on BOTH backend modules (see FakeTerm's
docstring for why patching `relay.iterm` alone is not enough).

Run: pytest tests/test_cli_spawn_send.py -q
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


def cfg_write(relay, **kv):
    p = relay.lead_guard.config_path(relay.STATE_ROOT)
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(p.read_text()) if p.exists() else {}
    cur.update(kv)
    p.write_text(json.dumps(cur))


# ── spawn: model resolution ─────────────────────────────────────────────────────────────────────

class TestSpawnModelResolution:
    """README Config: `executor_default_model` is "Model an executor launches with when `--model`
    is omitted — relay's own policy, never the CLI's personal `/model` default"."""

    def test_explicit_model_beats_config_default(self, relay, terms, tmp_path):
        cfg_write(relay, executor_default_model="haiku", executor_default_context="200k")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path),
                 "--name", "e1", "--model", "sonnet")
        assert relay.read_session("e1")["model"] == "sonnet"
        assert terms.spawns[0]["model"] == "sonnet"

    def test_config_default_applies_when_model_omitted(self, relay, terms, tmp_path):
        cfg_write(relay, executor_default_model="haiku")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        # haiku has no 1M window (lead_guard NO_1M_TIERS), so no `[1m]` suffix can ride it.
        assert relay.read_session("e1")["model"] == "haiku"

    def test_effective_model_is_stored_never_null(self, relay, terms, tmp_path):
        """The 2026-07-12 executor-model-leak incident (lead_guard "executor model policy"):
        storing null let `relay list` render `-` for a live executor. cmd_spawn's own comment:
        "never store null just because the caller didn't pass one"."""
        cfg_write(relay, executor_default_context="200k")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        assert relay.read_session("e1")["model"] == "sonnet"

    def test_model_above_ceiling_is_refused_without_override(self, relay, terms, tmp_path):
        """cmd_spawn: "A requested model above executor_model_ceiling is refused unless
        --model-override "<reason>" is given"."""
        cfg_write(relay, executor_model_ceiling="sonnet")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path),
                     "--name", "e1", "--model", "opus")
        assert "above the configured ceiling" in str(e.value)
        assert "--model-override" in str(e.value)

    def test_ceiling_refusal_writes_no_session_record(self, relay, terms, tmp_path):
        """"Resolve the model BEFORE any packet/session files are written" — a refused spawn must
        leave nothing behind to collide with the next --name."""
        cfg_write(relay, executor_model_ceiling="sonnet")
        with pytest.raises(SystemExit):
            run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path),
                     "--name", "e1", "--model", "opus")
        assert relay.read_session("e1") is None
        assert terms.spawns == []

    def test_model_override_ledgers_the_reason(self, relay, terms, tmp_path):
        """`--model-override REASON`: "the reason is recorded in the ledger
        (model_ceiling_override)" (main()'s own --model-override help text)."""
        cfg_write(relay, executor_model_ceiling="sonnet", executor_default_context="200k")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1",
                 "--model", "opus", "--model-override", "unknown-root-cause debugging")
        recs = ledger_events(relay, "model_ceiling_override")
        assert len(recs) == 1
        assert recs[0]["session_id"] == "e1"
        assert recs[0]["model"] == "opus"
        assert recs[0]["reason"] == "unknown-root-cause debugging"
        assert relay.read_session("e1")["status"] == "busy"

    def test_unknown_model_is_refused_before_any_tab_opens(self, relay, terms, tmp_path):
        """README Config: "an unrecognised model is refused before any tab opens"."""
        relay._probe_model = lambda alias: (None, "unrecognized_model")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path),
                     "--name", "e1", "--model", "sonnet")
        assert "not recognised" in str(e.value)
        assert terms.spawns == []
        assert relay.read_session("e1") is None

    def test_alias_is_launched_as_the_concrete_id(self, relay, terms, tmp_path):
        """README Config: an alias "is **resolved through this machine's Claude Code at spawn** and
        the executor is launched with the concrete id"."""
        cfg_write(relay, executor_default_context="200k")
        relay._probe_model = lambda alias: ("claude-sonnet-5", None)
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path),
                 "--name", "e1", "--model", "sonnet")
        assert terms.spawns[0]["model"] == "claude-sonnet-5"
        assert relay.read_session("e1")["model"] == "claude-sonnet-5"


# ── spawn: context window ───────────────────────────────────────────────────────────────────────

class TestSpawnContextWindow:
    """README "Executor context window" / lead_guard's CONTEXT_RE block: explicit `[1m]` wins, then
    the packet's `CONTEXT:` line, then the referenced-reading heuristic, then the config default."""

    def test_explicit_1m_suffix_rides_the_resolved_id(self, relay, terms, tmp_path):
        relay._probe_model = lambda alias: ("claude-sonnet-5", None)
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path),
                 "--name", "e1", "--model", "sonnet[1m]")
        assert terms.spawns[0]["model"] == "claude-sonnet-5[1m]"
        assert relay.read_session("e1")["context"] == "1m"

    def test_packet_context_200k_opts_down_from_the_config_default(self, relay, terms, tmp_path):
        cfg_write(relay, executor_default_context="1m")
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nCONTEXT: 200k\n\n## Preconditions\n- ok\n")
        run_main(relay, "spawn", str(tmp_path), "t", pkt, "--name", "e1", "--model", "sonnet")
        assert relay.read_session("e1")["context"] == "200k"
        assert "[1m]" not in terms.spawns[0]["model"]

    def test_haiku_never_gets_the_1m_window(self, relay, terms, tmp_path):
        """lead_guard: NO_1M_TIERS = ("haiku",) — "haiku always runs 200K" (spawn SKILL.md)."""
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nCONTEXT: 1m\n\n## Preconditions\n- ok\n")
        run_main(relay, "spawn", str(tmp_path), "t", pkt, "--name", "e1", "--model", "haiku")
        assert relay.read_session("e1")["context"] == "200k"

    def test_context_line_is_read_from_the_authored_body_not_the_seed(self, relay, terms, tmp_path):
        """cmd_spawn: `authored_body` is "the lead's own text — the packet `MCP:` line is read from
        THIS, never from appended context (a successor seed's territory block, say)". The same
        must hold for CONTEXT:, which is decided from `authored_body` on the very next lines."""
        seed = tmp_path / "seed.md"
        seed.write_text("# Successor seed\n\nCONTEXT: 1m\n")
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nCONTEXT: 200k\n\n## Preconditions\n- ok\n")
        run_main(relay, "spawn", str(tmp_path), "t", pkt, "--name", "e1", "--model", "sonnet",
                 "--seed", str(seed))
        assert relay.read_session("e1")["context"] == "200k"


# ── spawn: MCP ──────────────────────────────────────────────────────────────────────────────────

class TestSpawnMcpForms:
    """spawn SKILL.md: "Executors launch with NO MCP servers by default"; the packet's `MCP:` line
    is the source of truth and "`--mcp SPEC` on the command line overrides the line"."""

    def test_default_is_strict_zero_servers(self, relay, terms, tmp_path):
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        assert relay.read_session("e1")["mcp"] == "none"
        assert "--strict-mcp-config" in " ".join(terms.spawns[0]["mcp_flags"])

    def test_bare_mcp_flag_means_inherit(self, relay, terms, tmp_path):
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1", "--mcp")
        assert relay.read_session("e1")["mcp"] == "inherit"
        assert terms.spawns[0]["mcp_flags"] == []

    def test_packet_mcp_line_is_used_when_no_flag(self, relay, terms, tmp_path):
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nMCP: inherit\n\n## Preconditions\n- ok\n")
        run_main(relay, "spawn", str(tmp_path), "t", pkt, "--name", "e1")
        assert relay.read_session("e1")["mcp"] == "inherit"

    def test_flag_overrides_the_packet_line_and_says_so(self, relay, terms, tmp_path, capsys):
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nMCP: inherit\n\n## Preconditions\n- ok\n")
        run_main(relay, "spawn", str(tmp_path), "t", pkt, "--name", "e1", "--mcp", "none")
        assert relay.read_session("e1")["mcp"] == "none"
        assert "overrides it" in capsys.readouterr().out

    def test_unknown_allowlist_name_refuses_before_launching(self, relay, terms, tmp_path):
        """cmd_spawn: "Resolved BEFORE files are written so an unknown allowlist name refuses the
        spawn cleanly instead of launching an executor missing its tool"."""
        with pytest.raises(SystemExit) as e:
            run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path),
                     "--name", "e1", "--mcp", "definitely-not-a-server")
        assert "spawn: MCP" in str(e.value)
        assert terms.spawns == []
        assert relay.read_session("e1") is None


# ── spawn: effort ───────────────────────────────────────────────────────────────────────────────

class TestSpawnEffort:
    """README "Executor effort" / lead_guard EFFORT_LEVELS: flag > packet `EFFORT:` line > unset;
    "Invalid flag values are refused here; the CLI would only warn-and-ignore" (cmd_spawn)."""

    def test_flag_beats_packet_line(self, relay, terms, tmp_path):
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nEFFORT: low\n\n## Preconditions\n- ok\n")
        run_main(relay, "spawn", str(tmp_path), "t", pkt, "--name", "e1", "--effort", "max")
        assert relay.read_session("e1")["effort"] == "max"
        assert terms.spawns[0]["effort"] == "max"

    def test_packet_line_applies_when_no_flag(self, relay, terms, tmp_path):
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nEFFORT: high\n\n## Preconditions\n- ok\n")
        run_main(relay, "spawn", str(tmp_path), "t", pkt, "--name", "e1")
        assert relay.read_session("e1")["effort"] == "high"

    def test_unset_resolves_to_the_config_default(self, relay, terms, tmp_path):
        """README "Executor effort": "Executors run at `executor_default_effort` (`high`, the CLI
        default) ... It is always explicit: a new executor's `effort` is never unset"."""
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        assert relay.read_session("e1")["effort"] == "high"
        assert terms.spawns[0]["effort"] == "high"

    def test_invalid_flag_value_is_refused_with_the_valid_list(self, relay, terms, tmp_path):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path),
                     "--name", "e1", "--effort", "turbo")
        assert "valid: low, medium, high, xhigh, max" in str(e.value)
        assert terms.spawns == []

    def test_unparsable_packet_effort_line_degrades_to_the_config_default(self, relay, terms, tmp_path):
        """lead_guard.normalize_effort_spec returns None for an unknown level; lint warns
        (`effort-unparsable`) but the spawn proceeds on `executor_default_effort` ("high") — README
        "Executor effort": "a new executor's `effort` is never unset"."""
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nEFFORT: turbo\n\n## Preconditions\n- ok\n")
        run_main(relay, "spawn", str(tmp_path), "t", pkt, "--name", "e1")
        assert relay.read_session("e1")["effort"] == "high"


# ── spawn: seed ─────────────────────────────────────────────────────────────────────────────────

class TestSpawnSeedResolution:
    """resolve_seed: "Accepts either a path to a seed file or … a RETIRED SESSION'S id … A path is
    tried first so an on-disk file always wins over a name collision"."""

    def test_seed_by_path(self, relay, terms, tmp_path):
        seed = tmp_path / "successor-seed.md"
        seed.write_text("# Successor seed — old\n\nterritory notes\n")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1",
                 "--seed", str(seed))
        body = (relay.packets_dir("e1") / "001-packet.md").read_text()
        assert "territory notes" in body
        assert str(seed.resolve()) in body

    def test_seed_by_retired_sid(self, relay, terms, tmp_path):
        make_session(relay, "old")
        (relay.session_dir("old") / relay.SEED_FILENAME).write_text("# Successor seed — old\n\nnotes\n")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1",
                 "--seed", "old")
        assert "notes" in (relay.packets_dir("e1") / "001-packet.md").read_text()

    def test_seed_lands_before_the_gates_footer(self, relay, terms, tmp_path):
        """build_seeded_body: "Runs BEFORE build_packet, so the GATES/REPORT FORMAT footer still
        lands last (the seed must never come between the executor and its gates)"."""
        seed = tmp_path / "s.md"
        seed.write_text("SEEDMARKER")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1",
                 "--seed", str(seed))
        body = (relay.packets_dir("e1") / "001-packet.md").read_text()
        assert body.index("SEEDMARKER") < body.index("(relay — do not remove or reword this section)")

    def test_unresolvable_seed_names_both_things_it_tried(self, relay, terms, tmp_path):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1",
                     "--seed", "nope-not-here")
        msg = str(e.value)
        assert "neither a readable file nor a retired session" in msg
        assert "relay retire" in msg


# ── spawn: ownership, keep, layout, refusals ────────────────────────────────────────────────────

class TestSpawnOwnershipKeepLayout:
    def test_lead_defaults_to_the_env_session_id(self, relay, terms, tmp_path, monkeypatch):
        """main()'s --lead help: "defaults to $CLAUDE_CODE_SESSION_ID, else unowned"."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-1")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        assert relay.read_session("e1")["owner_lead"] == "lead-1"

    def test_explicit_lead_flag_wins_over_the_env(self, relay, terms, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-env")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1",
                 "--lead", "lead-flag")
        assert relay.read_session("e1")["owner_lead"] == "lead-flag"

    def test_no_lead_anywhere_is_unowned(self, relay, terms, tmp_path):
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        assert relay.read_session("e1")["owner_lead"] is None

    def test_keep_pins_against_auto_close(self, relay, terms, tmp_path):
        """spawn SKILL.md: "Add `--keep` if you already know a follow-up packet is coming soon and
        don't want auto-close to park it in between"."""
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1", "--keep")
        assert relay.read_session("e1")["keep"] is True

    def test_no_keep_flag_leaves_it_unpinned(self, relay, terms, tmp_path):
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        assert relay.read_session("e1")["keep"] is False

    def test_pane_and_tab_flags_reach_the_backend(self, relay, terms, tmp_path):
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1", "--pane")
        assert terms.spawns[0]["layout"] == "pane"
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e2", "--tab")
        assert terms.spawns[1]["layout"] == "tab"

    def test_neither_flag_falls_through_to_config(self, relay, terms, tmp_path):
        cfg_write(relay, executor_layout="pane")
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        assert terms.spawns[0]["layout"] == "pane"

    def test_duplicate_name_is_refused_and_points_at_send(self, relay, terms, tmp_path):
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        assert "already exists" in str(e.value) and "relay send" in str(e.value)
        assert len(terms.spawns) == 1

    def test_duplicate_name_refusal_does_not_renumber_the_packet(self, relay, terms, tmp_path):
        run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        with pytest.raises(SystemExit):
            run_main(relay, "spawn", str(tmp_path), "t", write_packet(tmp_path), "--name", "e1")
        assert sorted(p.name for p in relay.packets_dir("e1").glob("*-packet.md")) == ["001-packet.md"]

    def test_missing_worktree_is_refused_with_the_resolved_path(self, relay, terms, tmp_path):
        """cmd_spawn: "Fail loudly here instead" — a bogus worktree would `cd` into nothing and
        the `&&` chain would die, leaving a silently broken tab."""
        with pytest.raises(SystemExit) as e:
            run_main(relay, "spawn", str(tmp_path / "nope"), "t", write_packet(tmp_path), "--name", "e1")
        assert "worktree not found" in str(e.value)
        assert terms.spawns == []

    def test_missing_packet_file_fails_before_any_state_is_written(self, relay, terms, tmp_path):
        with pytest.raises((SystemExit, OSError)):
            run_main(relay, "spawn", str(tmp_path), "t", str(tmp_path / "gone.md"), "--name", "e1")
        assert relay.read_session("e1") is None
        assert terms.spawns == []


# ── send: which statuses are valid targets ──────────────────────────────────────────────────────

class TestSendStatusGate:
    """send SKILL.md: "`relay` refuses only `busy`/`stalled` (mid-turn — injecting risks corrupting
    it) and `superseded` (abandoned) targets. A `reported`/idle session is sent to in place. A
    **`closed` or `dead`** session that still has its captured Claude conversation is
    **automatically resumed** and delivered the packet in one shot"."""

    def test_reported_session_is_sent_to_in_place(self, relay, terms, tmp_path, capsys):
        make_session(relay, "e1", status="reported")
        run_main(relay, "send", "e1", write_packet(tmp_path))
        assert len(terms.sends) == 1
        assert terms.spawns == []                       # in place: no relaunch
        assert relay.read_session("e1")["current_packet"] == 2
        assert "existing tab" in capsys.readouterr().out

    def test_busy_session_is_refused(self, relay, terms, tmp_path, live_pid):
        make_session(relay, "e1", status="busy", pid=live_pid, pid_started=None)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path))
        assert "refusing to send" in str(e.value) and "mid-turn" in str(e.value)
        assert terms.sends == []

    def test_superseded_session_is_refused_and_names_its_successor(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="superseded", superseded_by="e2")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path))
        assert "superseded by e2" in str(e.value)

    def test_launch_failed_session_is_refused_and_points_at_restart(self, relay, terms, tmp_path):
        """§12 #20/#21: LAUNCH_FAILED means "there is no tab to send to and no conversation to
        resume"; the recovery is `relay restart`."""
        make_session(relay, "e1", status=relay.LAUNCH_FAILED)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path))
        assert relay.LAUNCH_FAILED in str(e.value)
        assert "relay restart e1" in str(e.value)

    def test_closed_session_is_resumed_and_delivered_in_one_shot(self, relay, terms, tmp_path, capsys):
        make_session(relay, "e1", status="closed")
        run_main(relay, "send", "e1", write_packet(tmp_path))
        assert len(terms.spawns) == 1
        assert terms.spawns[0]["resume_id"] == "cs-e1"      # SAME conversation
        assert terms.spawns[0]["session_uuid"] is None      # not a fresh one
        out = capsys.readouterr().out
        assert "was closed — resumed with full context and delivered packet 002" in out
        assert relay.read_session("e1")["current_packet"] == 2

    def test_dead_session_with_no_conversation_needs_a_fresh_spawn(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="dead", claude_session=None)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path))
        assert "no captured Claude session to resume" in str(e.value)
        assert "relay spawn" in str(e.value)

    def test_missing_session_is_refused(self, relay, terms, tmp_path):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "nope", write_packet(tmp_path))
        assert "no such session: nope" in str(e.value)

    def test_stored_busy_is_recomputed_before_the_guard(self, relay, terms, tmp_path):
        """cmd_send (A): "s["status"] is the STORED value … a session that has already reported
        still reads `busy` on disk and would be falsely refused"."""
        make_session(relay, "e1", status="busy", pid=None)
        (relay.packets_dir("e1") / "001-report.md").write_text("Done.\nStatus: clean\n")
        run_main(relay, "send", "e1", write_packet(tmp_path))
        assert len(terms.sends) == 1


class TestSendPacketNumbering:
    def test_numbers_increment_without_gaps(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="reported")
        for expected in (2, 3, 4):
            run_main(relay, "send", "e1", write_packet(tmp_path))
            assert relay.read_session("e1")["current_packet"] == expected
            mark_reported(relay, "e1")
        assert sorted(p.name for p in relay.packets_dir("e1").glob("*-packet.md")) == [
            "001-packet.md", "002-packet.md", "003-packet.md", "004-packet.md"]

    def test_an_interior_hole_is_not_backfilled(self, relay, terms, tmp_path):
        """next_packet_number takes max+1 of what is on disk, so a hole left by a deleted MIDDLE
        packet stays a hole — backfilling it would re-point a fresh packet at an older packet's
        report path. (Deleting the HIGHEST packet does re-issue its number; see AMBIGUOUS-cli-1.)"""
        make_session(relay, "e1", status="reported", current_packet=3,
                     report=DEFAULT_REPORT)
        (relay.packets_dir("e1") / "002-packet.md").unlink()
        run_main(relay, "send", "e1", write_packet(tmp_path))
        assert (relay.packets_dir("e1") / "004-packet.md").exists()
        assert not (relay.packets_dir("e1") / "002-packet.md").exists()

    def test_each_packet_gets_its_own_report_and_diff_paths(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="reported")
        run_main(relay, "send", "e1", write_packet(tmp_path))
        body = (relay.packets_dir("e1") / "002-packet.md").read_text()
        assert str(relay.packets_dir("e1") / "002-report.md") in body
        assert "002-diff.html" in body


class TestSendAdoptOnClaim:
    """send SKILL.md: "Sending into an executor also **adopts** it … If it's currently owned by a
    live *other* lead, relay warns instead of stealing it"."""

    def test_send_adopts_an_executor_of_a_dead_lead(self, relay, terms, tmp_path, monkeypatch, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-new", project="p", cwd="/tmp")
        make_session(relay, "e1", status="reported", owner_lead="lead-old")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        run_main(relay, "send", "e1", write_packet(tmp_path))
        assert relay.read_session("e1")["owner_lead"] == "lead-new"
        assert relay.read_session("e1")["owner_project"] == "p"
        assert "adopted 'e1' from retired lead" in capsys.readouterr().out
        assert [e["to_lead"] for e in ledger_events(relay, "adopted")] == ["lead-new"]

    def test_send_warns_instead_of_stealing_from_a_live_lead(self, relay, terms, tmp_path,
                                                             monkeypatch, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-new", project="p", cwd="/tmp")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-live", project="q", cwd="/tmp")
        (relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-live") / "pid").write_text(str(os.getpid()))
        make_session(relay, "e1", status="reported", owner_lead="lead-live")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        run_main(relay, "send", "e1", write_packet(tmp_path))
        assert relay.read_session("e1")["owner_lead"] == "lead-live"
        assert "owned by live lead" in capsys.readouterr().err

    def test_a_non_lead_caller_never_adopts(self, relay, terms, tmp_path, monkeypatch):
        """_maybe_adopt: adoption "only exists for leads" — an unarmed caller must not re-parent."""
        make_session(relay, "e1", status="reported", owner_lead="lead-old")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "just-some-session")
        run_main(relay, "send", "e1", write_packet(tmp_path))
        assert relay.read_session("e1")["owner_lead"] == "lead-old"


class TestSendHeavinessAndUpgrade:
    """README/§6e-e2: the heaviness gate refuses a plain send past `context_nudge_tokens` unless
    `--heavy-override "<reason>"`, which is ledgered; `--rotate`/`--upgrade` are the escapes."""

    def _make_heavy(self, relay, monkeypatch, tokens=400_000):
        monkeypatch.setattr(relay, "_usage_for_session",
                            lambda s: {"last_prompt": tokens, "prompt": tokens, "output": 1000,
                                       "cache_hit_rate": None})
        monkeypatch.setattr(relay, "_transcript_mb_for", lambda cs: None)

    def test_heavy_send_is_refused_and_offers_rotate(self, relay, terms, tmp_path, monkeypatch):
        make_session(relay, "e1", status="reported")
        self._make_heavy(relay, monkeypatch)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path))
        msg = str(e.value)
        assert "is heavy" in msg and "--rotate" in msg and "--heavy-override" in msg
        assert terms.sends == []

    def test_heavy_override_ledgers_its_reason(self, relay, terms, tmp_path, monkeypatch):
        make_session(relay, "e1", status="reported")
        self._make_heavy(relay, monkeypatch)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--heavy-override", "one more fix")
        rec = ledger_events(relay, "heavy_override")
        assert len(rec) == 1 and rec[0]["reason"] == "one more fix"
        assert len(terms.sends) == 1

    def test_upgrade_moves_one_tier_up_and_seeds_the_successor(self, relay, terms, tmp_path, capsys):
        """send SKILL.md `--upgrade`: "retire this session and spawn a seeded successor ONE TIER UP
        (haiku→sonnet→opus) with this packet first"."""
        make_session(relay, "e1", status="reported", model="claude-sonnet-5")
        run_main(relay, "send", "e1", write_packet(tmp_path), "--upgrade")
        assert relay.read_session("e1")["superseded_by"] == relay.SEED_RETIRED_BY_SEED
        new = relay.read_session("e1-r2")
        assert new is not None and relay.lead_guard.model_tier(new["model"]) == "opus"
        assert "Successor seed — e1" in (relay.packets_dir("e1-r2") / "001-packet.md").read_text()
        assert "upgrading 'e1' sonnet → opus" in capsys.readouterr().out

    def test_upgrade_still_honours_the_spawn_ceiling(self, relay, terms, tmp_path):
        """_rotate_and_send: "The spawn ceiling (executor_model_ceiling) still applies, so an
        upgrade past it refuses the same way a spawn would"."""
        cfg_write(relay, executor_model_ceiling="sonnet")
        make_session(relay, "e1", status="reported", model="claude-sonnet-5")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path), "--upgrade")
        assert "above the configured ceiling" in str(e.value)

    def test_upgrade_from_the_top_tier_is_refused(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="reported", model="fable")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path), "--upgrade")
        assert "already runs fable, the top tier" in str(e.value)

    def test_rotate_carries_worktree_topic_mcp_effort_and_keep(self, relay, terms, tmp_path):
        """send SKILL.md `--rotate`: "spawns `<sid>-r2` over the same worktree/topic/model/MCP set
        on the 1M window, and delivers this packet to it as packet 001"."""
        wt = tmp_path / "wt"; wt.mkdir()
        make_session(relay, "e1", status="reported", model="claude-sonnet-5", worktree=str(wt),
                     topic="charts", scope="ui", mcp="inherit", effort="high", keep=True)
        run_main(relay, "send", "e1", write_packet(tmp_path), "--rotate")
        new = relay.read_session("e1-r2")
        assert new["worktree"] == str(wt) and new["topic"] == "charts" and new["scope"] == "ui"
        assert new["mcp"] == "inherit" and new["effort"] == "high" and new["keep"] is True
        assert new["context"] == "1m"

    def test_rotate_picks_the_next_free_suffix(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="reported")
        make_session(relay, "e1-r2", status="closed")
        run_main(relay, "send", "e1", write_packet(tmp_path), "--rotate")
        assert relay.read_session("e1-r3") is not None


class TestSendMcpWidening:
    """send SKILL.md: a packet declaring an MCP server the executor lacks makes relay "relaunch
    that executor's SAME conversation (`--resume`…) with the wider set and deliver the packet in
    one shot"."""

    def test_packet_mcp_line_widens_via_a_resume(self, relay, terms, tmp_path, capsys):
        make_session(relay, "e1", status="reported", mcp="none")
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nMCP: inherit\n\n## Preconditions\n- ok\n")
        run_main(relay, "send", "e1", pkt)
        assert relay.read_session("e1")["mcp"] == "inherit"
        assert terms.spawns[0]["resume_id"] == "cs-e1"
        assert terms.sends == []                        # never typed into the old process
        assert "MCP set widened to inherit" in capsys.readouterr().out

    def test_an_already_covered_mcp_line_sends_in_place(self, relay, terms, tmp_path):
        """lead_guard.mcp_covers: `inherit` covers everything, so no relaunch is warranted."""
        make_session(relay, "e1", status="reported", mcp="inherit")
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nMCP: inherit\n\n## Preconditions\n- ok\n")
        run_main(relay, "send", "e1", pkt)
        assert len(terms.sends) == 1 and terms.spawns == []

    def test_unknown_server_name_refuses_before_killing_the_tab(self, relay, terms, tmp_path):
        """cmd_send: "validate the allowlist NOW (unknown names refuse before anything is killed)"."""
        make_session(relay, "e1", status="reported", mcp="none")
        pkt = write_packet(tmp_path,
                           body="GOAL — do it.\n\nMCP: definitely-not-a-server\n\n## Preconditions\n- ok\n")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", pkt)
        assert "send: packet MCP line" in str(e.value)
        assert terms.closes == [] and terms.spawns == []

    def test_widening_without_a_captured_conversation_is_refused(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="reported", mcp="none", claude_session=None)
        pkt = write_packet(tmp_path, body="GOAL — do it.\n\nMCP: inherit\n\n## Preconditions\n- ok\n")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", pkt)
        assert "no captured Claude session to relaunch" in str(e.value)


class TestSendTabGoneFallback:
    def test_a_dead_tab_falls_back_to_resume_and_delivers(self, relay, terms, tmp_path, capsys):
        """cmd_send (B): "If we pinned this session's Claude conversation at spawn, don't force a
        cold spawn — RESUME the conversation and deliver the new packet in one shot"."""
        make_session(relay, "e1", status="reported")
        terms.send_ok = False
        run_main(relay, "send", "e1", write_packet(tmp_path))
        assert terms.spawns[0]["resume_id"] == "cs-e1"
        assert "tab '[Exec] e1' was gone — resumed with full context" in capsys.readouterr().out
        assert relay.read_session("e1")["status"] == "busy"

    def test_the_old_tab_is_killed_and_closed_before_the_resume(self, relay, terms, tmp_path):
        """cmd_send: "The old tab can still be ALIVE here … kill + close it FIRST, or `--resume`
        would open a second live copy of the same conversation alongside the first"."""
        make_session(relay, "e1", status="reported")
        terms.send_ok = False
        run_main(relay, "send", "e1", write_packet(tmp_path))
        assert terms.closes and terms.closes[0]["handle"] == "w0t0p0:STUB"

    def test_no_conversation_and_no_tab_marks_the_session_dead(self, relay, terms, tmp_path):
        make_session(relay, "e1", status="reported", claude_session=None)
        terms.send_ok = False
        with pytest.raises(SystemExit) as e:
            run_main(relay, "send", "e1", write_packet(tmp_path))
        assert "marked session dead" in str(e.value)
        assert relay.read_session("e1")["status"] == "dead"
