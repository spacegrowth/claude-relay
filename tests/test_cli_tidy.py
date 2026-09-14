"""
Unit tests for `relay tidy` and the automatic post-spawn/handoff/resume tidy (backlog rows 63-64):
the desired tab ORDER computed from state ([Lead] [Exec 1] [Exec 2] … [Lead 2] [Exec 2.1] …), and
the rule that a lead and every executor it CURRENTLY owns wear one color.

No real terminal, no real `claude`, no real iTerm: `iterm.reorder_tabs` — the only code path that
can move a human's real tabs — is stubbed at the backend seam alongside every other entry point
(see FakeTerm), and tests/conftest.py sets RELAY_NO_TIDY=1 for the whole suite as a second net.
The tests here that assert the automatic tidy fired must delete that variable themselves, and only
ever do so with the seam already stubbed.

Run: pytest tests/test_cli_tidy.py -q
"""
import contextlib
import importlib.machinery
import importlib.util
import io
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
    mod._probe_model = lambda alias: (None, "disabled in tests")
    mod._cli_version = lambda: "test"
    _orig_read_pid, _orig_read_iterm_id, _orig_read_iterm_id_at = (
        mod.read_pid, mod.read_iterm_id, mod.read_iterm_id_at)
    mod.read_pid = lambda session_id, timeout=0.5: _orig_read_pid(session_id, timeout)
    mod.read_iterm_id = lambda session_id, timeout=0.5: _orig_read_iterm_id(session_id, timeout)
    mod.read_iterm_id_at = lambda path, timeout=0.5: _orig_read_iterm_id_at(path, timeout)
    return mod


# `reorder_tabs` is in this list for the same reason every other entry point is: relay reaches the
# REAL scripts/iterm.py through `iterm_backend` (tidy is iTerm-specific by design), so a stub on
# `relay.iterm` alone would not stop a tidy from reaching the live iTerm2 Python API.
BACKEND_ENTRY_POINTS = ("spawn", "send", "close", "focus", "is_alive", "rename_by_id",
                        "tty_by_id", "pid_on_tty", "title_by_id", "live_session_names",
                        "reorder_tabs", "exists_by_id", "close_by_id")


@pytest.fixture(autouse=True)
def _pristine_backends():
    """Hard guarantee that no test leaves a stub on a REAL backend module — see the identical
    fixture in tests/test_cli_lifecycle.py for the leak this prevents."""
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

    Beyond tests/test_cli_lifecycle.py's copy it records two more things this file is about:
    `reorder_tabs` calls (the tab-order seam) and the tab-color escapes relay writes to a tab's
    tty (`ttys` maps a handle to a real file relay may open, so `_paint_tab` can be observed
    without a real terminal anywhere near it)."""

    def __init__(self):
        self.spawns, self.sends, self.closes, self.focuses, self.renames = [], [], [], [], []
        self.ops = []
        self.send_ok = True
        self.close_ok = True
        self.focus_ok = True
        self.alive = True
        self.alive_by = {}
        self.spawn_error = None
        self.titles = {}
        self.ttys = {}             # handle -> a real path relay's tab painter may write to
        self.reorders = []         # every reorder_tabs call, as the handle list it was given
        self.dry_runs = []         # row 89: every dry_run=True call — never a move, kept apart
        self.reorder_kw = []       # row 89: the tty_hints/follows each call carried
        self.reorder_result = (True, "moved 3 tab(s) in 1 window(s)")
        # Row 89: when set, a LIVE tab bar — windows, each a list of tabs, each a list of bare
        # session ids — that reorder_tabs diffs the plan against (and, unless dry_run, rearranges)
        # with iterm_pyapi's own pure helpers, so a moved-count is computed rather than scripted.
        self.live = None
        # Row 69: an optional QUEUE of results, one popped per call, for the retry tests — a
        # single `reorder_result` cannot express "fails, then succeeds". Empty/None falls back to
        # `reorder_result`, so every existing test is unaffected.
        self.reorder_results = None
        self._name = None

    @contextlib.contextmanager
    def _as(self, name):
        prev, self._name = self._name, name
        try:
            yield
        finally:
            self._name = prev

    def for_backend(self, name):
        def bind(method):
            def call(*a, **kw):
                with self._as(name):
                    return method(*a, **kw)
            return call
        return SimpleNamespace(**{n: bind(getattr(self, n)) for n in BACKEND_ENTRY_POINTS})

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
        self.alive = True
        return {"ok": True, "session_id": "w0t0p0:STUB"}

    def send(self, label, prompt, handle=None, pid=None):
        self.sends.append({"label": label, "prompt": prompt, "handle": handle, "pid": pid})
        return self.send_ok

    def close(self, label, handle=None, pid=None):
        self.closes.append({"label": label, "handle": handle, "pid": pid})
        return self.close_ok

    def close_by_id(self, handle):
        self.closes.append({"label": None, "handle": handle, "pid": None})
        return self.close_ok

    def exists_by_id(self, handle):
        return bool(handle) and self.alive_by.get(self._name, self.alive)

    def focus(self, label, handle=None, pid=None):
        self.focuses.append({"label": label, "handle": handle, "pid": pid})
        return self.focus_ok

    def is_alive(self, label, handle=None, pid=None):
        return self.alive_by.get(self._name, self.alive)

    def rename_by_id(self, handle, new_name):
        self.renames.append({"handle": handle, "name": new_name})
        return True

    def tty_by_id(self, handle):
        return self.ttys.get(handle)

    def pid_on_tty(self, tty, binary_suffix=None):
        return None

    def title_by_id(self, handle):
        return self.titles.get(handle)

    def live_session_names(self):
        return set(self.titles.values())

    def reorder_tabs(self, window_ordering, timeout=None, tty_hints=None, follows=None,
                     dry_run=False):
        self.reorder_kw.append({"tty_hints": tty_hints, "follows": follows})
        if self.live is not None:
            return self._live_reorder(window_ordering, follows, dry_run)
        if dry_run:
            self.dry_runs.append(list(window_ordering))
            return (True, "would move 0 tab(s) in 0 window(s)")
        self.reorders.append(list(window_ordering))
        if self.reorder_results:
            return self.reorder_results.pop(0)
        return self.reorder_result


def _fake_live_reorder(self, window_ordering, follows, dry_run):
    pyapi = sys.modules["iterm_pyapi"]
    (self.dry_runs if dry_run else self.reorders).append(list(window_ordering))
    ids = [str(x).split(":")[-1] for x in window_ordering]
    anchors = {str(k).split(":")[-1]: str(v).split(":")[-1] for k, v in (follows or {}).items()}
    tabs = windows = 0
    matched = []
    for wi, window in enumerate(self.live):
        order, here = pyapi._window_tab_order([[t] for t in window], ids, anchors)
        matched += here
        n = pyapi._moved_count(order)
        if here and n:
            tabs, windows = tabs + n, windows + 1
            if not dry_run:
                self.live[wi] = [window[i] for i in order]
    if not matched:
        return (False, "no iTerm tab found for any of the ordered session ids")
    return (True, f"{'would move' if dry_run else 'moved'} {tabs} tab(s) in {windows} window(s)")


FakeTerm._live_reorder = _fake_live_reorder


@pytest.fixture
def terms(relay):
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
def tidy_on(relay, monkeypatch):
    """Let the AUTOMATIC tidy actually run in a test. Safe only because `terms` has already
    stubbed `reorder_tabs` and `tty_by_id` on every backend module — the same bargain
    tests/conftest.py describes for RELAY_NO_NOTIFY.

    Row 73: the automatic tidy now shells out to `<relay> tidy --quiet --lead <sid>` as a FRESH
    SUBPROCESS. A test must never let that reach a real interpreter — it would load the REAL
    bin/relay, unpatched, and could touch the human's actual iTerm. So this stubs `subprocess.run`
    at the module seam `maybe_tidy_tabs` calls through: instead of launching a process, it re-enters
    this SAME already-patched module's `main()` in-process with the given argv. The "child" sees
    the identical FakeTerm and ledger file the parent does — exactly what a real subprocess would
    see on disk — and its stdout is captured instead of printed, exactly as `capture_output=True`
    would capture a real child's stdout.

    Bug fixed after row 73 landed: `relay.subprocess` IS the shared stdlib `subprocess` module
    (`lead_guard.subprocess is subprocess` is True — it is not a relay-local alias), so patching
    `.run` here used to replace it PROCESS-WIDE for the life of every `tidy_on` test — hijacking
    every unrelated `subprocess.run` call (`lead_guard`'s `ps -o lstart=` pid-staleness check,
    `git rev-parse`, `open`, `claude --version`, ...), silently re-interpreting each one as a
    `relay <argv>` invocation and feeding the calling code a bogus exit-2 result. `fake_run` below
    now recognizes ONLY the exact tidy invocation `maybe_tidy_tabs` builds — a cmd list whose first
    three elements are `[sys.executable, RELAY_BIN, "tidy"]` — and delegates every other call,
    args and kwargs untouched, to the REAL `subprocess.run` captured before the patch is applied."""
    real_run = relay.subprocess.run

    def fake_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if list(cmd[:3]) != [sys.executable, relay.RELAY_BIN, "tidy"]:
            return real_run(*args, **kwargs)   # not a tidy call — untouched, real subprocess.run
        argv = list(cmd[2:])   # cmd is [sys.executable, RELAY_BIN, "tidy", ...]
        buf = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["relay", *argv]
        code = 0
        try:
            with contextlib.redirect_stdout(buf):
                try:
                    relay.main()
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        finally:
            sys.argv = old_argv
        return subprocess.CompletedProcess(cmd, code, stdout=buf.getvalue(), stderr="")
    monkeypatch.setattr(relay.subprocess, "run", fake_run)
    monkeypatch.delenv("RELAY_NO_TIDY", raising=False)


def run_main(relay, *argv):
    with mock.patch.object(sys, "argv", ["relay", *argv]):
        relay.main()


def arm_lead(relay, sid, project="proj", **kw):
    relay.lead_guard.write_marker(relay.STATE_ROOT, sid, project=project, cwd="/tmp",
                                  tab_label=f"[Lead] {project}", backend="iterm", **kw)
    return relay.lead_guard.read_marker(relay.STATE_ROOT, sid)


def make_exec(relay, sid, owner_lead, handle, spawned=None, status="busy", **over):
    """An executor record in the shape cmd_spawn writes, plus the ledger `spawned` event tidy
    orders a lead's executors by."""
    s = {"session_id": sid, "owner_lead": owner_lead, "owner_project": "proj",
         "worktree": str(relay.STATE_ROOT.parent), "topic": sid, "scope": sid,
         "tab_label": f"[Exec] {sid}", "model": "claude-sonnet-5", "mcp": "none", "keep": False,
         "context": "200k", "agent": None, "effort": None, "pid": None, "pid_started": None,
         "iterm_session": handle, "backend": "iterm", "claude_session": f"cs-{sid}",
         "status": status, "current_packet": 1, "busy_since": relay.now(),
         "busy_since_epoch": time.time(), "superseded_by": None,
         "created": relay.now(), "updated": relay.now()}
    s.update(over)
    relay.write_session(sid, s)
    if spawned:
        relay.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(relay.LEDGER, "a") as f:
            f.write(json.dumps({"ts": spawned, "event": "spawned", "session_id": sid}) + "\n")
    return s


