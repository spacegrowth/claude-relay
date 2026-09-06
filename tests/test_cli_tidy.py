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
                        "reorder_tabs")


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
        self.reorder_result = (True, "reordered 1 window(s)")
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

    def reorder_tabs(self, window_ordering, timeout=None):
        self.reorders.append(list(window_ordering))
        return self.reorder_result


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
def tidy_on(monkeypatch):
    """Let the AUTOMATIC tidy actually run in a test. Safe only because `terms` has already
    stubbed `reorder_tabs` and `tty_by_id` on every backend module — the same bargain
    tests/conftest.py describes for RELAY_NO_NOTIFY."""
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

    def test_it_applies_the_order_through_the_backend(self, relay, terms, capsys):
        two_leads(relay)
        run_main(relay, "tidy")
        assert terms.reorders == [["w0t0p0:A", "w0t3p0:A1", "w0t4p0:A2",
                                   "w0t9p0:B", "w0t7p0:B1", "w0t8p0:B2"]]
        assert "reordered 1 window(s)" in capsys.readouterr().out

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
