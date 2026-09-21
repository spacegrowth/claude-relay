"""
Bug-hunt unit tests for lead SUCCESSION and lead-tab addressing (packet bh-cli):
`relay handoff` / `relay close-predecessor` / `relay nudge-lead` / `relay focus` / `relay whoami`.
Split out of tests/test_cli_lead.py to keep each file under the packet's ~800-line bound.

Every assertion is anchored to a documented contract — README.md, skills/*/SKILL.md,
docs/post-0.3.27-backlog.md, or the function's own docstring — named in each test's docstring.

Run: pytest tests/test_cli_lead_succession.py -q
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
                        "tty_by_id", "pid_on_tty", "title_by_id", "live_session_names",
                        "exists_by_id", "close_by_id")


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
        self.dead_handles = set()   # handles the id-only lookups (exists_by_id) report as gone
        self.gone_after_failed_close = False   # a close_by_id that fails still leaves no tab
        self.tty_lookups = []  # every tty_by_id handle — the pid-kill path's only way in
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

    def exists_by_id(self, handle):
        self.ops.append(("exists_by_id", self._name))
        if not handle or handle in self.dead_handles:
            return False
        return self.alive_by.get(self._name, self.alive)

    def close_by_id(self, handle):
        self.ops.append(("close", self._name))
        self.closes.append({"label": None, "handle": handle, "pid": None, "by_id": True})
        if self.close_ok or self.gone_after_failed_close:
            self.dead_handles.add(handle)
        return self.close_ok

    def tty_by_id(self, handle):
        self.tty_lookups.append(handle)
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


# ── handoff ─────────────────────────────────────────────────────────────────────────────────────

class TestHandoff:
    """handoff SKILL.md: "This opens a NEW lead tab, pre-armed … seeded with a short pointer at a
    relay-prepared copy of the handoff file — your source md is never modified … **As its final
    act, this steps the CURRENT session down**"."""

    @pytest.fixture
    def outgoing(self, relay, monkeypatch):
        arm_lead(relay, "lead-old", "webapp", model="opus", iterm_session="w0t0p0:OLD",
                 backend="iterm", started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-old")
        return "lead-old"

    def _doc(self, tmp_path, text="# Handoff\n\nIn flight: e1. Next: review its report.\n"):
        p = tmp_path / "handoff.md"
        p.write_text(text)
        return str(p)

    def _successor(self, relay):
        return next(m["session_id"] for m in relay.lead_guard.list_leads(relay.STATE_ROOT))

    def test_the_successor_is_pre_armed_under_a_pinned_id(self, relay, terms, tmp_path, outgoing):
        run_main(relay, "handoff", self._doc(tmp_path))
        sid = self._successor(relay)
        assert terms.spawns[0]["session_uuid"] == sid       # the pin IS the launch id
        assert relay.lead_guard.is_lead(relay.STATE_ROOT, sid)
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, sid)["project"] == "webapp"

    def test_the_caller_is_stepped_down_as_the_final_act(self, relay, terms, tmp_path, outgoing,
                                                        capsys):
        run_main(relay, "handoff", self._doc(tmp_path))
        assert not relay.lead_guard.is_lead(relay.STATE_ROOT, "lead-old")
        assert "this session has stepped down" in capsys.readouterr().out
        assert ledger_events(relay, "lead_handoff")[-1]["from_lead"] == "lead-old"

    def test_the_relay_owned_copy_carries_the_aftercare_block(self, relay, terms, tmp_path, outgoing):
        run_main(relay, "handoff", self._doc(tmp_path))
        sid = self._successor(relay)
        copy = relay.lead_guard.lead_dir(relay.STATE_ROOT, sid) / "handoff.md"
        text = copy.read_text()
        assert "In flight: e1." in text
        assert "SUCCESSOR AFTERCARE" in text
        assert sid in text

    def test_the_users_source_file_is_never_modified(self, relay, terms, tmp_path, outgoing):
        doc = self._doc(tmp_path)
        before = Path(doc).read_text()
        run_main(relay, "handoff", doc)
        assert Path(doc).read_text() == before

    def test_the_seed_prompt_stays_a_short_pointer(self, relay, terms, tmp_path, outgoing):
        """cmd_handoff's LIVE-INCIDENT FIX: "a prior revision inlined that whole aftercare block …
        the typed launch command truncated mid-string on a real run, corrupting the shell"."""
        run_main(relay, "handoff", self._doc(tmp_path))
        prompt = terms.spawns[0]["prompt"]
        sid = self._successor(relay)
        copy = relay.lead_guard.lead_dir(relay.STATE_ROOT, sid) / "handoff.md"
        # the prompt POINTS at the aftercare section; it must not carry its body
        assert prompt.count(sid) == 1        # once, inside the copy's path — never inlined twice
        assert "close-predecessor" not in prompt            # that lives in the copy
        assert str(copy) in prompt and len(prompt) < 400
        assert "close-predecessor" in copy.read_text()

    def test_re_handing_off_an_aftercare_doc_never_stacks_a_second_block(self, relay, terms,
                                                                        tmp_path, outgoing):
        """#19: "any aftercare section already present in `body` (from a prior handoff of this same
        doc) is stripped before the fresh one is appended"."""
        run_main(relay, "handoff", self._doc(tmp_path))
        sid1 = self._successor(relay)
        copy1 = relay.lead_guard.lead_dir(relay.STATE_ROOT, sid1) / "handoff.md"
        text = copy1.read_text()
        assert text.count("SUCCESSOR AFTERCARE") == 1
        again = relay.build_handoff_copy(text, "successor-2")
        assert again.count("SUCCESSOR AFTERCARE") == 1
        assert "successor-2" in again and sid1 not in again

    def test_the_predecessors_tab_identity_travels_in_the_successors_marker(self, relay, terms,
                                                                            tmp_path, outgoing):
        """cmd_handoff: "Record the predecessor NOW, while its marker is still readable … this is
        the only place this information can be captured"."""
        run_main(relay, "handoff", self._doc(tmp_path))
        pred = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))["predecessor"]
        assert pred["session_id"] == "lead-old"
        assert pred["iterm_session"] == "w0t0p0:OLD"

    def test_the_predecessor_tab_is_retitled_ex_lead(self, relay, terms, tmp_path, outgoing):
        """§4/#4: "exactly one tab ever reads `[Lead] X`, and the leftovers announce themselves as
        `[ex-Lead] X`" — "ALWAYS by recorded HANDLE (rename_by_id), never by label match"."""
        run_main(relay, "handoff", self._doc(tmp_path))
        assert {"handle": "w0t0p0:OLD", "name": "[ex-Lead] webapp"} in terms.renames
        pred = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))["predecessor"]
        assert pred["tab_label"] == "[ex-Lead] webapp"

    def test_the_successor_inherits_the_predecessors_tab_color(self, relay, terms, tmp_path,
                                                               monkeypatch):
        """Backlog row 63 / README "Telling tabs apart": "each lead gets a stable color … and every
        executor it spawns inherits it". A handoff transfers the lead's identity AND re-parents its
        executors, so picking a fresh color for the successor made one group read as two."""
        arm_lead(relay, "lead-old", "webapp", iterm_session="w0t0p0:OLD", backend="iterm",
                 color=[210, 172, 124], started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-old")
        run_main(relay, "handoff", self._doc(tmp_path))
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))
        assert m["color"] == [210, 172, 124]
        assert terms.spawns[0]["tab_color"] == [210, 172, 124]   # the successor's TAB too

    def test_a_predecessor_with_no_color_falls_back_to_a_picked_one(self, relay, terms, tmp_path,
                                                                    outgoing):
        """"falling back to pick_lead_color only when the caller has none" — and the caller's own
        (absent) claim must not steer the pick."""
        run_main(relay, "handoff", self._doc(tmp_path))
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))
        assert tuple(m["color"]) in {tuple(c) for c in relay.lead_guard.TAB_PALETTE}

    def test_the_transferred_color_is_not_treated_as_taken(self, relay, terms, tmp_path,
                                                           monkeypatch):
        """pick_lead_color's `exclude_leads`: the outgoing lead's color is being TRANSFERRED, not
        shared, so it must not count as claimed while the successor's fallback pick runs."""
        arm_lead(relay, "lead-old", "webapp", iterm_session="w0t0p0:OLD", backend="iterm",
                 color="not-a-color", started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-old")
        picked = {}
        real = relay.lead_guard.pick_lead_color

        def spy(state_root, sid, exclude_leads=()):
            picked["exclude"] = tuple(exclude_leads)
            return real(state_root, sid, exclude_leads)

        monkeypatch.setattr(relay.lead_guard, "pick_lead_color", spy)
        run_main(relay, "handoff", self._doc(tmp_path))
        assert picked["exclude"] == ("lead-old",)

    def test_the_successor_never_inherits_the_autonomous_posture(self, relay, terms, tmp_path,
                                                                 monkeypatch):
        """cmd_handoff: "autonomous — False/"config", never inherited from the outgoing lead"."""
        arm_lead(relay, "lead-old", "webapp", autonomous=True, autonomous_source="command")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-old")
        run_main(relay, "handoff", self._doc(tmp_path))
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))
        assert relay.lead_guard.autonomous_state(m) == (False, "config")

    def test_the_successor_started_stamp_is_captured_once(self, relay, terms, tmp_path, outgoing):
        """"Captured ONCE and passed to both marker writes below … letting the second re-default
        `started` to its own now() would make the field mean 'when the tab finished opening'"."""
        run_main(relay, "handoff", self._doc(tmp_path))
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))
        assert m["started"] == m["last_active"] or m["started"] <= m["last_active"]
        assert m["started"] != "2020-01-01T00:00:00"       # the SUCCESSOR's own start, not the old one

    def test_project_and_model_flags_override_the_inherited_ones(self, relay, terms, tmp_path,
                                                                 outgoing):
        run_main(relay, "handoff", self._doc(tmp_path), "--project", "docs", "--model", "sonnet")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))
        assert m["project"] == "docs" and m["model"] == "sonnet"
        assert m["tab_label"] == "[Lead] docs"

    def test_a_named_heading_names_the_successor(self, relay, terms, tmp_path, outgoing):
        """Row 92: the handoff note's own `# ` heading, when it names the work, is the successor's
        default project — not the caller's ("webapp") at all."""
        doc = self._doc(tmp_path, "# Relay release 0.5.0\n\nIn flight: e1.\n")
        run_main(relay, "handoff", doc)
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))
        expected = relay.slugify("Relay release 0.5.0")
        assert m["project"] == expected
        assert m["tab_label"] == f"[Lead] {expected}"

    def test_a_handoff_prefixed_heading_drops_the_prefix(self, relay, terms, tmp_path, outgoing):
        """"Handoff — …"/"Handoff: …" names the ARTIFACT, not the work — stripped before
        slugifying, so the successor isn't named "handoff-relay-release-0-5-0"."""
        doc = self._doc(tmp_path, "# Handoff — Relay release 0.5.0\n\nIn flight: e1.\n")
        run_main(relay, "handoff", doc)
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))
        assert m["project"] == relay.slugify("Relay release 0.5.0")

    def test_no_heading_falls_back_to_the_callers_project_counter_stripped(self, relay, terms,
                                                                           tmp_path, monkeypatch):
        """Row 92 field shape: "this lead is `claude-relay-2` only because the name creeped once
        and every handoff since inherited the counter" — a handoff must never carry that counter
        forward, whether or not the note names a heading of its own."""
        arm_lead(relay, "lead-old", "claude-relay-2", model="opus", iterm_session="w0t0p0:OLD",
                 backend="iterm", started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-old")
        doc = self._doc(tmp_path, "what's in flight, what's done, next steps\n")  # no '# ' heading
        run_main(relay, "handoff", doc)
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, self._successor(relay))
        assert m["project"] == "claude-relay"

    def test_a_dropped_discipline_marker_is_warned_about(self, relay, terms, tmp_path, outgoing,
                                                         capsys):
        """d4: "`[discipline]` markers … that appear in the doc the outgoing lead itself inherited
        must survive into the doc it hands to its OWN successor, verbatim"."""
        inherited = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-old") / "handoff.md"
        inherited.parent.mkdir(parents=True, exist_ok=True)
        inherited.write_text("# Handoff\n\n[ops-not-lead-work] delegate box work.\n")
        run_main(relay, "handoff", self._doc(tmp_path))
        assert "discipline markers dropped: [ops-not-lead-work]" in capsys.readouterr().out

    def test_a_carried_forward_marker_is_silent(self, relay, terms, tmp_path, outgoing, capsys):
        inherited = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-old") / "handoff.md"
        inherited.parent.mkdir(parents=True, exist_ok=True)
        inherited.write_text("# Handoff\n\n[ops-not-lead-work] delegate box work.\n")
        run_main(relay, "handoff",
                 self._doc(tmp_path, "# Handoff\n\n[ops-not-lead-work] still true.\n"))
        assert "discipline markers dropped" not in capsys.readouterr().out

    def test_surfaced_and_pending_stamps_carry_forward_to_the_successor(self, relay, terms, tmp_path,
                                                                        outgoing):
        """Row 67: a report the predecessor already proved-delivered (surfaced_reports.json) or was
        mid-retry announcing (pending_wakes.json) must not look brand new to the successor — the
        gm-signin-114240 incident (a predecessor-reviewed-and-committed report re-woke the successor
        as "NOT yet proven delivered")."""
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-old", ["e1:1"])
        relay.lead_guard.mark_pending(relay.STATE_ROOT, "lead-old", ["e2:1"])
        run_main(relay, "handoff", self._doc(tmp_path))
        sid = self._successor(relay)
        assert relay.lead_guard.load_surfaced(relay.STATE_ROOT, sid) == {"e1:1"}
        assert "e2:1" in relay.lead_guard.load_pending(relay.STATE_ROOT, sid)

    def test_a_non_lead_caller_is_refused(self, relay, terms, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "not-a-lead")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "handoff", self._doc(tmp_path))
        assert "caller isn't an armed lead" in str(e.value)
        assert terms.spawns == []

    def test_a_missing_or_empty_handoff_file_is_refused(self, relay, terms, tmp_path, outgoing):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "handoff", str(tmp_path / "nope.md"))
        assert "doesn't exist or is empty" in str(e.value)
        empty = tmp_path / "empty.md"
        empty.write_text("")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "handoff", str(empty))
        assert "doesn't exist or is empty" in str(e.value)
        assert relay.lead_guard.is_lead(relay.STATE_ROOT, "lead-old")    # still lead

    def test_a_failed_spawn_leaves_no_ghost_and_keeps_the_caller_lead(self, relay, terms, tmp_path,
                                                                      outgoing, monkeypatch):
        """"a failed spawn must NOT step the caller down (it's still the only live lead), and must
        NOT leave the successor's pre-written marker behind as an unreachable ghost"."""
        terms.spawn_error = RuntimeError("no tab")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "handoff", self._doc(tmp_path))
        assert "this session remains lead" in str(e.value)
        assert relay.lead_guard.is_lead(relay.STATE_ROOT, "lead-old")
        assert [m["session_id"] for m in relay.lead_guard.list_leads(relay.STATE_ROOT)] == ["lead-old"]

    def test_a_busy_executor_is_reparented_to_the_successor(self, relay, terms, tmp_path, outgoing):
        """11a (lead-found, 2026-09-05 22:01): waiting for the successor's first send/resume
        (adopt-on-claim) left a window where a report landing BEFORE that first claim woke nobody
        — its escalation push still targeted the stepped-down predecessor. Re-parenting now happens
        proactively, at handoff time, before the predecessor is even stepped down."""
        make_session(relay, "e1", owner_lead="lead-old", owner_project="webapp", status="busy")
        run_main(relay, "handoff", self._doc(tmp_path))
        sid = self._successor(relay)
        s = relay.read_session("e1")
        assert s["owner_lead"] == sid
        assert s["owner_project"] == "webapp"
        rec = ledger_events(relay, "adopted")
        assert len(rec) == 1
        assert rec[0]["session_id"] == "e1"
        assert rec[0]["from_lead"] == "lead-old" and rec[0]["to_lead"] == sid

    def test_a_closed_executor_is_not_reparented(self, relay, terms, tmp_path, outgoing):
        """Only NON-terminal executors are worth re-parenting — a closed/superseded one has no
        further report or wake to worry about."""
        make_session(relay, "e1", owner_lead="lead-old", status="closed")
        run_main(relay, "handoff", self._doc(tmp_path))
        assert relay.read_session("e1")["owner_lead"] == "lead-old"
        assert ledger_events(relay, "adopted") == []

    def test_an_executor_owned_by_a_different_lead_is_untouched(self, relay, terms, tmp_path,
                                                                 outgoing):
        make_session(relay, "e-other", owner_lead="some-other-lead", status="busy")
        run_main(relay, "handoff", self._doc(tmp_path))
        assert relay.read_session("e-other")["owner_lead"] == "some-other-lead"
        assert ledger_events(relay, "adopted") == []

    # ── row 70 item 2: inherited pins are named at handoff ───────────────────────────────────────
    # A pin survives every handoff and auto-close never touches it, so a successor silently inherits
    # executors it will never park and never knew were pinned. Say so once, at the moment of
    # inheritance, with the release command.

    def test_inherited_pins_are_named_in_the_handoff_output(self, relay, terms, tmp_path, outgoing,
                                                            capsys):
        make_session(relay, "e1", owner_lead="lead-old", status="busy", keep=True)
        make_session(relay, "e2", owner_lead="lead-old", status="reported", keep=True)
        make_session(relay, "e3", owner_lead="lead-old", status="busy")   # unpinned
        run_main(relay, "handoff", self._doc(tmp_path))
        out = capsys.readouterr().out
        assert "2 pinned executors inherited: e1, e2" in out
        assert "relay keep <sid> --off" in out
        assert "e3" not in out.split("pinned executors inherited")[1].splitlines()[0]

    def test_reparenting_carries_each_executors_surfaced_stamp(self, relay, terms, tmp_path,
                                                                outgoing):
        """Row 70 item 4: `_reparent_executors` carries the stamps per adopted executor, so the
        carry does not depend on cmd_handoff's separate wholesale copy — an executor that changes
        hands takes its "already seen" history with it wherever the re-parent is driven from."""
        make_session(relay, "e1", owner_lead="lead-old", status="busy")
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-old", ["e1:1"])
        moved = relay._reparent_executors("lead-old", "lead-next", "webapp")
        assert moved == ["e1"]
        assert relay.lead_guard.load_surfaced(relay.STATE_ROOT, "lead-next") == {"e1:1"}

    def test_reparenting_does_not_import_another_executors_stamp(self, relay, terms, tmp_path,
                                                                 outgoing):
        make_session(relay, "e1", owner_lead="lead-old", status="busy")
        relay.lead_guard.mark_surfaced(relay.STATE_ROOT, "lead-old", ["e1:1", "e-elsewhere:3"])
        relay._reparent_executors("lead-old", "lead-next", "webapp")
        assert relay.lead_guard.load_surfaced(relay.STATE_ROOT, "lead-next") == {"e1:1"}

    def test_no_pins_means_no_pin_line(self, relay, terms, tmp_path, outgoing, capsys):
        make_session(relay, "e1", owner_lead="lead-old", status="busy")
        run_main(relay, "handoff", self._doc(tmp_path))
        assert "pinned executors inherited" not in capsys.readouterr().out

    # Lead review of packet 001: the console line above reaches the OUTGOING lead only — the one
    # console the successor never reads. The same sentence has to travel into the aftercare, which
    # is the document the successor is told to read before anything else.

    def _copy(self, relay):
        sid = self._successor(relay)
        return (relay.lead_guard.lead_dir(relay.STATE_ROOT, sid) / "handoff.md").read_text()

    def test_inherited_pins_are_named_in_the_successors_handoff_copy(self, relay, terms, tmp_path,
                                                                     outgoing):
        make_session(relay, "e1", owner_lead="lead-old", status="busy", keep=True)
        make_session(relay, "e2", owner_lead="lead-old", status="reported", keep=True)
        make_session(relay, "e3", owner_lead="lead-old", status="busy")     # unpinned
        run_main(relay, "handoff", self._doc(tmp_path))
        text = self._copy(relay)
        assert "2 pinned executors inherited: e1, e2" in text
        assert "relay keep <sid> --off" in text
        pin_line = next(ln for ln in text.splitlines() if "pinned executors inherited" in ln)
        assert "e3" not in pin_line

    def test_a_single_pin_reads_as_singular_in_the_copy(self, relay, terms, tmp_path, outgoing):
        make_session(relay, "e1", owner_lead="lead-old", status="busy", keep=True)
        run_main(relay, "handoff", self._doc(tmp_path))
        assert "1 pinned executor inherited: e1" in self._copy(relay)

    def test_a_closed_pinned_executor_is_not_named_in_the_copy(self, relay, terms, tmp_path,
                                                               outgoing):
        """Same set the re-parent moves: a closed executor is not inherited, so it is not a pin the
        successor has to care about."""
        make_session(relay, "e1", owner_lead="lead-old", status="closed", keep=True)
        run_main(relay, "handoff", self._doc(tmp_path))
        assert "pinned executor" not in self._copy(relay)

    def test_another_leads_pin_is_not_named_in_the_copy(self, relay, terms, tmp_path, outgoing):
        make_session(relay, "e-other", owner_lead="some-other-lead", status="busy", keep=True)
        run_main(relay, "handoff", self._doc(tmp_path))
        assert "pinned executor" not in self._copy(relay)

    def test_no_pins_means_no_pin_item_in_the_copy(self, relay, terms, tmp_path, outgoing):
        """Item 5 is absent entirely rather than rendered as a "0 pinned executors" line."""
        make_session(relay, "e1", owner_lead="lead-old", status="busy")
        run_main(relay, "handoff", self._doc(tmp_path))
        text = self._copy(relay)
        assert "pinned executor" not in text and "SUCCESSOR AFTERCARE" in text

    def test_the_pin_item_survives_a_re_handoff_without_stacking(self, relay, terms, tmp_path,
                                                                 outgoing):
        """#19 still holds with the new item: re-handing off an already-processed copy strips the
        whole old aftercare, pin line included, before appending the fresh one."""
        text = relay.build_handoff_copy("in flight: nothing", "successor-1", pins=["e1"])
        again = relay.build_handoff_copy(text, "successor-2", pins=["e2", "e3"])
        assert again.count("SUCCESSOR AFTERCARE") == 1
        assert "2 pinned executors inherited: e2, e3" in again
        assert "e1" not in again