def ledger_events(relay, event=None):
    """Every ledger record (optionally just one event name), in file order."""
    if not relay.LEDGER.exists():
        return []
    recs = [json.loads(ln) for ln in relay.LEDGER.read_text().splitlines() if ln.strip()]
    return [r for r in recs if event is None or r.get("event") == event]


def cfg_write(relay, **kv):
    p = relay.lead_guard.config_path(relay.STATE_ROOT)
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(p.read_text()) if p.exists() else {}
    cur.update(kv)
    p.write_text(json.dumps(cur))


def write_packet(tmp_path, name="p.md"):
    p = tmp_path / name
    p.write_text("GOAL — do the bounded thing described here.\n\n"
                 "## Work\nEdit the module and add the missing branch, then run the suite.\n\n"
                 "## Preconditions\n- the checkout is pulled\n")
    return str(p)


def two_leads(relay):
    """Two leads, two executors each, deliberately interleaved on disk so a passing order test
    can't be an accident of `all_session_ids`' alphabetical walk."""
    arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", color=[200, 140, 135],
             started="2020-01-01T00:00:00")
    arm_lead(relay, "lead-b", "beta", iterm_session="w0t9p0:B", color=[136, 164, 198],
             started="2020-02-01T00:00:00")
    make_exec(relay, "b-one", "lead-b", "w0t7p0:B1", spawned="2020-02-02T00:00:00")
    make_exec(relay, "a-one", "lead-a", "w0t3p0:A1", spawned="2020-01-02T00:00:00")
    make_exec(relay, "b-two", "lead-b", "w0t8p0:B2", spawned="2020-02-03T00:00:00")
    make_exec(relay, "a-two", "lead-a", "w0t4p0:A2", spawned="2020-01-03T00:00:00")


# ── the desired order, computed from state ──────────────────────────────────────────────────────

