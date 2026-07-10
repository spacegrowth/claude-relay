"""
Layer 1 (pure Python, no hooks/iTerm/API, CI-able) unit tests for the lead-mode routing gate:
the pure edit-sizing logic in lib/lead_guard.py, config fallback, grace-window and marker state,
and the bin/relay lead-start / route / close --self commands that drive them.

Run: pytest tests/test_lead_guard.py -v
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
import lead_guard as lg  # noqa: E402


def load_relay_module(state_root):
    """Same loader pattern as test_relay.py — bin/relay has no .py extension, and STATE_ROOT is
    patched to an isolated tmp dir so tests never touch ~/.relay-tasks."""
    path = str(REPO_ROOT / "bin" / "relay")
    loader = importlib.machinery.SourceFileLoader("relay_cli", path)
    spec = importlib.util.spec_from_file_location("relay_cli", path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["relay_cli"] = mod
    loader.exec_module(mod)
    mod.STATE_ROOT = state_root
    mod.LEDGER = state_root / "sessions.jsonl"
    return mod


@pytest.fixture
def relay(tmp_path):
    return load_relay_module(tmp_path / ".relay-tasks")


@pytest.fixture
def root(tmp_path):
    return tmp_path / ".relay-tasks"


# ---- edit_line_count (pure) -------------------------------------------------------------------

class TestEditLineCount:
    def test_write_counts_content_lines(self):
        assert lg.edit_line_count("Write", {"content": "a\nb\nc"}) == 3

    def test_write_single_line(self):
        assert lg.edit_line_count("Write", {"content": "one line"}) == 1

    def test_write_empty(self):
        assert lg.edit_line_count("Write", {"content": ""}) == 0

    def test_edit_counts_new_string(self):
        assert lg.edit_line_count("Edit", {"new_string": "x\ny\nz\nw"}) == 4

    def test_multiedit_sums_new_strings(self):
        ti = {"edits": [{"new_string": "a\nb"}, {"new_string": "c\nd\ne"}]}
        assert lg.edit_line_count("MultiEdit", ti) == 5

    def test_multiedit_malformed_degrades_to_zero(self):
        # Fail-open: an unexpected shape must not throw and must under-count (never over-block).
        assert lg.edit_line_count("MultiEdit", {"edits": "not-a-list"}) == 0
        assert lg.edit_line_count("MultiEdit", {"edits": [None, 42, "str"]}) == 0
        assert lg.edit_line_count("MultiEdit", {}) == 0

    def test_unknown_tool_is_zero(self):
        assert lg.edit_line_count("Bash", {"command": "rm -rf x\ny\nz"}) == 0


# ---- exceeds_gate (pure) ----------------------------------------------------------------------

class TestExceedsGate:
    def cfg(self, **over):
        c = dict(lg.LEAD_DEFAULTS)
        c.update(over)
        return c

    def test_under_threshold_not_new_passes(self):
        assert lg.exceeds_gate(10, False, self.cfg(edit_line_threshold=40)) is False

    def test_at_threshold_blocks(self):
        assert lg.exceeds_gate(40, False, self.cfg(edit_line_threshold=40)) is True

    def test_over_threshold_blocks(self):
        assert lg.exceeds_gate(41, False, self.cfg(edit_line_threshold=40)) is True

    def test_new_file_blocks_when_configured(self):
        assert lg.exceeds_gate(1, True, self.cfg(block_on_new_file=True)) is True

    def test_new_file_passes_when_disabled(self):
        assert lg.exceeds_gate(1, True, self.cfg(block_on_new_file=False)) is False


# ---- load_config ------------------------------------------------------------------------------

class TestLoadConfig:
    def test_missing_file_returns_defaults(self, root):
        assert lg.load_config(root) == lg.LEAD_DEFAULTS

    def test_partial_override_merges(self, root):
        (root / "lead").mkdir(parents=True)
        (root / "lead" / "config.json").write_text(json.dumps({"edit_line_threshold": 5}))
        cfg = lg.load_config(root)
        assert cfg["edit_line_threshold"] == 5
        assert cfg["grace_seconds"] == lg.LEAD_DEFAULTS["grace_seconds"]  # untouched key kept

    def test_corrupt_file_returns_defaults(self, root):
        (root / "lead").mkdir(parents=True)
        (root / "lead" / "config.json").write_text("{ not valid json ")
        assert lg.load_config(root) == lg.LEAD_DEFAULTS

    def test_unknown_keys_ignored(self, root):
        (root / "lead").mkdir(parents=True)
        (root / "lead" / "config.json").write_text(json.dumps({"bogus": 1, "edit_line_threshold": 7}))
        cfg = lg.load_config(root)
        assert "bogus" not in cfg
        assert cfg["edit_line_threshold"] == 7


# ---- is_new_file ------------------------------------------------------------------------------

class TestIsNewFile:
    def test_existing_file_is_not_new(self, tmp_path):
        f = tmp_path / "exists.txt"
        f.write_text("hi")
        assert lg.is_new_file({"file_path": str(f)}) is False

    def test_missing_file_is_new(self, tmp_path):
        assert lg.is_new_file({"file_path": str(tmp_path / "nope.txt")}) is True

    def test_no_path_is_not_new(self):
        assert lg.is_new_file({}) is False


# ---- marker / grace / lead state --------------------------------------------------------------

class TestLeadState:
    def test_not_lead_before_marker(self, root):
        assert lg.is_lead(root, "sess-1") is False

    def test_write_marker_makes_lead(self, root):
        lg.write_marker(root, "sess-1", model="opus")
        assert lg.is_lead(root, "sess-1") is True
        data = json.loads(lg.marker_path(root, "sess-1").read_text())
        assert data["session_id"] == "sess-1"
        assert data["model"] == "opus"

    def test_marker_records_project_cwd_and_heartbeat(self, root):
        lg.write_marker(root, "sess-1", project="webapp", cwd="/abs/path/webapp")
        data = json.loads(lg.marker_path(root, "sess-1").read_text())
        assert data["project"] == "webapp"
        assert data["cwd"] == "/abs/path/webapp"
        assert data["last_active"]  # heartbeat stamped on every write
        # read_marker surfaces the same new keys
        assert lg.read_marker(root, "sess-1")["project"] == "webapp"

    def test_marker_new_fields_default_none(self, root):
        lg.write_marker(root, "sess-1")
        data = json.loads(lg.marker_path(root, "sess-1").read_text())
        assert data["project"] is None and data["cwd"] is None

    def test_grace_window(self, root):
        lg.write_marker(root, "sess-1")
        assert lg.in_grace(root, "sess-1") is False          # none set yet
        lg.set_grace(root, "sess-1", 100, now_ts=1000.0)
        assert lg.in_grace(root, "sess-1", now_ts=1050.0) is True   # inside window
        assert lg.in_grace(root, "sess-1", now_ts=1100.0) is False  # exactly at expiry
        assert lg.in_grace(root, "sess-1", now_ts=1200.0) is False  # past expiry

    def test_clear_lead_removes_subtree(self, root):
        lg.write_marker(root, "sess-1")
        lg.set_grace(root, "sess-1", 100)
        lg.clear_lead(root, "sess-1")
        assert lg.is_lead(root, "sess-1") is False
        assert not lg.lead_dir(root, "sess-1").exists()


class TestListLeads:
    """list_leads reads every lead/*/marker.json, oldest-first by `started`, and is fully
    defensive: config.json, non-marker dirs, and malformed markers are all skipped, never fatal."""
    def _marker(self, root, sid, started, **extra):
        d = lg.lead_dir(root, sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "marker.json").write_text(json.dumps(
            {"session_id": sid, "started": started, **extra}))

    def test_empty_when_no_lead_dir(self, root):
        assert lg.list_leads(root) == []

    def test_reads_all_markers(self, root):
        self._marker(root, "a", "2026-07-07T10:00:00", project="alpha")
        self._marker(root, "b", "2026-07-07T11:00:00", project="beta")
        got = lg.list_leads(root)
        assert {m["session_id"] for m in got} == {"a", "b"}
        assert got[0]["project"] == "alpha"  # each item is the marker dict as stored

    def test_sorted_oldest_first_by_started(self, root):
        self._marker(root, "new", "2026-07-07T12:00:00")
        self._marker(root, "old", "2026-07-07T09:00:00")
        self._marker(root, "mid", "2026-07-07T10:30:00")
        assert [m["session_id"] for m in lg.list_leads(root)] == ["old", "mid", "new"]

    def test_skips_config_json(self, root):
        # lead/config.json is a file, not a lead dir — must never appear as a "lead".
        (root / "lead").mkdir(parents=True, exist_ok=True)
        lg.config_path(root).write_text(json.dumps({"grace_seconds": 60}))
        self._marker(root, "a", "2026-07-07T10:00:00")
        assert [m["session_id"] for m in lg.list_leads(root)] == ["a"]

    def test_skips_dir_without_marker(self, root):
        (lg.lead_dir(root, "no-marker")).mkdir(parents=True, exist_ok=True)
        self._marker(root, "a", "2026-07-07T10:00:00")
        assert [m["session_id"] for m in lg.list_leads(root)] == ["a"]

    def test_malformed_marker_is_skipped_not_fatal(self, root):
        d = lg.lead_dir(root, "broken"); d.mkdir(parents=True, exist_ok=True)
        (d / "marker.json").write_text("{ not json")
        self._marker(root, "good", "2026-07-07T10:00:00")
        assert [m["session_id"] for m in lg.list_leads(root)] == ["good"]

    def test_marker_missing_started_sorts_first_without_crashing(self, root):
        d = lg.lead_dir(root, "no-started"); d.mkdir(parents=True, exist_ok=True)
        (d / "marker.json").write_text(json.dumps({"session_id": "no-started"}))
        self._marker(root, "has-started", "2026-07-07T10:00:00")
        assert [m["session_id"] for m in lg.list_leads(root)] == ["no-started", "has-started"]

    def test_clear_lead_only_touches_that_session(self, root):
        lg.write_marker(root, "sess-1")
        lg.write_marker(root, "sess-2")
        lg.clear_lead(root, "sess-1")
        assert lg.is_lead(root, "sess-1") is False
        assert lg.is_lead(root, "sess-2") is True  # sibling untouched


# ---- append_ledger shape matches bin/relay ----------------------------------------------------

class TestLedger:
    def test_append_ledger_writes_record(self, root):
        lg.append_ledger(root, "blocked", session_id="sess-1", file_path="/x.py", lines=99)
        line = (root / "sessions.jsonl").read_text().strip()
        rec = json.loads(line)
        assert rec["event"] == "blocked"
        assert rec["session_id"] == "sess-1"
        assert rec["lines"] == 99
        assert "ts" in rec


class TestFindTerminalNotifier:
    """PATH-robust detection: `shutil.which` first, then standard brew locations — the fix for
    hook/launchd shells whose PATH lacks /opt/homebrew/bin (a bare which there false-negatives)."""
    def test_uses_which_when_on_path(self, monkeypatch):
        monkeypatch.setattr(lg.shutil, "which", lambda n: "/somewhere/terminal-notifier")
        assert lg.find_terminal_notifier() == "/somewhere/terminal-notifier"

    def test_falls_back_to_brew_path_when_path_misses(self, monkeypatch):
        monkeypatch.setattr(lg.shutil, "which", lambda n: None)   # PATH miss (the hook case)
        monkeypatch.setattr(lg.os, "access", lambda p, m: p == "/opt/homebrew/bin/terminal-notifier")
        assert lg.find_terminal_notifier() == "/opt/homebrew/bin/terminal-notifier"

    def test_none_when_truly_absent(self, monkeypatch):
        monkeypatch.setattr(lg.shutil, "which", lambda n: None)
        monkeypatch.setattr(lg.os, "access", lambda p, m: False)
        assert lg.find_terminal_notifier() is None


class TestIsGateExempt:
    """Packet files are the lead's OWN deliverable — the gate must never block writing them
    (before this, block_on_new_file gated every new packet the lead wrote)."""
    def test_under_state_root_exempt(self, root):
        assert lg.is_gate_exempt(root, str(root / "calc-1" / "packets" / "001-packet.md")) is True

    def test_packet_named_file_exempt_anywhere(self, root, tmp_path):
        assert lg.is_gate_exempt(root, str(tmp_path / "elsewhere" / "003-packet.md")) is True

    def test_ordinary_file_not_exempt(self, root, tmp_path):
        assert lg.is_gate_exempt(root, str(tmp_path / "cli.py")) is False

    def test_missing_or_empty_path_not_exempt(self, root):
        assert lg.is_gate_exempt(root, None) is False
        assert lg.is_gate_exempt(root, "") is False


class TestPickLeadColor:
    """pick_lead_color ensures leads get distinct colors across active leads, collision-free."""

    def test_colliding_ids_get_different_colors(self, root):
        # Two session IDs that hash to the same TAB_PALETTE slot should get different colors.
        # Use real-world colliding IDs if available, or monkeypatch lead_color to force a collision.
        import hashlib
        # Find two real IDs that hash to the same slot, or construct via monkeypatch
        sid1 = "collision-test-1"
        sid2 = "collision-test-2"

        # Monkeypatch lead_color to force both to hash to slot 0, then walk forward
        def fake_lead_color(sid):
            # Return the first color (forces collision for both)
            return list(lg.TAB_PALETTE[0])

        # Write markers for two leads; the first will claim index 0
        lg.write_marker(root, sid1, color=lg.lead_color(sid1))
        # Without the fake: sid2 should walk forward from 0 and find the first unclaimed
        color1 = lg.pick_lead_color(root, sid1)
        color2 = lg.pick_lead_color(root, sid2)

        # They should get different colors (unless all 6 are in use)
        assert color1 != color2 or len(lg.TAB_PALETTE) == 1

    def test_rearm_stability_preserves_existing_color(self, root):
        # When /relay:mode re-runs, pick_lead_color must return the SAME color, not repaint.
        sid = "stable-lead"
        original_color = [200, 140, 135]  # arbitrary choice from palette
        lg.write_marker(root, sid, color=original_color)

        # Call pick_lead_color: should return the existing color unchanged
        picked = lg.pick_lead_color(root, sid)
        assert picked == original_color

    def test_palette_exhausted_falls_back(self, root):
        # All 6 palette colors claimed by OTHER leads → pick_lead_color falls back to lead_color.
        sid = "unlucky"
        # Write 6 markers claiming all palette colors
        for i, color in enumerate(lg.TAB_PALETTE):
            lg.write_marker(root, f"lead-{i}", color=list(color))

        # Now pick_lead_color for a new lead should fall back (no error, returns lead_color result)
        picked = lg.pick_lead_color(root, sid)
        assert tuple(picked) == tuple(lg.lead_color(sid))  # fell back to hash-based
        assert picked == lg.lead_color(sid)

    def test_all_palette_entries_are_valid_tuples(self, root):
        # Verify the palette itself is well-formed: 6 entries, each a 3-tuple of ints 0-255.
        assert len(lg.TAB_PALETTE) == 6
        for color in lg.TAB_PALETTE:
            assert isinstance(color, tuple) and len(color) == 3
            for component in color:
                assert isinstance(component, int) and 0 <= component <= 255

    def test_pick_returns_list_not_tuple(self, root):
        # pick_lead_color returns [r, g, b] (list), not (r, g, b) (tuple).
        sid = "format-test"
        result = lg.pick_lead_color(root, sid)
        assert isinstance(result, list) and len(result) == 3

    def test_defensive_malformed_state_falls_back(self, root):
        # If state is corrupted (e.g., marker with invalid color), pick_lead_color still works.
        sid = "test-lead"
        d = lg.lead_dir(root, sid)
        d.mkdir(parents=True, exist_ok=True)
        # Write a broken marker
        (d / "marker.json").write_text(json.dumps({"session_id": sid, "color": "not-a-list"}))

        # Should not raise, should fall back gracefully
        picked = lg.pick_lead_color(root, sid)
        assert isinstance(picked, list) and len(picked) == 3

    def test_stale_palette_color_repicked(self, root):
        # Stale (old-palette) marker color must NOT be preserved; lead gets re-painted from current
        # palette. This handles the muted→bright transition: old leads with muted colors pick fresh.
        sid = "old-lead"
        stale_color = [1, 2, 3]  # not in current TAB_PALETTE
        # Write a marker with a stale color
        lg.write_marker(root, sid, color=stale_color)

        # pick_lead_color must NOT return the stale color; it must pick a current palette color
        picked = lg.pick_lead_color(root, sid)
        assert picked != stale_color
        assert tuple(picked) in {tuple(c) for c in lg.TAB_PALETTE}


class TestPreToolHookPacketExemption:
    """Drive hooks/pretool_route_guard.py exactly as Claude Code would (JSON on stdin, tmp HOME):
    an armed lead writing a large NEW packet file must pass silently; the same-sized ordinary new
    file must still be denied."""
    def _run(self, home, payload):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "pretool_route_guard.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)})
        return p.returncode, p.stdout

    def _payload(self, sid, file_path):
        big = "\n".join(f"line {i}" for i in range(100))  # far over edit_line_threshold
        return {"session_id": sid, "tool_name": "Write",
                "tool_input": {"file_path": file_path, "content": big}}

    def test_packet_write_passes_ungated(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        rc, out = self._run(tmp_path, self._payload("lead-1", str(root / "e1" / "packets" / "002-packet.md")))
        assert rc == 0 and out.strip() == ""          # silent allow

    def test_ordinary_large_new_file_still_denied(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        rc, out = self._run(tmp_path, self._payload("lead-1", str(tmp_path / "cli.py")))
        assert rc == 0 and '"deny"' in out            # blocked with guidance


# ---- bin/relay commands driving the above ------------------------------------------------------

class TestRelayLeadCommands:
    @pytest.fixture(autouse=True)
    def _has_terminal_notifier(self, relay, monkeypatch):
        # lead-start now HARD-requires terminal-notifier; pretend it's installed for these tests.
        monkeypatch.setattr(relay.lead_guard, "find_terminal_notifier", lambda: "/x/terminal-notifier")
        # NEVER /rename the REAL terminal these tests run in: cmd_lead_start reads TERM_SESSION_ID
        # and types an osascript /rename into the matching live iTerm session — which, unmocked, is
        # the developer's own tab running pytest (observed live: the suite renamed the user's tab
        # to 'relay-lead-test-lead-start-defaults-proje0'). Belt AND suspenders: drop the env var
        # and stub the renamer.
        monkeypatch.delenv("TERM_SESSION_ID", raising=False)
        monkeypatch.setattr(relay.iterm, "rename_by_id", lambda *a, **k: True)

    def test_lead_start_writes_marker_and_ledger(self, relay, root):
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model="opus"))
        assert lg.is_lead(root, "sess-1") is True
        events = [json.loads(l)["event"] for l in (root / "sessions.jsonl").read_text().splitlines()]
        assert "lead_started" in events

    def test_lead_start_defaults_project_to_cwd_basename_and_records_cwd(self, relay, root, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # cwd basename == tmp_path.name
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model=None, project=None))
        m = lg.read_marker(root, "sess-1")
        assert m["project"] == tmp_path.name
        # cwd is os.getcwd() (macOS may resolve /private symlinks) — compare the resolved path
        assert m["cwd"] == os.getcwd()

    def test_lead_start_honors_explicit_project(self, relay, root, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model=None, project="webapp"))
        assert lg.read_marker(root, "sess-1")["project"] == "webapp"

    def test_lead_start_warns_but_arms_without_terminal_notifier(self, relay, root, monkeypatch, capsys):
        # Missing notifier is no longer a hard failure: banners degrade to the osascript fallback,
        # so arming must proceed — with a visible warning naming the degradation and the fix.
        monkeypatch.setattr(relay.lead_guard, "find_terminal_notifier", lambda: None)  # absent
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model=None, project=None))
        assert lg.is_lead(root, "sess-1") is True    # DID arm
        err = capsys.readouterr().err
        assert "terminal-notifier" in err and "brew install" in err

    def test_lead_start_no_warning_when_notifications_disabled(self, relay, root, monkeypatch, capsys):
        # notify_on_wake: false is an explicit opt-out of banners → no notifier, no warning.
        monkeypatch.setattr(relay.lead_guard, "find_terminal_notifier", lambda: None)  # absent
        cfgp = lg.config_path(root)
        cfgp.parent.mkdir(parents=True, exist_ok=True)
        cfgp.write_text(json.dumps({"notify_on_wake": False}))
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model=None, project=None))
        assert lg.is_lead(root, "sess-1") is True
        assert "terminal-notifier" not in capsys.readouterr().err

    def test_lead_start_marker_records_color_and_label(self, relay, root):
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model=None, project="webapp"))
        m = lg.read_marker(root, "sess-1")
        assert m["tab_label"] == "[L] webapp"
        assert tuple(m["color"]) in lg.TAB_PALETTE
        assert m["color"] == lg.lead_color("sess-1")  # executors will inherit this exact color

    def test_route_opens_grace_and_logs(self, relay, root):
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model=None))
        relay.cmd_route(SimpleNamespace(kind="retain", reason="finalizing staged diff", session="sess-1"))
        assert lg.in_grace(root, "sess-1") is True
        events = [json.loads(l)["event"] for l in (root / "sessions.jsonl").read_text().splitlines()]
        assert "retained" in events

    def test_route_refuses_non_lead(self, relay):
        with pytest.raises(SystemExit):
            relay.cmd_route(SimpleNamespace(kind="retain", reason="x", session="never-a-lead"))

    def test_close_self_steps_down(self, relay, root):
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model=None))
        relay.cmd_close(SimpleNamespace(session_id=None, self_session="sess-1", supersede=None))
        assert lg.is_lead(root, "sess-1") is False

    def test_close_executor_unaffected_by_self_path(self, relay, root):
        # A normal executor close still works and does not touch lead state.
        relay.write_session("exec-1", {
            "session_id": "exec-1", "status": "reported", "current_packet": 1,
            "topic": "t", "worktree": "/w", "tab_label": "relay-exec-1",
        })
        relay.cmd_close(SimpleNamespace(session_id="exec-1", self_session=None, supersede=None))
        assert relay.read_session("exec-1")["status"] == "closed"

    def test_stop_steps_down(self, relay, root):
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model=None))
        relay.cmd_stop(SimpleNamespace(session_id="sess-1"))
        assert lg.is_lead(root, "sess-1") is False
        events = [json.loads(l)["event"] for l in (root / "sessions.jsonl").read_text().splitlines()]
        assert "lead_stepped_down" in events

    def test_stop_non_lead_is_noop(self, relay, root):
        relay.cmd_stop(SimpleNamespace(session_id="never-a-lead"))
        assert lg.is_lead(root, "never-a-lead") is False


# ---- App 1: executor-report surfacing ---------------------------------------------------------

class TestReportSurfacing:
    def _executor(self, root, sid, packet=1, status="reported", with_report=True, owner_lead="lead-1"):
        # owner_lead defaults to lead-1 (the lead these tests surface to) — wakes are owned-only now.
        d = root / sid
        (d / "packets").mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": sid, "current_packet": packet, "status": status, "owner_lead": owner_lead,
        }))
        if with_report:
            (d / "packets" / f"{packet:03d}-report.md").write_text("done")

    def test_executor_reports_lists_only_reported(self, root):
        self._executor(root, "exec-done", with_report=True)
        self._executor(root, "exec-busy", with_report=False)
        lg.write_marker(root, "lead-1")  # lead dir must NOT appear as an executor
        reps = {sid for sid, _, _ in lg.executor_reports(root)}
        assert reps == {"exec-done"}

    def test_closed_executor_report_ignored(self, root):
        self._executor(root, "exec-closed", status="closed", with_report=True)
        assert lg.executor_reports(root) == []

    def test_new_reports_surface_once(self, root):
        self._executor(root, "exec-done", packet=1)
        lg.write_marker(root, "lead-1")
        fresh = lg.new_reports_for(root, "lead-1")
        assert [f[1] for f in fresh] == ["exec-done"]
        # after marking, the same report no longer surfaces
        lg.mark_surfaced(root, "lead-1", [f[0] for f in fresh])
        assert lg.new_reports_for(root, "lead-1") == []

    def test_new_packet_from_same_executor_surfaces_again(self, root):
        self._executor(root, "exec-done", packet=1)
        lg.write_marker(root, "lead-1")
        lg.mark_surfaced(root, "lead-1", [f[0] for f in lg.new_reports_for(root, "lead-1")])
        # same session reports a SECOND packet → new key, surfaces again
        (root / "exec-done" / "session.json").write_text(json.dumps({
            "session_id": "exec-done", "current_packet": 2, "status": "reported", "owner_lead": "lead-1"}))
        (root / "exec-done" / "packets" / "002-report.md").write_text("done2")
        fresh = lg.new_reports_for(root, "lead-1")
        assert [(f[1], f[2]) for f in fresh] == [("exec-done", 2)]


# ---- ownership scoping: wake only about my OWN work --------------------------------------------

class TestOwnershipScoping:
    """The multi-lead capstone: an idle lead is woken ONLY about executors it OWNS — never another
    lead's, and never UNOWNED (bare/legacy) ones (those would spam every lead with stale reports).
    Unowned executors stay visible passively in `relay list`, just not via the wake."""

    def _busy(self, root, sid, owner_lead):
        d = root / sid
        d.mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": sid, "current_packet": 1, "status": "busy", "owner_lead": owner_lead,
        }))

    def _reported(self, root, sid, owner_lead, packet=1):
        d = root / sid
        (d / "packets").mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": sid, "current_packet": packet, "status": "reported",
            "owner_lead": owner_lead,
        }))
        (d / "packets" / f"{packet:03d}-report.md").write_text("done")

    # --- has_inflight_executors ---

    def test_inflight_owned_counted_for_owner_only(self, root):
        self._busy(root, "exec-a", owner_lead="lead-A")
        assert lg.has_inflight_executors(root, "lead-A") is True   # A's own executor
        assert lg.has_inflight_executors(root, "lead-B") is False  # NOT B's — no cross-wait

    def test_inflight_unowned_not_counted_for_a_scoped_lead(self, root):
        # Unowned (bare/legacy) busy executor must NOT make any specific lead wait — it would spam
        # every one. The global (unscoped) call still counts it (back-compat).
        self._busy(root, "exec-free", owner_lead=None)
        assert lg.has_inflight_executors(root, "lead-A") is False
        assert lg.has_inflight_executors(root, "lead-B") is False
        assert lg.has_inflight_executors(root) is True   # unscoped/global still counts it

    def test_inflight_global_when_owner_none(self, root):
        # owner_lead=None arg → pre-ownership behavior: ANY busy executor counts.
        self._busy(root, "exec-a", owner_lead="lead-A")
        self._busy(root, "exec-b", owner_lead="lead-B")
        assert lg.has_inflight_executors(root) is True

    def test_inflight_scoped_ignores_other_leads_only(self, root):
        # Only another lead's executor is busy → scoped lead sees nothing in flight.
        self._busy(root, "exec-b", owner_lead="lead-B")
        assert lg.has_inflight_executors(root, "lead-A") is False

    # --- new_reports_for ---

    def test_reports_owned_surface_to_owner(self, root):
        self._reported(root, "exec-a", owner_lead="lead-A")
        lg.write_marker(root, "lead-A")
        assert [f[1] for f in lg.new_reports_for(root, "lead-A")] == ["exec-a"]

    def test_reports_unowned_do_not_surface(self, root):
        # Unowned reports must NOT wake a lead — else every stale unowned report spams every new lead
        # (the bug that motivated owned-only wakes). They remain visible only in `relay list`.
        self._reported(root, "exec-free", owner_lead=None)
        lg.write_marker(root, "lead-A")
        assert lg.new_reports_for(root, "lead-A") == []

    def test_two_leads_no_cross_wake(self, root):
        # THE capstone case: A owns exec-a, B owns exec-b. Each lead is told ONLY about its own.
        self._reported(root, "exec-a", owner_lead="lead-A")
        self._reported(root, "exec-b", owner_lead="lead-B")
        lg.write_marker(root, "lead-A")
        lg.write_marker(root, "lead-B")
        assert [f[1] for f in lg.new_reports_for(root, "lead-A")] == ["exec-a"]  # A: only A's
        assert [f[1] for f in lg.new_reports_for(root, "lead-B")] == ["exec-b"]  # B: only B's
        # Explicitly: lead A's new_reports_for EXCLUDES lead B's executor.
        assert "exec-b" not in {f[1] for f in lg.new_reports_for(root, "lead-A")}


# ---- lead heartbeat ----------------------------------------------------------------------------

class TestTouchLead:
    def test_touch_updates_last_active(self, root, monkeypatch):
        lg.write_marker(root, "lead-1")
        # freeze now() to a distinct value so the update is unambiguous
        monkeypatch.setattr(lg, "now", lambda: "2099-01-01T00:00:00")
        lg.touch_lead(root, "lead-1")
        m = lg.read_marker(root, "lead-1")
        assert m["last_active"] == "2099-01-01T00:00:00"

    def test_touch_preserves_other_fields(self, root):
        lg.write_marker(root, "lead-1", model="opus", project="proj", cwd="/w")
        before = lg.read_marker(root, "lead-1")
        lg.touch_lead(root, "lead-1")
        after = lg.read_marker(root, "lead-1")
        for k in ("session_id", "project", "cwd", "started", "model"):
            assert after[k] == before[k]

    def test_touch_missing_marker_is_noop(self, root):
        # No marker on disk → silent no-op, never raises, writes nothing.
        lg.touch_lead(root, "ghost")
        assert lg.read_marker(root, "ghost") == {}
        assert not lg.marker_path(root, "ghost").exists()


# ---- App 2: lead-commit surfacing (real git repo) ----------------------------------------------

class TestCommitSurfacing:
    def _git(self, repo, *args):
        import subprocess
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    def _commit(self, repo, name, msg):
        (repo / name).write_text("x")
        self._git(repo, "add", name)
        self._git(repo, "commit", "-m", msg)

    def test_new_commits_since_baseline(self, tmp_path):
        repo = tmp_path / "r"; repo.mkdir()
        self._git(repo, "init", "-q")
        self._commit(repo, "a", "first")
        base = lg.git_head(repo)
        assert base  # HEAD resolves in a real repo
        assert lg.new_commits(repo, base) == []  # nothing new yet
        self._commit(repo, "b", "second inline change")
        commits = lg.new_commits(repo, base)
        assert len(commits) == 1 and "second inline change" in commits[0]

    def test_git_head_empty_outside_repo(self, tmp_path):
        assert lg.git_head(tmp_path) == ""

    def test_new_commits_empty_without_baseline(self, tmp_path):
        repo = tmp_path / "r"; repo.mkdir()
        self._git(repo, "init", "-q")
        self._commit(repo, "a", "first")
        assert lg.new_commits(repo, "") == []  # no baseline → nothing to diff


class TestNotifyFallback:
    """_notify uses terminal-notifier when present, else falls back to macOS's built-in
    `display notification` via osascript (same info; not clickable, no coalescing).
    subprocess.run is mocked — no real banners fire."""
    def _load_hook(self):
        import importlib.machinery
        import importlib.util
        path = str(REPO_ROOT / "hooks" / "stop_lead_watch.py")
        loader = importlib.machinery.SourceFileLoader("stop_hook_mod", path)
        spec = importlib.util.spec_from_file_location("stop_hook_mod", path, loader=loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def _notify(self, monkeypatch, notifier_path):
        mod = self._load_hook()
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)  # the suite sets it; this test mocks instead
        calls = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append(list(a[0])))
        monkeypatch.setattr(lg, "find_terminal_notifier", lambda: notifier_path)
        mod._notify({"notify_on_wake": True}, "exec-1 reported (packet 001)",
                    project="webapp", executor="exec-1", lead_sid="lead-1")
        return calls

    def test_terminal_notifier_used_when_present(self, monkeypatch):
        calls = self._notify(monkeypatch, "/x/terminal-notifier")
        assert calls and calls[0][0] == "/x/terminal-notifier"
        assert "-execute" in calls[0]                      # click→focus wired
        assert "-group" in calls[0]                        # per-lead coalescing

    def test_osascript_fallback_when_missing(self, monkeypatch):
        calls = self._notify(monkeypatch, None)
        assert calls and calls[0][0] == "osascript"
        joined = " ".join(calls[0])
        assert "display notification" in joined
        assert "webapp" in joined                          # still names the project

    def test_notify_on_wake_false_sends_nothing(self, monkeypatch):
        mod = self._load_hook()
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        calls = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append(a[0]))
        mod._notify({"notify_on_wake": False}, "msg", project="p", executor="e", lead_sid="l")
        assert calls == []


# ---- Stop hook end-to-end via stdin payload ----------------------------------------------------

class TestStopHookLivePayload:
    """Drive hooks/stop_lead_watch.py exactly as Claude Code would (JSON on stdin), under a tmp
    HOME so it reads an isolated ~/.relay-tasks. Asserts the exit-2 wake vs exit-0 silent contract."""
    def _run(self, home, payload):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "stop_lead_watch.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "HOME": str(home), "RELAY_NO_NOTIFY": "1"})  # no REAL desktop banners
        return p.returncode, p.stderr

    def test_non_lead_is_silent(self, tmp_path):
        rc, err = self._run(tmp_path, {"session_id": "nobody", "cwd": str(tmp_path)})
        assert rc == 0 and err == ""

    def test_new_report_wakes(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        # an executor with a report
        ed = root / "exec-1"; (ed / "packets").mkdir(parents=True)
        (ed / "session.json").write_text(json.dumps(
            {"session_id": "exec-1", "current_packet": 1, "status": "reported", "owner_lead": "lead-1"}))
        (ed / "packets" / "001-report.md").write_text("done")
        rc, err = self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path)})
        assert rc == 2                      # WAKE
        assert "exec-1" in err              # announced
        assert "WAIT for their direction" in err

    def test_report_does_not_re_wake(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        ed = root / "exec-1"; (ed / "packets").mkdir(parents=True)
        (ed / "session.json").write_text(json.dumps(
            {"session_id": "exec-1", "current_packet": 1, "status": "reported", "owner_lead": "lead-1"}))
        (ed / "packets" / "001-report.md").write_text("done")
        assert self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path)})[0] == 2
        # second stop: same report already surfaced → silent
        assert self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path)})[0] == 0

    def test_stop_hook_active_is_silent(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        ed = root / "exec-1"; (ed / "packets").mkdir(parents=True)
        (ed / "session.json").write_text(json.dumps(
            {"session_id": "exec-1", "current_packet": 1, "status": "reported", "owner_lead": "lead-1"}))
        (ed / "packets" / "001-report.md").write_text("done")
        # stop_hook_active skips the SYNCHRONOUS re-announce (no loop); the executor already reported
        # (not busy) so there's nothing left to poll → silent. (A still-busy one WOULD be polled —
        # see test_stop_active_still_polls_for_late_report.)
        rc, _ = self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path), "stop_hook_active": True})
        assert rc == 0


class TestStopHookBackgroundPoll:
    """The critical case a one-shot check misses: the executor finishes AFTER the lead is idle.
    The hook must background-poll and wake when the report lands. Drives the real hook subprocess."""
    def _setup(self, home, poll_seconds=8, poll_interval=1):
        root = home / ".relay-tasks"
        (root / "lead").mkdir(parents=True)
        (root / "lead" / "config.json").write_text(json.dumps(
            {"poll_seconds": poll_seconds, "poll_interval": poll_interval}))
        lg.write_marker(root, "lead-1")
        return root

    def _busy_executor(self, root, sid="exec-1", packet=1):
        d = root / sid; (d / "packets").mkdir(parents=True)
        (d / "session.json").write_text(json.dumps(
            {"session_id": sid, "current_packet": packet, "status": "busy",  # busy, NO report yet
             "owner_lead": "lead-1"}))
        return d / "packets" / f"{packet:03d}-report.md"

    def _popen(self, home, **extra):
        import subprocess
        proc = subprocess.Popen(
            ["python3", str(REPO_ROOT / "hooks" / "stop_lead_watch.py")],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "HOME": str(home), "RELAY_NO_NOTIFY": "1"})  # no REAL desktop banners
        proc.stdin.write(json.dumps({"session_id": "lead-1", "cwd": str(home), **extra}))
        proc.stdin.close()  # signal EOF so the hook's json.load() completes
        return proc

    @staticmethod
    def _finish(proc, timeout):
        # Don't use communicate() after a manual stdin.close() — it re-flushes the closed pipe and
        # raises. stderr output is tiny (well under the pipe buffer) so a plain read can't deadlock.
        proc.wait(timeout=timeout)
        return proc.returncode, proc.stderr.read()

    def test_wakes_on_late_arriving_report(self, tmp_path):
        import time
        root = self._setup(tmp_path)
        report = self._busy_executor(root)
        proc = self._popen(tmp_path)
        time.sleep(2)                       # lead is idle, poller is waiting; no report yet
        assert proc.poll() is None          # still polling, has NOT exited
        report.write_text("Fixed the off-by-one in slug()")   # executor finishes NOW
        rc, err = self._finish(proc, timeout=10)
        assert rc == 2                      # woke on the late report
        assert "exec-1" in err
        assert "Fixed the off-by-one" in err   # the report BRIEF is surfaced in the wake (Request 3)

    def test_stop_active_still_polls_for_late_report(self, tmp_path):
        # BUG 4: on the post-wake re-run (stop_hook_active), a still-BUSY executor must still be
        # watched, so a report that lands while the lead sits idle awaiting your answer is caught —
        # not missed. Before the fix this early-exited on the stop_hook_active flag.
        import time
        root = self._setup(tmp_path)
        report = self._busy_executor(root)
        proc = self._popen(tmp_path, stop_hook_active=True)
        time.sleep(2)
        assert proc.poll() is None                 # armed a poller DESPITE stop_hook_active (the fix)
        report.write_text("done late")
        rc, err = self._finish(proc, timeout=10)
        assert rc == 2 and "exec-1" in err          # woke on the late report

    def test_times_out_silent_when_no_report(self, tmp_path):
        self._setup(tmp_path, poll_seconds=3)
        self._busy_executor(tmp_path / ".relay-tasks")   # busy forever, never reports
        proc = self._popen(tmp_path)
        rc, _ = self._finish(proc, timeout=12)
        assert rc == 0                       # timed out silently

    def test_no_inflight_exits_fast_without_polling(self, tmp_path):
        import time
        self._setup(tmp_path, poll_seconds=30)   # long timeout — but there's nothing to wait on
        proc = self._popen(tmp_path)
        t0 = time.time()
        rc, _ = self._finish(proc, timeout=10)
        assert rc == 0
        assert time.time() - t0 < 5              # returned fast, did NOT enter the 30s poll

    def test_second_poller_does_not_stack(self, tmp_path):
        import time
        root = self._setup(tmp_path, poll_seconds=8)
        self._busy_executor(root)
        p1 = self._popen(tmp_path)
        time.sleep(2)                            # p1 holds the poll lock
        p2 = self._popen(tmp_path)
        rc2, _ = self._finish(p2, timeout=8)
        assert rc2 == 0                          # second poller saw the lock and bailed immediately
        p1.terminate()
        try:
            p1.wait(timeout=5)
        except Exception:
            pass


class TestSessionEndHookLeadCleanup:
    """Drive hooks/sessionend_lead_cleanup.py exactly as Claude Code would (JSON on stdin), under a
    tmp HOME so it reads an isolated ~/.relay-tasks. Tests that it logs SessionEnd events and only
    clears lead state on documented real-end reasons."""

    def _run(self, home, payload):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "sessionend_lead_cleanup.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)})
        return p.returncode, p.stderr

    def _read_ledger(self, root):
        """Read all ledger lines as JSON records."""
        ledger_path = root / "sessions.jsonl"
        if not ledger_path.exists():
            return []
        return [json.loads(line) for line in ledger_path.read_text().strip().split("\n") if line]

    def test_reason_logout_clears_lead(self, tmp_path):
        """reason="logout" (a real end) → marker cleared; SessionEnd logged."""
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        assert (root / "lead" / "lead-1").exists()

        rc, _ = self._run(tmp_path, {"session_id": "lead-1", "reason": "logout"})

        # Session ended cleanly
        assert rc == 0
        # Marker should be cleared
        assert not (root / "lead" / "lead-1").exists()
        # Event logged
        ledger = self._read_ledger(root)
        assert any(r["event"] == "session_end" and r["session_id"] == "lead-1" and r["reason"] == "logout"
                   for r in ledger)

    def test_reason_absent_preserves_lead(self, tmp_path):
        """reason absent (unknown) → marker SURVIVES; SessionEnd logged."""
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        marker_path = root / "lead" / "lead-1"
        assert marker_path.exists()

        rc, _ = self._run(tmp_path, {"session_id": "lead-1"})  # no reason field

        # Session handled cleanly
        assert rc == 0
        # Marker should still exist (fail-safe)
        assert marker_path.exists()
        # Event logged with reason=None
        ledger = self._read_ledger(root)
        assert any(r["event"] == "session_end" and r["session_id"] == "lead-1" and r["reason"] is None
                   for r in ledger)

    def test_reason_unknown_preserves_lead(self, tmp_path):
        """reason="unknown/junk" (not in REAL_END_REASONS) → marker SURVIVES; SessionEnd logged."""
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        marker_path = root / "lead" / "lead-1"
        assert marker_path.exists()

        rc, _ = self._run(tmp_path, {"session_id": "lead-1", "reason": "other_weird_reason"})

        # Session handled cleanly
        assert rc == 0
        # Marker should still exist (fail-safe)
        assert marker_path.exists()
        # Event logged
        ledger = self._read_ledger(root)
        assert any(r["event"] == "session_end" and r["session_id"] == "lead-1"
                   and r["reason"] == "other_weird_reason" for r in ledger)

    def test_non_lead_exits_silently_but_logs(self, tmp_path):
        """non-lead session → exits 0 silently; SessionEnd still logged (harmless)."""
        root = tmp_path / ".relay-tasks"

        rc, err = self._run(tmp_path, {"session_id": "nobody", "reason": "logout"})

        # Session ended cleanly
        assert rc == 0
        assert err == ""
        # Event logged with was_lead=false
        ledger = self._read_ledger(root)
        assert any(r["event"] == "session_end" and r["session_id"] == "nobody"
                   and r["was_lead"] is False for r in ledger)