# ── close-predecessor ───────────────────────────────────────────────────────────────────────────

class TestSuccessorTitleNeverCollides:
    """Backlog row 75: the handoff gave the successor the predecessor's exact title
    (`[Lead] claude-relay-2`) while the predecessor's tab was still open — the ambiguity row 74's
    close-predecessor then mis-resolved. The `[ex-Lead]` retitle only lands at step-down, so the
    collision window is the handoff itself."""

    @pytest.fixture
    def outgoing(self, relay, monkeypatch):
        arm_lead(relay, "lead-old", "webapp", iterm_session="w0t0p0:OLD", backend="iterm",
                 started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-old")
        monkeypatch.delenv("TERM_SESSION_ID", raising=False)
        return "lead-old"

    def _handoff(self, relay, tmp_path):
        doc = tmp_path / "handoff.md"
        doc.write_text("# Handoff\n\nIn flight: nothing.\n")
        run_main(relay, "handoff", str(doc))
        return next(m["session_id"] for m in relay.lead_guard.list_leads(relay.STATE_ROOT)
                    if m["session_id"] != "lead-other")

    def test_the_successor_is_disambiguated_from_its_still_open_predecessor(self, relay, terms,
                                                                            tmp_path, outgoing):
        sid = self._handoff(relay, tmp_path)
        expected = f"[Lead] webapp ·{sid[:4]}"
        assert terms.spawns[0]["label"] == expected                   # the tab opens under it
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, sid)["tab_label"] == expected

    def test_the_suffix_is_deterministic_from_the_successor_id(self, relay):
        assert relay._distinct_lead_label("[Lead] x", "70e2abcd-1111", {"tab_label": "[Lead] x"}) \
            == "[Lead] x ·70e2"

    def test_no_live_lead_wearing_the_title_keeps_it_bare(self, relay, terms, tmp_path, outgoing):
        relay.lead_guard.update_marker(relay.STATE_ROOT, "lead-old", tab_label="[Lead] renamed")
        sid = self._handoff(relay, tmp_path)
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, sid)["tab_label"] == "[Lead] webapp"

    def test_another_live_lead_wearing_the_title_also_disambiguates(self, relay, terms, tmp_path,
                                                                    outgoing):
        relay.lead_guard.update_marker(relay.STATE_ROOT, "lead-old", tab_label="[Lead] renamed")
        arm_lead(relay, "lead-other", "webapp", iterm_session="w0t5p0:OTHER", backend="iterm")
        sid = self._handoff(relay, tmp_path)
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, sid)["tab_label"] \
            == f"[Lead] webapp ·{sid[:4]}"

    def test_a_dead_lead_wearing_the_title_does_not_count(self, relay, terms, tmp_path, outgoing):
        relay.lead_guard.update_marker(relay.STATE_ROOT, "lead-old", tab_label="[Lead] renamed")
        arm_lead(relay, "lead-other", "webapp", iterm_session="w0t5p0:OTHER", backend="iterm")
        terms.alive = False           # its tab is gone — a ghost holds no title
        sid = self._handoff(relay, tmp_path)
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, sid)["tab_label"] == "[Lead] webapp"

    def test_a_dead_predecessor_is_not_live_while_its_suffixed_successor_is_open(self, relay):
        """Through the REAL iterm.is_alive/title_is_live (only the two osascript probes are faked,
        no FakeTerm): the predecessor's handle is gone and the only tab carrying its title stem is
        the successor's `[Lead] webapp ·70e2` — `_lead_liveness` must not read it as live."""
        m = {"session_id": "lead-old", "tab_label": "[Lead] webapp", "iterm_session": "w0t0p0:OLD",
             "backend": "iterm", "last_active": "2000-01-01T00:00:00"}
        it = relay.iterm_backend
        with mock.patch.object(it, "_session_exists_by_id", return_value=False), \
             mock.patch.object(it, "running", return_value=True), \
             mock.patch.object(it, "live_session_names", return_value={"[Lead] webapp ·70e2"}):
            assert relay.backend.by_name("iterm") is it
            assert relay._lead_liveness(m) == "ghost"
            succ = dict(m, session_id="70e2abcd", tab_label="[Lead] webapp ·70e2",
                        iterm_session="w0t1p0:NEW")
            assert relay._lead_liveness(succ) == "live"

    def test_the_aftercare_re_arm_keeps_the_disambiguated_title(self, relay, terms, tmp_path,
                                                                outgoing, monkeypatch):
        """The aftercare tells the successor to run /relay:mode, whose lead-start renames the tab —
        back to the bare, colliding title unless it keeps the handoff's exact label."""
        sid = self._handoff(relay, tmp_path)
        expected = f"[Lead] webapp ·{sid[:4]}"
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
        monkeypatch.setenv("TERM_SESSION_ID", "w0t0p0:STUB")
        terms.renames.clear()
        run_main(relay, "lead-start", sid, "--project", "webapp")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, sid)["tab_label"] == expected
        assert terms.renames == [{"handle": "w0t0p0:STUB", "name": expected}]

    def test_an_unrelated_label_is_not_preserved_by_a_re_arm(self, relay, terms, monkeypatch):
        """Only the handoff's own deterministic form survives a re-arm; anything else is re-derived."""
        arm_lead(relay, "lead-x", "webapp")
        relay.lead_guard.update_marker(relay.STATE_ROOT, "lead-x", tab_label="[Lead] webapp ·zzzz")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-x")
        run_main(relay, "lead-start", "lead-x", "--project", "webapp", "--no-rename")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-x")["tab_label"] == "[Lead] webapp"

    def test_tidy_list_and_focus_still_find_the_suffixed_successor(self, relay, terms, tmp_path,
                                                                   outgoing, capsys):
        sid = self._handoff(relay, tmp_path)
        expected = f"[Lead] webapp ·{sid[:4]}"
        groups = relay.tidy_groups()                  # grouped by marker + handle, not title
        assert [(g["lead"], g["handle"]) for g in groups] == [(sid, "w0t0p0:STUB")]
        capsys.readouterr()
        run_main(relay, "list")
        assert "webapp" in capsys.readouterr().out
        run_main(relay, "focus", "webapp")
        assert terms.focuses[-1] == {"label": expected, "handle": "w0t0p0:STUB", "pid": None}