class TestTidyGroups:
    """Backlog row 64: "each armed lead (oldest `started` first), immediately followed by its
    non-closed executors in spawn order (ledger `spawned` ts), then the next lead"."""

    def test_two_leads_with_two_executors_each(self, relay, terms):
        two_leads(relay)
        groups = relay.tidy_groups()
        assert [g["lead"] for g in groups] == ["lead-a", "lead-b"]
        assert relay.tidy_order(groups) == ["w0t0p0:A", "w0t3p0:A1", "w0t4p0:A2",
                                            "w0t9p0:B", "w0t7p0:B1", "w0t8p0:B2"]

    def test_executors_are_ordered_by_the_ledger_spawn_time_not_by_name(self, relay, terms):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        make_exec(relay, "aaa", "lead-a", "w0t1p0:AAA", spawned="2020-06-01T00:00:00")
        make_exec(relay, "zzz", "lead-a", "w0t2p0:ZZZ", spawned="2020-01-01T00:00:00")
        assert relay.tidy_order(relay.tidy_groups()) == ["w0t0p0:A", "w0t2p0:ZZZ", "w0t1p0:AAA"]

    def test_closed_and_superseded_and_dead_executors_are_excluded(self, relay, terms):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        make_exec(relay, "live", "lead-a", "w0t1p0:LIVE", spawned="2020-01-02T00:00:00")
        make_exec(relay, "gone", "lead-a", "w0t2p0:GONE", spawned="2020-01-03T00:00:00",
                  status="closed")
        make_exec(relay, "old", "lead-a", "w0t3p0:OLD", spawned="2020-01-04T00:00:00",
                  status="superseded")
        make_exec(relay, "rip", "lead-a", "w0t4p0:RIP", spawned="2020-01-05T00:00:00",
                  status="dead")
        assert relay.tidy_order(relay.tidy_groups()) == ["w0t0p0:A", "w0t1p0:LIVE"]

    def test_an_executor_in_another_window_is_left_where_it_is(self, relay, terms):
        """"Executors whose owner is a different window stay where they are" — reported, never
        ordered: reordering it would drag the tab into the lead's window."""
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        make_exec(relay, "same", "lead-a", "w0t1p0:SAME", spawned="2020-01-02T00:00:00")
        make_exec(relay, "other", "lead-a", "w3t0p0:OTHER", spawned="2020-01-03T00:00:00")
        groups = relay.tidy_groups()
        assert groups[0]["elsewhere"] == ["other"]
        assert relay.tidy_order(groups) == ["w0t0p0:A", "w0t1p0:SAME"]

    def test_an_unowned_executor_belongs_to_no_group(self, relay, terms):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        make_exec(relay, "orphan", None, "w0t1p0:ORPHAN", spawned="2020-01-02T00:00:00")
        assert relay.tidy_order(relay.tidy_groups()) == ["w0t0p0:A"]

    def test_a_lead_with_no_captured_tab_is_skipped_entirely(self, relay, terms):
        """No handle means no tab to anchor the group to — ordering its executors alone would move
        them to the front of the window, away from a lead relay cannot see."""
        arm_lead(relay, "lead-a", "alpha", iterm_session=None, started="2020-01-01T00:00:00")
        make_exec(relay, "e1", "lead-a", "w0t1p0:E1", spawned="2020-01-02T00:00:00")
        assert relay.tidy_groups() == []

    def test_a_broken_marker_is_skipped_not_crashed_on(self, relay, terms):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        broken = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-x")
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "marker.json").write_text("{not json")
        assert [g["lead"] for g in relay.tidy_groups()] == ["lead-a"]

    def test_tombstoned_and_migrated_husks_over_the_live_tab_are_skipped(self, relay, terms):
        """THE INCIDENT's leftovers: a hijack/exit leaves husk markers that keep the tab id they
        were armed in — the very tab the live lead now holds — so `list_leads` (a raw directory
        walk) hands tidy several markers for ONE tab. Unfiltered, each husk is its own group: the
        tab is repainted once per husk, and the husks' older lineage steals the front slot."""
        arm_lead(relay, "lead-live", "alpha", iterm_session="w0t0p0:A",
                 started="2020-06-01T00:00:00", lineage_started="2020-06-01T00:00:00")
        arm_lead(relay, "lead-dead", "alpha", iterm_session="w0t0p0:A",
                 started="2020-01-01T00:00:00", lineage_started="2020-01-01T00:00:00")
        arm_lead(relay, "lead-moved", "alpha", iterm_session="w0t0p0:A",
                 started="2020-02-01T00:00:00", lineage_started="2020-02-01T00:00:00")
        assert relay.lead_guard.tombstone_lead(relay.STATE_ROOT, "lead-dead", reason="exit")
        assert relay.lead_guard.update_marker(relay.STATE_ROOT, "lead-moved",
                                              migrated_to="lead-live")
        groups = relay.tidy_groups()
        assert [g["lead"] for g in groups] == ["lead-live"]
        assert relay.tidy_order(groups) == ["w0t0p0:A"]      # the tab is named exactly once