class TestResumeLeadKeepsTheDisambiguatedTitle:
    """Backlog row 75's leftover: `relay resume-lead` recomputed the bare `[Lead] X`, so a suffixed
    successor resumed while its predecessor's tab was still open re-collided — and its marker's
    tab_label no longer matched the real tab. The resuming lead's OWN tab is dead in every test
    (FakeTerm.alive False, so the refuse-if-alive guard passes); OTHER leads' liveness is pinned
    per session id through `_lead_liveness`."""

    def _resume(self, relay, terms, sid, live=()):
        terms.alive = False
        with mock.patch.object(relay, "_lead_liveness",
                               side_effect=lambda m, *a, **k: "live" if m.get("session_id") in live
                               else "ghost"):
            relay.cmd_resume_lead(sid)
        return terms.spawns[-1]["label"], \
            relay.lead_guard.read_marker(relay.STATE_ROOT, sid)["tab_label"]

    def test_a_recorded_suffixed_label_is_kept_verbatim(self, relay, terms):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "ab12cdef", project="proj", cwd="/tmp",
                                      tab_label="[Lead] proj ·ab12")
        arm_lead(relay, "lead-pred", "proj")          # the predecessor, still open under the bare title
        assert self._resume(relay, terms, "ab12cdef", live={"lead-pred"}) \
            == ("[Lead] proj ·ab12", "[Lead] proj ·ab12")

    def test_a_recorded_label_is_preferred_over_the_recomputed_one(self, relay, terms):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "ab12cdef", project="proj", cwd="/tmp",
                                      tab_label="[Lead] proj ·ab12")
        assert self._resume(relay, terms, "ab12cdef") == ("[Lead] proj ·ab12", "[Lead] proj ·ab12")

    def test_no_recorded_label_and_no_collision_resumes_bare(self, relay, terms):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "ab12cdef", project="proj", cwd="/tmp")
        arm_lead(relay, "lead-dead", "proj")          # wears the title, but its tab is gone
        assert self._resume(relay, terms, "ab12cdef") == ("[Lead] proj", "[Lead] proj")

    def test_no_recorded_label_while_another_live_lead_wears_it_is_suffixed(self, relay, terms):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "ab12cdef", project="proj", cwd="/tmp")
        arm_lead(relay, "lead-other", "proj")
        assert self._resume(relay, terms, "ab12cdef", live={"lead-other"}) \
            == ("[Lead] proj ·ab12", "[Lead] proj ·ab12")

    def test_a_recorded_bare_label_another_live_lead_wears_is_suffixed(self, relay, terms):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "ab12cdef", project="proj", cwd="/tmp",
                                      tab_label="[Lead] proj")
        arm_lead(relay, "lead-other", "proj")
        assert self._resume(relay, terms, "ab12cdef", live={"lead-other"}) \
            == ("[Lead] proj ·ab12", "[Lead] proj ·ab12")

    def test_an_already_suffixed_label_is_never_suffixed_again(self, relay, terms):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "ab12cdef", project="proj", cwd="/tmp",
                                      tab_label="[Lead] proj ·ab12")
        # Even a live lead wearing the exact suffixed title does not stack a second suffix.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-other", project="proj", cwd="/tmp",
                                      tab_label="[Lead] proj ·ab12")
        assert self._resume(relay, terms, "ab12cdef", live={"lead-other"}) \
            == ("[Lead] proj ·ab12", "[Lead] proj ·ab12")
        assert relay._resume_lead_label("[Lead] proj ·ab12", "ab12cdef") == "[Lead] proj ·ab12"

    def test_its_own_marker_never_counts_as_the_collision(self, relay, terms):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "ab12cdef", project="proj", cwd="/tmp",
                                      tab_label="[Lead] proj")
        assert self._resume(relay, terms, "ab12cdef", live={"ab12cdef"}) \
            == ("[Lead] proj", "[Lead] proj")