class TestSuccessorKeepsThePredecessorsSlot:
    """"the successor takes the predecessor's slot: order it where the predecessor was, and
    close-predecessor then leaves the successor in place"."""

    @pytest.fixture
    def handed_off(self, relay, terms, tmp_path, monkeypatch):
        arm_lead(relay, "lead-old", "alpha", iterm_session="w0t0p0:OLD",
                 color=[200, 140, 135], started="2020-01-01T00:00:00")
        arm_lead(relay, "lead-b", "beta", iterm_session="w0t9p0:B", color=[136, 164, 198],
                 started="2020-02-01T00:00:00")
        make_exec(relay, "a-one", "lead-old", "w0t3p0:A1", spawned="2020-01-02T00:00:00")
        make_exec(relay, "b-one", "lead-b", "w0t8p0:B1", spawned="2020-02-02T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-old")
        doc = tmp_path / "handoff.md"
        doc.write_text("# Handoff\n\nIn flight: a-one.\n")
        run_main(relay, "handoff", str(doc))
        return next(m["session_id"] for m in relay.lead_guard.list_leads(relay.STATE_ROOT)
                    if m["session_id"] != "lead-b")

    def test_the_successor_group_stays_first(self, relay, terms, handed_off):
        """Ordering on the successor's OWN `started` would send the whole lineage to the end of the
        tab bar — it is minutes old, while the lead it replaced was days old."""
        groups = relay.tidy_groups()
        assert [g["lead"] for g in groups] == [handed_off, "lead-b"]
        assert groups[0]["executors"][0]["sid"] == "a-one"   # the re-parented executor came along

    def test_the_lineage_start_is_the_predecessors(self, relay, terms, handed_off):
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, handed_off)
        assert m["lineage_started"] == "2020-01-01T00:00:00"
        assert m["started"] != "2020-01-01T00:00:00"          # its OWN start is still its own

    def test_a_re_arm_does_not_lose_the_lineage(self, relay, terms, handed_off, monkeypatch):
        """The successor's aftercare tells it to re-run /relay:mode, and write_marker rewrites the
        WHOLE marker — a dropped lineage would teleport the group at the next tidy."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", handed_off)
        run_main(relay, "lead-start", handed_off, "--no-rename")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, handed_off)
        assert m["lineage_started"] == "2020-01-01T00:00:00"
        assert [g["lead"] for g in relay.tidy_groups()] == [handed_off, "lead-b"]

    def test_close_predecessor_leaves_the_successor_in_place(self, relay, terms, handed_off,
                                                             monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", handed_off)
        run_main(relay, "close-predecessor")
        assert [g["lead"] for g in relay.tidy_groups()] == [handed_off, "lead-b"]

    def test_restoring_a_crashed_lead_does_not_lose_the_lineage(self, relay, terms, handed_off):
        """cmd_resume_lead rewrites the WHOLE marker too — the §1 disease its own comment block
        names. A restore is the same lead, so its slot in the tab bar is the same slot."""
        terms.alive = False          # the crash this restore is for
        run_main(relay, "resume", handed_off)
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, handed_off)
        assert m["lineage_started"] == "2020-01-01T00:00:00"
        assert [g["lead"] for g in relay.tidy_groups()] == [handed_off, "lead-b"]


# ── the command ─────────────────────────────────────────────────────────────────────────────────

class TestTidyCommand:
    def test_dry_run_prints_the_order_and_moves_nothing(self, relay, terms, capsys):
        two_leads(relay)
        run_main(relay, "tidy", "--dry-run")
        out = capsys.readouterr().out
        assert out.index("[Exec] a-one") < out.index("[Lead] beta") < out.index("[Exec] b-one")
        assert "nothing moved" in out
        assert terms.reorders == []
        assert len(terms.dry_runs) == 1   # it READ the live order (dry_run=True) and moved nothing

    def test_it_applies_the_order_through_the_backend(self, relay, terms, capsys):
        two_leads(relay)
        run_main(relay, "tidy")
        assert terms.reorders == [["w0t0p0:A", "w0t3p0:A1", "w0t4p0:A2",
                                   "w0t9p0:B", "w0t7p0:B1", "w0t8p0:B2"]]
        assert "moved 3 tab(s) in 1 window(s)" in capsys.readouterr().out

    def test_an_unavailable_api_is_reported_not_raised(self, relay, terms, capsys):
        two_leads(relay)
        terms.reorder_result = (False, "iterm2 python api unavailable (ImportError: no iterm2)")
        run_main(relay, "tidy")
        assert "not applied" in capsys.readouterr().out

    def test_nothing_to_order_is_a_clean_message(self, relay, terms, capsys):
        run_main(relay, "tidy")
        assert "nothing to order" in capsys.readouterr().out
        assert terms.reorders == []

    def test_it_reapplies_each_leads_color_to_its_whole_group(self, relay, terms, tmp_path):
        """Item 3: "tidy … re-applies every lead's colour to itself and its executors"."""
        two_leads(relay)
        for handle in ("w0t0p0:A", "w0t3p0:A1", "w0t4p0:A2", "w0t9p0:B"):
            terms.ttys[handle] = str(tmp_path / handle.replace(":", "_"))
        run_main(relay, "tidy")
        painted = {h: Path(p).read_text() for h, p in terms.ttys.items()}
        want_a = relay.iterm_backend.tab_color_escape([200, 140, 135])
        want_b = relay.iterm_backend.tab_color_escape([136, 164, 198])
        assert painted["w0t0p0:A"] == want_a
        assert painted["w0t3p0:A1"] == want_a == painted["w0t4p0:A2"]
        assert painted["w0t9p0:B"] == want_b

    def test_a_colorless_lead_is_given_a_color_and_it_is_persisted(self, relay, terms):
        """"When the owner has no colour, pick one for the owner and persist it, never leave tabs
        blank"."""
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        make_exec(relay, "e1", "lead-a", "w0t1p0:E1", spawned="2020-01-02T00:00:00")
        run_main(relay, "tidy")
        color = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-a")["color"]
        assert tuple(color) in {tuple(c) for c in relay.lead_guard.TAB_PALETTE}

    def test_tab_colors_off_paints_nothing(self, relay, terms, tmp_path):
        cfg_write(relay, tab_colors=False)
        two_leads(relay)
        terms.ttys["w0t0p0:A"] = str(tmp_path / "tty-a")
        run_main(relay, "tidy")
        assert not Path(terms.ttys["w0t0p0:A"]).exists()   # never opened, so never written
        assert terms.reorders                              # ordering is independent of coloring

    # ── row 73: --quiet and --lead, the flags the subprocess boundary is built on ──────────────

    def test_quiet_suppresses_the_listing_and_success_line_but_still_tidies(self, relay, terms,
                                                                            capsys):
        two_leads(relay)
        with pytest.raises(SystemExit) as ei:
            run_main(relay, "tidy", "--quiet")
        assert ei.value.code == 0
        assert capsys.readouterr().out == ""
        assert terms.reorders   # it still actually ran, just said nothing about it

    def test_quiet_on_failure_prints_exactly_one_plain_line_and_exits_1(self, relay, terms,
                                                                        capsys):
        two_leads(relay)
        terms.reorder_result = (False, "iterm2 python api unavailable (ImportError: no iterm2)")
        with pytest.raises(SystemExit) as ei:
            run_main(relay, "tidy", "--quiet")
        assert ei.value.code == 1
        assert capsys.readouterr().out.strip() == \
            "iterm2 python api unavailable (ImportError: no iterm2)"

    def test_quiet_with_nothing_to_order_reports_that_reason_and_exits_1(self, relay, terms,
                                                                         capsys):
        with pytest.raises(SystemExit) as ei:
            run_main(relay, "tidy", "--quiet")
        assert ei.value.code == 1
        assert capsys.readouterr().out.strip() == "no lead tabs to order"

    def test_dry_run_with_quiet_prints_nothing_and_still_moves_nothing(self, relay, terms, capsys):
        two_leads(relay)
        run_main(relay, "tidy", "--dry-run", "--quiet")   # never exits — dry-run returns plainly
        assert capsys.readouterr().out == ""
        assert terms.reorders == []

    def test_the_lead_flag_parses_and_is_a_grouping_no_op(self, relay, terms):
        """`tidy_groups()` already spans every armed lead in one pass — `--lead` is accepted for
        bookkeeping/future scoping and changes nothing about which tabs get ordered."""
        two_leads(relay)
        run_main(relay, "tidy", "--lead", "lead-a")
        assert terms.reorders == [["w0t0p0:A", "w0t3p0:A1", "w0t4p0:A2",
                                   "w0t9p0:B", "w0t7p0:B1", "w0t8p0:B2"]]

    def test_dry_run_alone_is_unchanged(self, relay, terms, capsys):
        """Acceptance: `relay tidy --dry-run` behaves exactly as it did before --quiet/--lead
        existed — this just re-confirms the plain-dry-run test above still holds post-change."""
        two_leads(relay)
        run_main(relay, "tidy", "--dry-run")
        out = capsys.readouterr().out
        assert out.index("[Exec] a-one") < out.index("[Lead] beta") < out.index("[Exec] b-one")
        assert "nothing moved" in out
        assert terms.reorders == []


# ── the automatic tidy ──────────────────────────────────────────────────────────────────────────