class TestClosePredecessor:
    """cmd_close_predecessor: "reads it back, closes the tab, and clears the field so the offer
    can't repeat. Never invoked automatically"."""

    def _armed_with_pred(self, relay, monkeypatch, **pred_over):
        pred = {"session_id": "lead-old", "tab_label": "[ex-Lead] webapp",
                "iterm_session": "w0t0p0:OLD"}
        pred.update(pred_over)
        arm_lead(relay, "lead-new", "webapp", predecessor=pred, backend="iterm",
                 iterm_session="w0t1p0:NEW")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        monkeypatch.delenv("TERM_SESSION_ID", raising=False)   # the runner's own tab is not in play
        return pred

    def test_it_closes_the_predecessors_tab_by_its_recorded_handle(self, relay, terms, monkeypatch,
                                                                   capsys):
        self._armed_with_pred(relay, monkeypatch)
        run_main(relay, "close-predecessor")
        assert terms.closes and terms.closes[0]["handle"] == "w0t0p0:OLD"
        assert "closed its tab" in capsys.readouterr().out

    def test_it_clears_the_field_so_the_offer_cannot_repeat(self, relay, terms, monkeypatch, capsys):
        self._armed_with_pred(relay, monkeypatch)
        run_main(relay, "close-predecessor")
        assert "predecessor" not in relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-new")
        capsys.readouterr()
        run_main(relay, "close-predecessor")
        assert "no predecessor recorded" in capsys.readouterr().out
        assert len(terms.closes) == 1            # closed exactly once

    def test_it_ledgers_the_close(self, relay, terms, monkeypatch):
        self._armed_with_pred(relay, monkeypatch)
        run_main(relay, "close-predecessor")
        rec = ledger_events(relay, "predecessor_closed")
        assert len(rec) == 1 and rec[0]["predecessor"] == "lead-old" and rec[0]["tab_closed"] is True

    def test_no_predecessor_is_a_clean_no_op(self, relay, terms, monkeypatch, capsys):
        arm_lead(relay, "lead-new", "webapp")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        run_main(relay, "close-predecessor")
        assert "no predecessor recorded for this lead — nothing to close" in capsys.readouterr().out
        assert terms.closes == []

    def test_it_refuses_to_close_a_still_armed_predecessor(self, relay, terms, monkeypatch):
        """"not closing a live lead's tab; ask them to /relay:stop first"."""
        self._armed_with_pred(relay, monkeypatch)
        arm_lead(relay, "lead-old", "webapp-old")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "close-predecessor")
        assert "still an armed lead" in str(e.value)
        assert terms.closes == []

    def test_a_non_lead_caller_is_refused(self, relay, terms, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "not-a-lead")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "close-predecessor")
        assert "caller isn't an armed lead" in str(e.value)

    def test_a_tab_that_closed_itself_is_not_reported_as_lingering(self, relay, terms, monkeypatch,
                                                                    capsys):
        self._armed_with_pred(relay, monkeypatch)
        terms.close_ok = False
        terms.gone_after_failed_close = True    # the handle resolved, then the tab went on its own
        run_main(relay, "close-predecessor")
        assert "tab closed itself" in capsys.readouterr().out

    def test_a_lingering_tab_says_cmd_w(self, relay, terms, monkeypatch, capsys):
        self._armed_with_pred(relay, monkeypatch)
        terms.close_ok = False
        terms.alive = True
        run_main(relay, "close-predecessor")
        assert "Cmd-W it if it lingers" in capsys.readouterr().out

    def test_it_addresses_the_predecessors_own_backend(self, relay, terms, monkeypatch):
        """`_lead_tab_target`'s docstring states the invariant for every lead-tab operation: the
        backend and handle come "from the marker rather than from the caller's ambient state", and
        `cmd_nudge_lead`'s docstring names the live incident (Defect A) when they did not —
        "an executor running under Terminal.app selects the `terminal` backend for ITSELF, and used
        to hand that same wrong backend to an iTerm-hosted lead, which can't inject text at all".
        `cmd_handoff` already resolves the predecessor's tab as
        `backend.by_name(caller_marker.get("backend")) or iterm` for its `[ex-Lead]` retitle;
        `cmd_close_predecessor` calls the module-level `iterm` unconditionally, and the
        `predecessor` dict it reads back does not even carry a `backend` field to key off."""
        # the predecessor lead ran under Terminal.app; this invocation runs under iTerm
        pred = {"session_id": "lead-old", "tab_label": "[ex-Lead] webapp",
                "iterm_session": "twid:7", "backend": "terminal"}
        arm_lead(relay, "lead-new", "webapp", predecessor=pred, backend="iterm")
        monkeypatch.setattr(relay, "iterm", relay.backend.by_name("iterm"))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        monkeypatch.delenv("TERM_SESSION_ID", raising=False)
        terms.close_ok = False       # so the post-close liveness branch is exercised too
        run_main(relay, "close-predecessor")
        tab_ops = [(op, n) for op, n in terms.ops if op in ("close", "is_alive", "exists_by_id")]
        # resolve, close, re-check — every one of them through the predecessor's own backend
        assert [op for op, _ in tab_ops] == ["exists_by_id", "close", "exists_by_id"]
        assert {n for _, n in tab_ops} == {"terminal"}

    # ── row 74: never the caller's own tab ──────────────────────────────────────────────────────

    def _incident(self, relay, monkeypatch, **pred_over):
        """The 2026-09-07 16:10 shape: successor and predecessor BOTH titled `[Lead] claude-relay-2`
        (the predecessor's `[ex-Lead]` retitle never took), the successor's marker records the
        predecessor, and the predecessor's recorded handle no longer resolves."""
        pred = {"session_id": "4dff0f10-old", "tab_label": "[Lead] claude-relay-2",
                "iterm_session": "w0t0p0:OLD", "backend": "iterm"}
        pred.update(pred_over)
        arm_lead(relay, "lead-new", "claude-relay-2", predecessor=pred, backend="iterm",
                 iterm_session="w0t1p0:NEW")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        monkeypatch.delenv("TERM_SESSION_ID", raising=False)
        return pred

    def _assert_nothing_touched(self, relay, terms, pred):
        assert terms.closes == []                               # no close of ANY kind — by id or title
        assert not [op for op, _ in terms.ops if op == "close"]
        assert terms.tty_lookups == []                          # the pid-kill path was never entered
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-new")["predecessor"] == pred

    def test_an_unresolvable_handle_refuses_instead_of_matching_the_shared_title(
            self, relay, terms, monkeypatch):
        """Backlog row 74: `close(tab_label, handle)` fell back to a TITLE match when the handle
        missed, and both leads wore `[Lead] claude-relay-2` — so it closed the caller's own tab.
        `retitle_tab`'s rule for this exact moment: "ALWAYS by recorded HANDLE, never by label
        match … No handle → no-op, never a guess"."""
        pred = self._incident(relay, monkeypatch)
        terms.dead_handles.add("w0t0p0:OLD")
        terms.titles = {"w0t1p0:NEW": "[Lead] claude-relay-2"}   # the caller's tab: same title
        with pytest.raises(SystemExit) as e:
            run_main(relay, "close-predecessor")
        assert str(e.value) == ("close-predecessor: predecessor tab handle w0t0p0:OLD not found — "
                                "not guessing by title; close it by hand")
        self._assert_nothing_touched(relay, terms, pred)
        rec = ledger_events(relay, "predecessor_closed")
        assert len(rec) == 1 and rec[0]["tab_closed"] is False

    def test_a_missing_handle_refuses_without_touching_any_tab(self, relay, terms, monkeypatch):
        pred = self._incident(relay, monkeypatch, iterm_session=None)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "close-predecessor")
        assert "predecessor tab handle (none recorded) not found — not guessing by title" in str(e.value)
        self._assert_nothing_touched(relay, terms, pred)
        assert not [op for op, _ in terms.ops if op in ("exists_by_id", "is_alive")]

    def test_a_refusal_can_be_retried_once_the_handle_resolves(self, relay, terms, monkeypatch,
                                                             capsys):
        """The field is kept precisely so the user can retry after fixing."""
        self._incident(relay, monkeypatch)
        terms.dead_handles.add("w0t0p0:OLD")
        with pytest.raises(SystemExit):
            run_main(relay, "close-predecessor")
        terms.dead_handles.clear()
        run_main(relay, "close-predecessor")
        assert [c["handle"] for c in terms.closes] == ["w0t0p0:OLD"]
        assert "closed its tab" in capsys.readouterr().out
        assert "predecessor" not in relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-new")

    def test_the_callers_own_tab_is_a_hard_stop_even_when_it_resolves(self, relay, terms,
                                                                      monkeypatch):
        """Belt and braces: a predecessor record naming the CALLER's own tab (same iTerm session
        UUID; the w#t#p# prefix is only a position) refuses with its own message, although every
        other check — recorded, resolvable, predecessor stepped down — would pass."""
        pred = self._incident(relay, monkeypatch, iterm_session="w3t4p0:NEW")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "close-predecessor")
        assert "is THIS lead's own tab; nothing closed" in str(e.value)
        assert "not found" not in str(e.value)
        self._assert_nothing_touched(relay, terms, pred)
        assert ledger_events(relay, "predecessor_closed") == []

    def test_the_tab_the_caller_runs_in_is_a_hard_stop_too(self, relay, terms, monkeypatch):
        """Same stop when the caller's marker has no handle but the process runs in that tab."""
        pred = {"session_id": "old", "tab_label": "[Lead] x", "iterm_session": "w0t0p0:HERE",
                "backend": "iterm"}
        arm_lead(relay, "lead-new", "x", predecessor=pred, backend="iterm")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-new")
        monkeypatch.setenv("TERM_SESSION_ID", "w1t2p0:HERE")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "close-predecessor")
        assert "is THIS lead's own tab" in str(e.value)
        assert terms.closes == []

    def test_same_titles_with_distinct_resolvable_handles_close_only_the_predecessor(
            self, relay, terms, monkeypatch, capsys):
        """The happy path under the incident's titles: the predecessor's handle resolves, so it —
        and only it — is closed, by id, and the field is cleared exactly as before."""
        self._incident(relay, monkeypatch)
        run_main(relay, "close-predecessor")
        assert terms.closes == [{"label": None, "handle": "w0t0p0:OLD", "pid": None, "by_id": True}]
        assert terms.tty_lookups == ["w0t0p0:OLD"]      # the pid lookup used the SAME handle
        assert "closed its tab" in capsys.readouterr().out
        assert "predecessor" not in relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-new")
        rec = ledger_events(relay, "predecessor_closed")
        assert len(rec) == 1 and rec[0]["tab_closed"] is True


# ── nudge-lead ──────────────────────────────────────────────────────────────────────────────────

class TestNudgeLead:
    """cmd_nudge_lead: "ALWAYS sends, unconditionally … there is no busy/stale guard here any more.
    The only refusal left is a dead tab." And: it sends "via the marker's OWN recorded backend
    (`term_backend`), never the caller's ambient guess — Defect A"."""

    def test_it_types_into_the_leads_own_tab(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp", iterm_session="w0t0p0:LEAD", backend="iterm")
        run_main(relay, "nudge-lead", "lead-1", "e1 reported")
        assert terms.sends == [{"label": "[Lead] webapp", "prompt": "e1 reported",
                                "handle": "w0t0p0:LEAD", "pid": None}]
        assert "nudged: sent message to idle lead 'lead-1'" in capsys.readouterr().out

    def test_it_uses_the_markers_recorded_backend(self, relay, terms, monkeypatch):
        """Defect A: an executor under Terminal.app must still address an iTerm-hosted lead
        through iTerm."""
        arm_lead(relay, "lead-1", "webapp", iterm_session="w0t0p0:LEAD", backend="iterm")
        monkeypatch.setattr(relay, "iterm", relay.backend.by_name("terminal"))   # ambient guess
        run_main(relay, "nudge-lead", "lead-1", "wake up")
        assert [n for op, n in terms.ops if op == "send"] == ["iterm"]

    def test_an_unknown_lead_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "nudge-lead", "nobody", "hi")
        assert "no such lead: nobody" in str(e.value)

    def test_a_dead_tab_reports_no_live_tab(self, relay, terms):
        arm_lead(relay, "lead-1", "webapp", backend="iterm")
        terms.send_ok = False
        terms.alive = False
        with pytest.raises(SystemExit) as e:
            run_main(relay, "nudge-lead", "lead-1", "hi")
        assert str(e.value).startswith("no-live-tab:")

    def test_a_live_but_uninjectable_tab_is_a_different_failure(self, relay, terms):
        """§9.6a: "A dead tab and a live-but-uninjectable one are DIFFERENT failures and must not
        report the same way" — terminal_app.send is unconditionally False by design."""
        arm_lead(relay, "lead-1", "webapp", backend="iterm")
        terms.send_ok = False
        terms.alive = True
        with pytest.raises(SystemExit) as e:
            run_main(relay, "nudge-lead", "lead-1", "hi")
        assert str(e.value).startswith("cannot-inject:")
        assert "cannot type into a running process" in str(e.value)

    def test_a_marker_with_no_backend_is_probed_not_guessed(self, relay, terms, monkeypatch):
        """"A marker with no recorded backend (every lead armed before D2) gets probed
        (_probe_backend_for_tab) rather than guessed"."""
        terms.alive_by = {"iterm": False, "terminal": True}   # only Terminal.app has the tab
        arm_lead(relay, "lead-1", "webapp")                      # no backend field
        monkeypatch.setattr(relay, "iterm", relay.backend.by_name("iterm"))
        run_main(relay, "nudge-lead", "lead-1", "hi")
        assert [n for op, n in terms.ops if op == "send"] == ["terminal"]