class TestAutomaticTidy:
    def _spawn(self, relay, tmp_path, monkeypatch, name="e1"):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        run_main(relay, "spawn", str(tmp_path), "topic", write_packet(tmp_path), "--name", name)

    def test_spawn_tidies(self, relay, terms, tidy_on, tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", color=[200, 140, 135],
                 started="2020-01-01T00:00:00")
        self._spawn(relay, tmp_path, monkeypatch)
        assert terms.reorders and terms.reorders[-1][0] == "w0t0p0:A"
        assert "w0t0p0:STUB" in terms.reorders[-1]      # the tab that just opened is in the order

    def test_an_unavailable_api_leaves_the_spawn_successful_with_one_dim_line(
            self, relay, terms, tidy_on, tmp_path, monkeypatch, capsys):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        terms.reorder_result = (False, "iterm2 python api unavailable (ImportError: no iterm2)")
        self._spawn(relay, tmp_path, monkeypatch)
        out = capsys.readouterr().out
        assert "spawned session 'e1'" in out
        assert out.count("tab tidy skipped") == 1
        assert relay.read_session("e1")["status"] == "busy"

    def test_the_tidy_tabs_config_key_turns_it_off(self, relay, terms, tidy_on, tmp_path,
                                                   monkeypatch, capsys):
        cfg_write(relay, tidy_tabs=False)
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        self._spawn(relay, tmp_path, monkeypatch)
        assert terms.reorders == []
        assert "tab tidy" not in capsys.readouterr().out   # off is silent, not a warning

    def test_relay_no_tidy_stops_it_even_with_the_config_on(self, relay, terms, tmp_path,
                                                            monkeypatch, capsys):
        """The suite-wide kill-switch (tests/conftest.py) — deliberately NOT lifted here."""
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        self._spawn(relay, tmp_path, monkeypatch)
        assert terms.reorders == []
        assert "tab tidy" not in capsys.readouterr().out

    def test_handoff_tidies(self, relay, terms, tidy_on, tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", color=[200, 140, 135],
                 started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        doc = tmp_path / "handoff.md"
        doc.write_text("# Handoff\n\nIn flight: nothing.\n")
        run_main(relay, "handoff", str(doc))
        assert terms.reorders and terms.reorders[-1] == ["w0t0p0:STUB"]

    def test_restart_tidies(self, relay, terms, tidy_on, tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", color=[200, 140, 135],
                 started="2020-01-01T00:00:00")
        make_exec(relay, "e1", "lead-a", "w0t1p0:E1", spawned="2020-01-02T00:00:00", status="dead")
        relay.packets_dir("e1").mkdir(parents=True, exist_ok=True)
        (relay.packets_dir("e1") / "001-packet.md").write_text("GOAL — packet 1.\n")
        terms.alive = False
        run_main(relay, "restart", "e1")
        assert terms.reorders


# ── backlog row 73: the automatic tidy always failed with tidy_skipped attempts=2 ───────────────

class TestAutomaticTidyRunsAsAFreshSubprocess:
    """The retry added in row 69 never helped, because both attempts fail the SAME way: the
    spawning process already holds one iTerm2 Python API connection (that's how it captured the
    new tab), and a second connection from that same process fails outright with a
    connection-shaped error. `relay tidy` by hand, in a fresh process, always works. So the
    automatic path now shells out to `<relay> tidy --quiet --lead <sid>` as a subprocess of its
    own, instead of calling `_tidy_now` in-process — these tests stub `subprocess.run` directly
    (no FakeTerm needed) to prove the boundary itself, independent of the tab-ordering tests
    above, which exercise it through the `tidy_on` fixture's in-process stand-in."""

    def test_it_spawns_a_subprocess_instead_of_calling_tidy_now_in_process(self, relay, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        monkeypatch.delenv("RELAY_NO_TIDY", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(relay.subprocess, "run", fake_run), \
             mock.patch.object(relay, "_tidy_now") as spy:
            ok = relay.maybe_tidy_tabs("spawn")
        assert ok is True
        spy.assert_not_called()                      # the in-process call path is NOT taken
        assert len(calls) == 1
        cmd, kw = calls[0]
        assert cmd == [sys.executable, relay.RELAY_BIN, "tidy", "--quiet", "--lead", "lead-a"]
        assert kw.get("timeout")                      # bounded, so a hung iTerm API can't wedge a spawn

    def test_no_caller_lead_id_omits_the_lead_flag(self, relay, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        monkeypatch.delenv("RELAY_NO_TIDY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(relay.subprocess, "run", fake_run):
            relay.maybe_tidy_tabs("spawn")
        assert calls == [[sys.executable, relay.RELAY_BIN, "tidy", "--quiet"]]

    def test_a_failing_exit_degrades_to_one_dim_line_and_never_raises(self, relay, capsys,
                                                                       monkeypatch):
        monkeypatch.delenv("RELAY_NO_TIDY", raising=False)

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="no iTerm tab found for any of the ordered session ids", stderr="")

        with mock.patch.object(relay.subprocess, "run", fake_run):
            ok = relay.maybe_tidy_tabs("spawn")     # must not raise
        assert ok is False
        out = capsys.readouterr().out
        assert out.count("tab tidy skipped") == 1
        assert "no iTerm tab found" in out

    def test_a_timeout_degrades_to_one_dim_line_and_never_raises(self, relay, capsys, monkeypatch):
        monkeypatch.delenv("RELAY_NO_TIDY", raising=False)

        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        with mock.patch.object(relay.subprocess, "run", fake_run):
            ok = relay.maybe_tidy_tabs("spawn")     # must not raise
        assert ok is False
        assert capsys.readouterr().out.count("tab tidy skipped") == 1

    def test_a_missing_relay_binary_degrades_to_one_dim_line_and_never_raises(self, relay, capsys,
                                                                              monkeypatch):
        monkeypatch.delenv("RELAY_NO_TIDY", raising=False)

        def fake_run(cmd, **kw):
            raise FileNotFoundError(2, "No such file or directory")

        with mock.patch.object(relay.subprocess, "run", fake_run):
            ok = relay.maybe_tidy_tabs("spawn")     # must not raise
        assert ok is False
        assert capsys.readouterr().out.count("tab tidy skipped") == 1

    def test_relay_no_tidy_short_circuits_before_any_subprocess_is_spawned(self, relay,
                                                                           monkeypatch):
        monkeypatch.setenv("RELAY_NO_TIDY", "1")

        def fail_if_called(*a, **kw):
            raise AssertionError("subprocess.run should never be called")

        with mock.patch.object(relay.subprocess, "run", fail_if_called):
            ok = relay.maybe_tidy_tabs("spawn")
        assert ok is False


class TestASpawnLedgersExactlyOneTidyEvent:
    """Acceptance #4: before row 73's fix, the automatic tidy called `_tidy_now` IN-PROCESS, which
    ledgers its own outcome — so if a parent-side call were ever reintroduced alongside the
    subprocess this now shells out to (which ledgers its OWN outcome via its own `cmd_tidy` run),
    one spawn would write TWO ledger events instead of one. A test that only checks "an event
    exists" cannot see that regression; this asserts the COUNT.

    `tidy_on` stands the subprocess in with a re-entrant `relay.main()` call under argv rewritten
    to the `tidy` invocation (see its docstring) — so a ledger write's `sys.argv` at call time
    tells the subprocess's call apart from a hypothetical direct call in `maybe_tidy_tabs`'s own
    frame, which would fire while `sys.argv` still read the `spawn` the test issued."""

    def _ledger_spy(self, relay, monkeypatch):
        """Wrap `_ledger_tidy` (not replace it) so every real write still happens, while recording
        `sys.argv` at each call — one entry per event ledgered, in order."""
        seen = []
        orig = relay._ledger_tidy

        def spy(*a, **kw):
            seen.append(list(sys.argv))
            return orig(*a, **kw)

        monkeypatch.setattr(relay, "_ledger_tidy", spy)
        return seen

    def test_a_successful_spawn_tidy_ledgers_exactly_one_tidy_event(self, relay, terms, tidy_on,
                                                                    tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        seen = self._ledger_spy(relay, monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        run_main(relay, "spawn", str(tmp_path), "topic", write_packet(tmp_path), "--name", "e1")
        assert len(seen) == 1                          # not two — no parent-side ledger call
        assert seen[0][:2] == ["relay", "tidy"]         # fired from the subprocess's argv, not spawn's
        assert len(ledger_events(relay, "tidy")) == 1
        assert ledger_events(relay, "tidy_skipped") == []

    def test_a_failing_spawn_tidy_ledgers_exactly_one_tidy_skipped_event(self, relay, terms,
                                                                         tidy_on, tmp_path,
                                                                         monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        terms.reorder_result = (False, "iterm2 python api unavailable (ImportError: no iterm2)")
        seen = self._ledger_spy(relay, monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        run_main(relay, "spawn", str(tmp_path), "topic", write_packet(tmp_path), "--name", "e1")
        assert len(seen) == 1                          # not two
        assert seen[0][:2] == ["relay", "tidy"]
        assert len(ledger_events(relay, "tidy_skipped")) == 1
        assert ledger_events(relay, "tidy") == []

    def test_relay_no_tidy_ledgers_no_event_for_a_spawn(self, relay, terms, tmp_path, monkeypatch):
        """RELAY_NO_TIDY (set suite-wide by tests/conftest.py, deliberately NOT lifted here) short-
        circuits `maybe_tidy_tabs` before any subprocess is spawned — no `tidy`, no `tidy_skipped`,
        nothing at all."""
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        run_main(relay, "spawn", str(tmp_path), "topic", write_packet(tmp_path), "--name", "e1")
        assert ledger_events(relay, "tidy") == []
        assert ledger_events(relay, "tidy_skipped") == []


# ── one color per lead group, always ────────────────────────────────────────────────────────────

class TestExecutorColorFollowsItsOwner:
    """Item 3: "an executor's colour is ALWAYS its CURRENT owner lead's marker colour, resolved at
    spawn/rotate/resume/restart, and re-applied to the live tab whenever ownership changes"."""

    def test_a_spawn_under_a_cleared_owner_marker_still_gets_a_color(self, relay, terms, tmp_path,
                                                                     monkeypatch):
        """The rotate case: `--lead` names the retiring session's owner, which may have stepped
        down since — its marker is `{}` and the tab used to open unpainted."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "someone-else")
        run_main(relay, "spawn", str(tmp_path), "topic", write_packet(tmp_path),
                 "--name", "e1", "--lead", "stepped-down-lead")
        color = terms.spawns[0]["tab_color"]
        assert tuple(color) in {tuple(c) for c in relay.lead_guard.TAB_PALETTE}

    def test_a_cleared_owner_is_never_resurrected_as_a_lead(self, relay, terms, tmp_path,
                                                            monkeypatch):
        """Persisting the picked color must not write a marker for a non-lead — that would make a
        dead session read as armed everywhere `is_lead` is asked."""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "someone-else")
        run_main(relay, "spawn", str(tmp_path), "topic", write_packet(tmp_path),
                 "--name", "e1", "--lead", "stepped-down-lead")
        assert not relay.lead_guard.is_lead(relay.STATE_ROOT, "stepped-down-lead")
        assert relay.lead_guard.list_leads(relay.STATE_ROOT) == []

    def test_an_owner_with_no_color_gets_one_picked_and_persisted_at_spawn(self, relay, terms,
                                                                           tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        run_main(relay, "spawn", str(tmp_path), "topic", write_packet(tmp_path), "--name", "e1")
        marker_color = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-a")["color"]
        assert terms.spawns[0]["tab_color"] == marker_color
        assert tuple(marker_color) in {tuple(c) for c in relay.lead_guard.TAB_PALETTE}

    def test_a_reparent_at_handoff_recolors_the_executors_tab(self, relay, terms, tmp_path,
                                                              monkeypatch):
        arm_lead(relay, "lead-old", "alpha", iterm_session="w0t0p0:OLD", color=[200, 140, 135],
                 started="2020-01-01T00:00:00")
        make_exec(relay, "e1", "lead-old", "w0t1p0:E1", spawned="2020-01-02T00:00:00")
        terms.ttys["w0t1p0:E1"] = str(tmp_path / "tty-e1")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-old")
        doc = tmp_path / "handoff.md"
        doc.write_text("# Handoff\n\nIn flight: e1.\n")
        run_main(relay, "handoff", str(doc))
        successor = relay.read_session("e1")["owner_lead"]
        want = relay.lead_guard.read_marker(relay.STATE_ROOT, successor)["color"]
        assert Path(terms.ttys["w0t1p0:E1"]).read_text() == relay.iterm_backend.tab_color_escape(want)

    def test_an_adoption_recolors_the_executors_tab(self, relay, terms, tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", color=[200, 140, 135],
                 started="2020-01-01T00:00:00")
        arm_lead(relay, "lead-b", "beta", iterm_session="w0t9p0:B", color=[136, 164, 198],
                 started="2020-02-01T00:00:00")
        make_exec(relay, "e1", "lead-a", "w0t1p0:E1", spawned="2020-01-02T00:00:00")
        terms.ttys["w0t1p0:E1"] = str(tmp_path / "tty-e1")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-b")
        run_main(relay, "adopt", "e1", "--force")
        assert relay.read_session("e1")["owner_lead"] == "lead-b"
        assert (Path(terms.ttys["w0t1p0:E1"]).read_text()
                == relay.iterm_backend.tab_color_escape([136, 164, 198]))

    def test_a_relaunch_uses_the_current_owners_color_not_the_recorded_one(self, relay, terms,
                                                                          tmp_path, monkeypatch):
        arm_lead(relay, "lead-b", "beta", iterm_session="w0t9p0:B", color=[136, 164, 198],
                 started="2020-02-01T00:00:00")
        make_exec(relay, "e1", "lead-b", "w0t1p0:E1", spawned="2020-01-02T00:00:00", status="dead")
        relay.packets_dir("e1").mkdir(parents=True, exist_ok=True)
        (relay.packets_dir("e1") / "001-packet.md").write_text("GOAL — packet 1.\n")
        terms.alive = False
        run_main(relay, "restart", "e1")
        assert terms.spawns[0]["tab_color"] == [136, 164, 198]


# ── backlog row 69: the post-spawn tidy loses the race with iTerm ───────────────────────────────

CONN_FAIL = (False, "iterm2 python api unavailable "
                    "(ConnectionClosedError: no close frame received or sent)")
IMPORT_FAIL = (False, "iterm2 python api unavailable (ModuleNotFoundError: No module named 'iterm2')")
NOT_FOUND = (False, "no iTerm tab found for any of the ordered session ids")
REORDERED = (True, "reordered 1 window(s)")
REORDERED_PARTIAL = (True, "reordered 2 window(s); 3 session id(s) had no tab")


class TestTidyRetriesTheSpawnRace:
    """Row 69: every automatic tidy fired right after `relay spawn` failed with
    "iterm2 python api unavailable (ConnectionClosedError: no close frame received or sent)", while
    `relay tidy` typed by hand seconds later worked every time — the API websocket is opened while
    iTerm is still finishing the AppleScript tab creation. One retry, a second later, and ONLY for
    a connection-shaped failure; every attempt-reaching tidy ledgers its outcome once."""

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self, relay, monkeypatch):
        """The retry's 1 s wait, recorded instead of served — a suite that actually slept a second
        per retry test would be paying real time for a cosmetic feature."""
        self.sleeps = []
        monkeypatch.setattr(relay.time, "sleep", lambda s: self.sleeps.append(s))

    def test_a_connection_failure_is_retried_once_and_then_succeeds(self, relay, terms, tidy_on):
        two_leads(relay)
        terms.reorder_results = [CONN_FAIL, REORDERED]
        run_main(relay, "tidy")
        assert len(terms.reorders) == 2                      # exactly one retry, not a loop
        assert terms.reorders[0] == terms.reorders[1]        # the SAME order, asked again
        assert self.sleeps == [relay.TIDY_RETRY_DELAY]
        ev = ledger_events(relay, "tidy")
        assert len(ev) == 1 and ev[0]["attempts"] == 2
        assert ledger_events(relay, "tidy_skipped") == []

    def test_the_retry_succeeding_prints_success_not_the_skipped_line(self, relay, terms, tidy_on,
                                                                      capsys):
        two_leads(relay)
        terms.reorder_results = [CONN_FAIL, REORDERED]
        run_main(relay, "tidy")
        out = capsys.readouterr().out
        assert "reordered 1 window(s)" in out and "not applied" not in out

    def test_two_connection_failures_ledger_tidy_skipped_with_two_attempts(self, relay, terms,
                                                                          tidy_on, capsys):
        two_leads(relay)
        terms.reorder_results = [CONN_FAIL, CONN_FAIL]
        run_main(relay, "tidy")
        assert len(terms.reorders) == 2                      # never a third
        ev = ledger_events(relay, "tidy_skipped")
        assert len(ev) == 1 and ev[0]["attempts"] == 2
        assert "ConnectionClosedError" in ev[0]["reason"]
        assert ledger_events(relay, "tidy") == []
        assert "not applied" in capsys.readouterr().out      # the dim line, for the FINAL failure

    def test_a_session_id_not_found_result_is_never_retried(self, relay, terms, tidy_on):
        """"no tab holds these ids" is a real answer, not a race — asking again one second later
        gets the same answer for a second of dead time."""
        two_leads(relay)
        terms.reorder_results = [NOT_FOUND, REORDERED]
        run_main(relay, "tidy")
        assert len(terms.reorders) == 1
        assert self.sleeps == []
        ev = ledger_events(relay, "tidy_skipped")
        assert len(ev) == 1 and ev[0]["attempts"] == 1

    def test_a_missing_iterm2_package_is_never_retried(self, relay, terms, tidy_on):
        two_leads(relay)
        terms.reorder_results = [IMPORT_FAIL, REORDERED]
        run_main(relay, "tidy")
        assert len(terms.reorders) == 1
        assert self.sleeps == []

    def test_a_first_attempt_success_ledgers_tidy_with_one_attempt_and_the_counts(self, relay,
                                                                                  terms, tidy_on):
        two_leads(relay)
        terms.reorder_result = REORDERED_PARTIAL
        run_main(relay, "tidy")
        assert len(terms.reorders) == 1
        ev = ledger_events(relay, "tidy")
        assert len(ev) == 1
        assert ev[0]["attempts"] == 1 and ev[0]["windows"] == 2 and ev[0]["missing"] == 3

    def test_the_automatic_post_spawn_tidy_gets_the_same_retry(self, relay, terms, tidy_on,
                                                               tmp_path, monkeypatch, capsys):
        """The bug was only ever observed on the AUTOMATIC path — the retry lives in the shared
        `_tidy_now`, so proving it through `relay spawn` proves it where it actually bites."""
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        terms.reorder_results = [CONN_FAIL, REORDERED]
        run_main(relay, "spawn", str(tmp_path), "topic", write_packet(tmp_path), "--name", "e1")
        assert len(terms.reorders) == 2
        assert "tab tidy skipped" not in capsys.readouterr().out
        assert [e["attempts"] for e in ledger_events(relay, "tidy")] == [2]

    def test_the_tidy_event_names_the_lead_that_ran_it(self, relay, terms, tidy_on, monkeypatch):
        two_leads(relay)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        run_main(relay, "tidy")
        assert ledger_events(relay, "tidy")[0]["session_id"] == "lead-a"

    def test_relay_no_tidy_ledgers_nothing(self, relay, terms):
        """The suite-wide kill-switch is deliberately NOT lifted here: "tidy does not exist" must
        mean no ledger noise either."""
        two_leads(relay)
        run_main(relay, "tidy")
        assert ledger_events(relay, "tidy") == [] and ledger_events(relay, "tidy_skipped") == []

    def test_a_dry_run_never_ledgers(self, relay, terms, tidy_on):
        two_leads(relay)
        run_main(relay, "tidy", "--dry-run")
        assert terms.reorders == []
        assert ledger_events(relay, "tidy") == [] and ledger_events(relay, "tidy_skipped") == []


class TestListNamesALeadWhoseTidyKeepsBeingSkipped:
    """The dim "tab tidy skipped" line is printed once, mid-spawn, and scrolls away — so a tab bar
    that has silently stopped being tidied is invisible minutes later. `relay list` reads the
    ledger for it."""

    def _tidy_once(self, relay, terms, monkeypatch, result):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        monkeypatch.delenv("RELAY_NO_TIDY", raising=False)
        monkeypatch.setattr(relay.time, "sleep", lambda s: None)
        terms.reorder_result = result
        run_main(relay, "tidy")

    def test_a_skipped_tidy_is_named_in_list(self, relay, terms, monkeypatch, capsys):
        two_leads(relay)
        self._tidy_once(relay, terms, monkeypatch, CONN_FAIL)
        capsys.readouterr()
        run_main(relay, "list")
        out = capsys.readouterr().out
        assert "lead-a: last tab tidy skipped after 2 attempt(s)" in out
        assert "lead-b" not in out.split("last tab tidy skipped")[1].splitlines()[0]

    def test_a_later_successful_tidy_clears_the_footnote(self, relay, terms, monkeypatch, capsys):
        two_leads(relay)
        self._tidy_once(relay, terms, monkeypatch, CONN_FAIL)
        self._tidy_once(relay, terms, monkeypatch, REORDERED)
        capsys.readouterr()
        run_main(relay, "list")
        assert "last tab tidy skipped" not in capsys.readouterr().out

    def test_no_tidy_history_means_no_footnote(self, relay, terms, capsys):
        two_leads(relay)
        run_main(relay, "list")
        assert "last tab tidy skipped" not in capsys.readouterr().out


# ── the `tidy_on` fixture must not hijack unrelated subprocess.run calls ───────────────────────

class TestTidyOnFixtureDoesNotHijackOtherSubprocessCalls:
    """Permanent guard for the bug `tidy_on`'s docstring describes: `relay.subprocess` IS the
    shared stdlib `subprocess` module, so a `fake_run` that doesn't check `cmd` before acting
    replaces `.run` PROCESS-WIDE for the life of the test — silently re-interpreting every
    unrelated call (`lead_guard`'s `ps -o lstart=`, `git rev-parse`, `open`, `claude --version`,
    ...) as a `relay <argv>` invocation and feeding the caller a bogus result.

    Before the fix, this went undetected because every `tidy_on` test only ever drove the exact
    tidy invocation through `relay.subprocess.run` — nothing in the suite exercised a NON-tidy
    call while the patch was active, so the suite stayed green under the unconditionally-
    intercepting fixture (proven by the fork reviewer with a temporary probe, then deleted). This
    test IS that probe, kept.

    A cheap, real, side-effect-free command is driven straight through `relay.subprocess.run` —
    the exact seam `tidy_on` patches — and checked against evidence only the REAL implementation
    could produce. Exit 0 alone would not be enough: the pre-fix fake also happened to build a
    `SystemExit` from `relay.main()` called with no argv, and coincidence aside, a differently-
    broken fake could return 0 too. So this also pins the real stdout, which the fake can't
    fabricate."""

    def test_a_non_tidy_call_reaches_the_real_subprocess_run(self, relay, terms, tidy_on):
        result = relay.subprocess.run(["/bin/echo", "not-a-tidy-call-7f2c"],
                                       capture_output=True, text=True)
        assert result.returncode == 0                              # real exit 0, not the fake's exit 2
        assert result.stdout.strip() == "not-a-tidy-call-7f2c"     # only the real /bin/echo emits this


# ── backlog row 89: the real moved-count, stale handles, orphaned executors ─────────────────────

class TestTidyMovedCount:
    """Row 89: a spawned executor landed FIRST in its window and stayed there through every tidy,
    while `--dry-run` printed `len(tidy_order(groups))` — every managed tab — as its count. The
    count is now the plan diffed against the LIVE tab bar."""

    def test_dry_run_prints_the_real_count_and_moves_nothing(self, relay, terms, capsys):
        two_leads(relay)
        terms.live = [["B1", "A", "X", "A1", "A2", "B", "B2"]]
        run_main(relay, "tidy", "--dry-run")
        out = capsys.readouterr().out
        assert "dry run — 7 tab(s) would be reordered" in out
        assert terms.live == [["B1", "A", "X", "A1", "A2", "B", "B2"]]   # untouched
        assert terms.reorders == []

    def test_dry_run_on_a_tidy_tab_bar_says_zero(self, relay, terms, capsys):
        two_leads(relay)
        terms.live = [["A", "A1", "A2", "B", "B1", "B2", "X"]]
        run_main(relay, "tidy", "--dry-run")
        assert "dry run — 0 tab(s) would be reordered" in capsys.readouterr().out

    def test_tidy_is_idempotent(self, relay, terms, capsys, monkeypatch):
        monkeypatch.delenv("RELAY_NO_TIDY", raising=False)   # ledger is silent under the switch;
        two_leads(relay)                                       # reorder_tabs itself is stubbed
        terms.live = [["X", "B2", "A1", "B", "A", "A2", "B1"]]
        run_main(relay, "tidy")
        assert terms.live == [["A", "A1", "A2", "B", "B1", "B2", "X"]]
        assert "tidy: moved 6 tab(s) in 1 window(s)" in capsys.readouterr().out
        run_main(relay, "tidy")
        assert "tidy: moved 0 tab(s) in 0 window(s)" in capsys.readouterr().out
        assert terms.live == [["A", "A1", "A2", "B", "B1", "B2", "X"]]
        ev = ledger_events(relay, "tidy")
        assert [e["moved"] for e in ev] == [6, 0]   # B already sat at index 3
        assert ev[0]["windows"] == 1 and ev[1]["windows"] == 0
        run_main(relay, "tidy", "--dry-run")
        assert "dry run — 0 tab(s) would be reordered" in capsys.readouterr().out

    def test_the_ledger_reason_carries_the_moved_count(self, relay, terms, monkeypatch):
        monkeypatch.delenv("RELAY_NO_TIDY", raising=False)
        two_leads(relay)
        terms.reorder_result = (True, "moved 4 tab(s) in 2 window(s); 1 session id(s) had no tab")
        run_main(relay, "tidy")
        ev = ledger_events(relay, "tidy")[-1]
        assert ev["reason"].startswith("moved 4 tab(s) in 2 window(s)")
        assert (ev["moved"], ev["windows"], ev["missing"]) == (4, 2, 1)

    def test_an_old_shape_reason_still_parses(self, relay, terms, monkeypatch):
        monkeypatch.delenv("RELAY_NO_TIDY", raising=False)
        two_leads(relay)
        terms.reorder_result = (True, "reordered 2 window(s); 3 session id(s) had no tab")
        run_main(relay, "tidy")
        ev = ledger_events(relay, "tidy")[-1]
        assert (ev["moved"], ev["windows"], ev["missing"]) == (0, 2, 3)

    def test_executors_are_anchored_to_their_lead(self, relay, terms):
        two_leads(relay)
        run_main(relay, "tidy")
        assert terms.reorder_kw[-1]["follows"] == {
            "w0t3p0:A1": "w0t0p0:A", "w0t4p0:A2": "w0t0p0:A",
            "w0t7p0:B1": "w0t9p0:B", "w0t8p0:B2": "w0t9p0:B"}

    def test_the_incident_an_executor_whose_lead_tab_is_gone_is_not_hoisted_first(
            self, relay, terms):
        """The 2026-09-13 shape: the lead's recorded handle names no live tab, the executor's does.
        Pre-fix the lone matched executor was moved to index 0."""
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:STALE", color=[200, 140, 135],
                 started="2020-01-01T00:00:00")
        make_exec(relay, "e1", "lead-a", "w0t3p0:E1", spawned="2020-01-02T00:00:00")
        terms.live = [["OTHER-LEAD", "LIVE-A", "E1"]]
        run_main(relay, "tidy")
        assert terms.live == [["OTHER-LEAD", "LIVE-A", "E1"]]


class TestTidyTtyHints:
    """Row 89 root cause: an iTerm restart re-mints every tab's session UUID; the claude process
    (and its tty) survives. Hints map each handle to that tty."""

    def _sessions_dir(self, relay, tmp_path, monkeypatch, recs):
        d = tmp_path / "claude-sessions"
        d.mkdir()
        for pid, sid in recs:
            (d / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid}))
        monkeypatch.setattr(relay, "CLAUDE_SESSIONS_DIR", d)

    def test_leads_via_claude_sessions_and_executors_via_claude_session_or_pid(
            self, relay, terms, tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        make_exec(relay, "e1", "lead-a", "w0t1p0:E1", spawned="2020-01-02T00:00:00", pid=303)
        make_exec(relay, "e2", "lead-a", "w0t2p0:E2", spawned="2020-01-03T00:00:00", pid=999)
        self._sessions_dir(relay, tmp_path, monkeypatch, [(101, "lead-a"), (202, "cs-e2")])
        monkeypatch.setattr(relay, "_ttys_of_pids",
                            lambda pids: {101: "ttys001", 202: "ttys002", 303: "ttys003"})
        hints = relay._tidy_tty_hints(relay.tidy_groups())
        assert hints == {"w0t0p0:A": "ttys001", "w0t1p0:E1": "ttys003", "w0t2p0:E2": "ttys002"}

    def test_an_ambiguous_or_dead_process_gives_no_hint(self, relay, terms, tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        arm_lead(relay, "lead-b", "beta", iterm_session="w0t1p0:B", started="2020-01-02T00:00:00")
        self._sessions_dir(relay, tmp_path, monkeypatch, [(1, "lead-a"), (2, "lead-a"), (3, "lead-b")])
        monkeypatch.setattr(relay, "_ttys_of_pids", lambda pids: {1: "ttys001", 2: "ttys009"})
        assert relay._tidy_tty_hints(relay.tidy_groups()) == {}

    def test_tidy_hands_the_hints_to_the_backend(self, relay, terms, tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w0t0p0:A", started="2020-01-01T00:00:00")
        self._sessions_dir(relay, tmp_path, monkeypatch, [(101, "lead-a")])
        monkeypatch.setattr(relay, "_ttys_of_pids", lambda pids: {101: "ttys001"})
        run_main(relay, "tidy")
        run_main(relay, "tidy", "--dry-run")
        assert [kw["tty_hints"] for kw in terms.reorder_kw] == [{"w0t0p0:A": "ttys001"}] * 2

    def test_ps_output_is_parsed(self, relay, monkeypatch):
        out = " 101 ttys001\n 202 ??\n"
        monkeypatch.setattr(relay.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out))
        assert relay._ttys_of_pids([101, 202]) == {101: "ttys001"}
        assert relay._ttys_of_pids([]) == {}


class TestSpawnLeadHandle:
    """Row 89: the tab is created next to the lead's LIVE tab — the caller's own
    $ITERM_SESSION_ID when the caller is that lead — not the marker's possibly-stale handle."""

    def _spawn(self, relay, tmp_path, monkeypatch):
        run_main(relay, "spawn", str(tmp_path), "topic", write_packet(tmp_path), "--name", "e1")

    def test_the_calling_leads_own_session_id_wins(self, relay, terms, tmp_path, monkeypatch):
        arm_lead(relay, "lead-a", "alpha", iterm_session="w1t7p0:" + "0" * 36,
                 started="2020-01-01T00:00:00")
        live = "w1t1p0:3D646899-FF6B-4C79-B062-1138A605D519"
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        monkeypatch.setenv("ITERM_SESSION_ID", live)
        monkeypatch.delenv("RELAY_HEADLESS", raising=False)
        self._spawn(relay, tmp_path, monkeypatch)
        assert terms.spawns[-1]["lead_handle"] == live

    def test_another_caller_gets_the_marker_handle(self, relay, terms, tmp_path, monkeypatch):
        recorded = "w1t7p0:" + "0" * 36
        m = arm_lead(relay, "lead-a", "alpha", iterm_session=recorded)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "someone-else")
        monkeypatch.setenv("ITERM_SESSION_ID", "w1t1p0:3D646899-FF6B-4C79-B062-1138A605D519")
        assert relay._live_lead_handle("lead-a", m) == recorded

    def test_a_headless_or_malformed_env_is_ignored(self, relay, monkeypatch):
        recorded = "w1t7p0:" + "0" * 36
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-a")
        monkeypatch.setenv("ITERM_SESSION_ID", "garbage")
        monkeypatch.delenv("TERM_SESSION_ID", raising=False)
        assert relay._live_lead_handle("lead-a", {"iterm_session": recorded}) == recorded
        monkeypatch.setenv("ITERM_SESSION_ID", "w1t1p0:3D646899-FF6B-4C79-B062-1138A605D519")
        monkeypatch.setenv("RELAY_HEADLESS", "1")
        assert relay._live_lead_handle("lead-a", {"iterm_session": recorded}) == recorded