# ── focus ───────────────────────────────────────────────────────────────────────────────────────

class TestFocus:
    """focus SKILL.md: "**Works for executors AND leads** … If an executor's tab is closed, relay
    points you to `/relay:resume` … or `/relay:restart`; if a lead's tab can't be matched,
    re-running `/relay:mode` in that lead resets its title"."""

    def test_it_focuses_an_executor_by_label_and_handle(self, relay, terms, capsys):
        make_session(relay, "e1")
        run_main(relay, "focus", "e1")
        assert terms.focuses == [{"label": "[Exec] e1", "handle": "w0t0p0:STUB", "pid": None}]
        assert "focused 'e1' (tab '[Exec] e1')" in capsys.readouterr().out

    def test_a_gone_executor_tab_points_at_resume_and_restart(self, relay, terms):
        make_session(relay, "e1")
        terms.focus_ok = False
        with pytest.raises(SystemExit) as e:
            run_main(relay, "focus", "e1")
        msg = str(e.value)
        assert "could not focus 'e1'" in msg
        assert "relay resume e1" in msg and "relay restart e1" in msg

    def test_it_focuses_a_lead_by_its_marker_identity(self, relay, terms, capsys):
        """cmd_focus: "Identity first, exactly like the executor branch above: the marker's recorded
        backend and handle, not the label alone. Two leads can share a tab title"."""
        arm_lead(relay, "lead-1", "webapp", iterm_session="w0t0p0:LEAD", backend="iterm")
        run_main(relay, "focus", "lead-1")
        assert terms.focuses == [{"label": "[Lead] webapp", "handle": "w0t0p0:LEAD", "pid": None}]
        assert "focused lead 'webapp'" in capsys.readouterr().out

    def test_a_gone_lead_tab_points_at_relay_mode(self, relay, terms):
        arm_lead(relay, "lead-1", "webapp", backend="iterm")
        terms.focus_ok = False
        with pytest.raises(SystemExit) as e:
            run_main(relay, "focus", "lead-1")
        assert "re-run /relay:mode to reset it" in str(e.value)

    def test_an_unknown_session_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "focus", "nobody")
        assert "no such session: nobody" in str(e.value)

    def test_a_lead_can_be_focused_by_project_name(self, relay, terms, capsys):
        """README: "Anywhere a command above takes a session id, you can pass … a lead's project
        name … `relay focus webapp` … just works"."""
        arm_lead(relay, "11111111-2222-3333-4444-555555555555", "webapp", backend="iterm")
        run_main(relay, "focus", "webapp")
        assert "focused lead 'webapp'" in capsys.readouterr().out


# ── whoami ──────────────────────────────────────────────────────────────────────────────────────

class TestWhoami:
    """cmd_whoami: "`<token>` may be a lead's session id, an executor's relay name, or an
    executor's claude_session uuid; omitted, it falls back to $CLAUDE_CODE_SESSION_ID"."""

    def test_a_lead_token_reports_its_project_and_its_executors(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp", iterm_session="w0:L", backend="iterm")
        make_session(relay, "e1", owner_lead="lead-1", status="reported")
        make_session(relay, "e2", owner_lead="lead-other")
        run_main(relay, "whoami", "lead-1", "--json")
        data = json.loads(capsys.readouterr().out)
        assert data["role"] == "lead" and data["name"] == "webapp"
        assert data["backend"] == "iterm" and data["tab_label"] == "[Lead] webapp"
        assert data["execs"] == [{"name": "e1", "status": "reported"}]

    def test_an_executor_name_reports_its_lead_and_report_path(self, relay, terms, capsys):
        arm_lead(relay, "lead-1", "webapp", iterm_session="w0:L", backend="iterm")
        make_session(relay, "e1", owner_lead="lead-1", status="reported", current_packet=2)
        run_main(relay, "whoami", "e1", "--json")
        data = json.loads(capsys.readouterr().out)
        assert data["role"] == "executor" and data["name"] == "e1"
        assert data["session"] == "cs-e1" and data["current_packet"] == 2
        assert data["report_path"] == str(relay.packets_dir("e1") / "002-report.md")
        assert data["report_exists"] is True
        assert data["owner_lead"] == "lead-1" and data["lead_backend"] == "iterm"

    def test_an_executors_claude_uuid_resolves_to_its_relay_name(self, relay, terms, capsys):
        """_resolve_whoami: "`name` is the canonical relay identifier … NOT its claude_session
        uuid"."""
        make_session(relay, "e1", claude_session="11111111-2222-3333-4444-555555555555")
        run_main(relay, "whoami", "11111111-2222-3333-4444-555555555555", "--json")
        data = json.loads(capsys.readouterr().out)
        assert data["name"] == "e1" and data["role"] == "executor"

    def test_it_falls_back_to_the_env_session_id(self, relay, terms, monkeypatch, capsys):
        make_session(relay, "e1", claude_session="cs-abc")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cs-abc")
        run_main(relay, "whoami", "--json")
        assert json.loads(capsys.readouterr().out)["name"] == "e1"

    def test_no_token_and_no_env_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "whoami")
        assert "no token given and $CLAUDE_CODE_SESSION_ID is not set" in str(e.value)

    def test_an_unresolvable_token_is_refused(self, relay, terms):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "whoami", "not-anything")
        assert "could not resolve 'not-anything'" in str(e.value)

    def test_an_unowned_executor_prints_a_dash(self, relay, terms, capsys):
        make_session(relay, "e1", owner_lead=None)
        run_main(relay, "whoami", "e1")
        assert "lead       : -  (unowned)" in capsys.readouterr().out

    def test_a_lead_project_name_resolves(self, relay, terms, capsys):
        """README, right under the command table (which lists `relay whoami [<token>]`):
        "Anywhere a command above takes a session id, you can pass the executor's name …, a lead's
        project name, or a unique prefix of either's id — no more pasting lead UUIDs."
        main()'s RESOLVE_FIELDS names two deliberate exclusions — `lead-start` and `handoff` —
        and whoami is not one of them; it simply is not in the table."""
        arm_lead(relay, "11111111-2222-3333-4444-555555555555", "webapp")
        run_main(relay, "whoami", "webapp", "--json")
        assert json.loads(capsys.readouterr().out)["role"] == "lead"


class TestHandoffInheritsTheCallersModel:
    """Row 94/96: a handoff successor must run on the SAME model the outgoing lead runs on NOW —
    the CLI's stamp on its last assistant turn (live) — falling back to the marker's arm-time model
    only when the transcript has no turn, and to the CLI default only after both."""

    def _doc(self, tmp_path):
        p = tmp_path / "handoff.md"; p.write_text("# Handoff\n\nnext steps\n"); return str(p)

    def _run(self, relay, terms, tmp_path, monkeypatch, marker_model, live_model, *extra):
        from unittest import mock
        arm_lead(relay, "lead-old", "webapp", model=marker_model, iterm_session="w0t0p0:OLD",
                 backend="iterm", started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-old")
        with mock.patch.object(relay, "_lead_live_model", return_value=live_model):
            run_main(relay, "handoff", self._doc(tmp_path), *extra)
        return terms.spawns[0].get("model")

    def test_live_model_wins_over_marker(self, relay, terms, tmp_path, monkeypatch):
        """Row 96: a lead armed on opus that switched with `/model sonnet` hands off a SONNET lead,
        not the arm-time opus its marker still says."""
        assert self._run(relay, terms, tmp_path, monkeypatch, "opus", "claude-sonnet-5") == "claude-sonnet-5"

    def test_marker_model_used_when_no_live_turn(self, relay, terms, tmp_path, monkeypatch):
        assert self._run(relay, terms, tmp_path, monkeypatch, "opus", None) == "opus"

    def test_live_model_used_when_marker_has_none(self, relay, terms, tmp_path, monkeypatch):
        assert self._run(relay, terms, tmp_path, monkeypatch, None, "claude-fable-5-1") == "claude-fable-5-1"

    def test_explicit_flag_wins_over_both(self, relay, terms, tmp_path, monkeypatch):
        assert self._run(relay, terms, tmp_path, monkeypatch, "opus", "claude-fable-5-1",
                         "--model", "sonnet") == "sonnet"


class TestLineupReadsThisMachineNotAStaticList:
    """Row 96: `relay lineup` builds the spoken model check from what THIS machine's Claude Code
    resolves (the spawn resolver) and from the lead's live model — never from TIER_ORDER recited as
    fact. A class the CLI rejects as unrecognised is absent from above/below/executors."""

    def _resolver(self, table):
        def resolve(alias):
            v = table.get(alias)
            if v is None:
                raise ValueError(f"model '{alias}' is not recognised by this Claude Code (test)")
            if v == "?":
                return alias, "unresolved: probe timed out"
            return v, "cache"
        return resolve

    def _lineup(self, relay, monkeypatch, capsys, table, live, marker_model=None, *extra):
        arm_lead(relay, "lead-1", "webapp", model=marker_model)
        monkeypatch.setattr(relay, "resolve_model_for_launch", self._resolver(table))
        with mock.patch.object(relay, "_lead_live_model", return_value=live):
            run_main(relay, "lineup", "--session", "lead-1", "--json", *extra)
        return json.loads(capsys.readouterr().out)

    FULL = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5", "opus": "claude-opus-5", "fable": "claude-fable-5-1"}

    def test_no_fable_here_means_opus_is_the_top(self, relay, monkeypatch, capsys):
        table = dict(self.FULL); del table["fable"]
        d = self._lineup(relay, monkeypatch, capsys, table, "claude-opus-5")
        assert d["class"] == "opus" and d["above"] == [] and d["below"] == ["haiku", "sonnet"]
        assert "strongest class available on this machine" in d["model_check"]
        assert "fable" not in d["model_check"].lower() and d["stop"] is False
        assert [c["state"] for c in d["classes"]] == ["available", "available", "available", "unavailable"]

    def test_fable_here_means_opus_has_one_above(self, relay, monkeypatch, capsys):
        d = self._lineup(relay, monkeypatch, capsys, self.FULL, "claude-opus-5")
        assert d["above"] == ["fable"] and "one stronger class (fable)" in d["model_check"]

    def test_executor_range_is_what_resolves_within_the_ceiling(self, relay, monkeypatch, capsys):
        table = {"sonnet": "claude-sonnet-5", "opus": "claude-opus-5", "fable": "claude-fable-5-1"}  # no haiku
        d = self._lineup(relay, monkeypatch, capsys, table, "claude-fable-5-1")
        assert d["executors"] == ["sonnet", "opus"]  # ceiling default opus, haiku absent here
        assert "executors available here: sonnet, opus." in d["model_check"]

    def test_bottom_of_what_is_available_stops(self, relay, monkeypatch, capsys):
        table = {"sonnet": "claude-sonnet-5", "opus": "claude-opus-5"}
        d = self._lineup(relay, monkeypatch, capsys, table, "claude-sonnet-5")
        assert d["below"] == [] and d["stop"] is True and "/model opus" in d["model_check"]

    def test_two_above_recommends_the_strongest_available(self, relay, monkeypatch, capsys):
        d = self._lineup(relay, monkeypatch, capsys, self.FULL, "claude-sonnet-5")
        assert d["above"] == ["opus", "fable"] and "/model fable" in d["model_check"] and d["stop"] is False

    def test_live_model_beats_marker_and_unknown_probe_is_not_available(self, relay, monkeypatch, capsys):
        table = dict(self.FULL); table["fable"] = "?"
        d = self._lineup(relay, monkeypatch, capsys, table, "claude-opus-5", "claude-sonnet-5")
        assert d["model"] == "claude-opus-5" and d["model_source"] == "live transcript"
        assert d["above"] == [] and d["classes"][3]["state"] == "unknown"

    def test_text_output_leads_with_the_model_check_line(self, relay, monkeypatch, capsys):
        arm_lead(relay, "lead-1", "webapp")
        monkeypatch.setattr(relay, "resolve_model_for_launch", self._resolver(self.FULL))
        with mock.patch.object(relay, "_lead_live_model", return_value="claude-fable-5-1"):
            run_main(relay, "lineup", "--session", "lead-1")
        out = capsys.readouterr().out.splitlines()
        assert out[0].startswith("Model check: claude-fable-5-1 — the strongest class available")
        assert any(l.strip().startswith("fable") and "available → claude-fable-5-1" in l for l in out)


class TestLeadStartRecordsTheLiveModelFirst:
    """Row 96: a re-arm after `/model` must record the model the lead runs on NOW, not the marker's
    arm-time value (which lead-start used to prefer)."""

    def test_live_beats_existing_marker(self, relay, terms, monkeypatch):
        arm_lead(relay, "lead-1", "webapp", model="opus")
        with mock.patch.object(relay, "_lead_live_model", return_value="claude-sonnet-5"):
            run_main(relay, "lead-start", "lead-1", "--no-rename")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")["model"] == "claude-sonnet-5"

    def test_marker_kept_when_no_live_turn_yet(self, relay, terms, monkeypatch):
        arm_lead(relay, "lead-1", "webapp", model="claude-fable-5-1")
        with mock.patch.object(relay, "_lead_live_model", return_value=None):
            run_main(relay, "lead-start", "lead-1", "--no-rename")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")["model"] == "claude-fable-5-1"
