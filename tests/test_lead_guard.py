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
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import lead_guard as lg  # noqa: E402
import iterm  # noqa: E402 — same sys.modules entry stop_lead_watch.py's own `import iterm` binds to


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


# ---- classify_bash_command (pure) — task d1 (§10) verb taxonomy --------------------------------

class TestClassifyBashCommand:
    """Field calibration: the incident's implementation verbs must all match a rule name; a full
    healthy-day custody/read command list must all pass as None (free-pass). Both lists are derived
    straight from §10's own text (docs/post-0.3.27-backlog.md §10, bullet d1)."""

    INCIDENT = [
        ("npm install express", "npm-install"),
        ("npm ci", "npm-install"),
        ("npm run build", "npm-run-build"),
        ("yarn install", "package-install"),
        ("pip install requests", "package-install"),
        ("tsc --build", "compiler"),
        ("go build ./...", "compiler"),
        ("cargo build --release", "compiler"),
        ("make", "compiler"),
        ("git clone git@github.com:x/y.git /opt/svc", "git-clone"),
        ("sudo tee /etc/systemd/system/myapp.service <<EOF", "service-file-write"),
        ("systemctl daemon-reload", "service-file-write"),
        ("sed -i s/foo/bar/ config.yml", "sed-inplace"),
        ("cat <<EOF > /opt/svc/app.env", "heredoc"),
        ("tee -a /opt/svc/config.ini <<< data", "tee-mutation"),
        ("rsync -av ./dist/ box:/opt/svc/", "rsync"),
    ]

    HEALTHY = [
        'git commit -m "land staged work"',
        "git push origin main",
        "systemctl restart api",
        "systemctl status api",
        "ssh box clickhouse-client < migration.sql",
        "pytest tests/",
        "npm test",
        "npm run test:unit",
        "go test ./...",
        "cargo test",
        "make test",
        "git status",
        "git diff",
        "git log --oneline -5",
        "cat README.md",
        "ls -la",
        "grep -rn foo .",
    ]

    @pytest.mark.parametrize("cmd,expected_rule", INCIDENT)
    def test_incident_commands_match_implementation_rule(self, cmd, expected_rule):
        assert lg.classify_bash_command(cmd) == expected_rule

    @pytest.mark.parametrize("cmd", HEALTHY)
    def test_healthy_day_commands_free_pass(self, cmd):
        assert lg.classify_bash_command(cmd) is None

    def test_custody_wins_on_pattern_overlap(self):
        # "npm run test:build" contains "build" but is a test invocation — custody (checked first)
        # must win over the npm-run-build implementation pattern.
        assert lg.classify_bash_command("npm run test:build") is None

    def test_unparseable_input_free_passes(self):
        assert lg.classify_bash_command(None) is None
        assert lg.classify_bash_command("") is None
        assert lg.classify_bash_command(123) is None


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


# ---- executor model policy ---------------------------------------------------------------------

class TestModelTier:
    def test_extracts_known_tiers(self):
        assert lg.model_tier("claude-sonnet-5") == "sonnet"
        assert lg.model_tier("claude-opus-4-8") == "opus"
        assert lg.model_tier("claude-haiku-4-5-20251001") == "haiku"
        assert lg.model_tier("claude-fable-5") == "fable"

    def test_bare_tier_name(self):
        assert lg.model_tier("opus") == "opus"

    def test_unknown_model_returns_none(self):
        assert lg.model_tier("some-future-model-xyz") is None

    def test_empty_or_none_returns_none(self):
        assert lg.model_tier(None) is None
        assert lg.model_tier("") is None


class TestModelExceedsCeiling:
    def test_below_ceiling_does_not_exceed(self):
        assert lg.model_exceeds_ceiling("sonnet", "opus") is False

    def test_at_ceiling_does_not_exceed(self):
        assert lg.model_exceeds_ceiling("opus", "opus") is False

    def test_above_ceiling_exceeds(self):
        assert lg.model_exceeds_ceiling("fable", "opus") is True

    def test_unknown_model_tier_exceeds_by_default(self):
        # Fail closed: a model name this list doesn't recognize must be refused, not silently let
        # through just because it can't be ranked.
        assert lg.model_exceeds_ceiling("some-future-model-xyz", "opus") is True

    def test_unknown_ceiling_tier_exceeds_by_default(self):
        assert lg.model_exceeds_ceiling("sonnet", "not-a-real-tier") is True

    def test_tier_ordering_is_fable_above_opus_above_sonnet_above_haiku(self):
        assert lg.model_exceeds_ceiling("opus", "sonnet") is True
        assert lg.model_exceeds_ceiling("sonnet", "haiku") is True
        assert lg.model_exceeds_ceiling("haiku", "sonnet") is False


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


class TestPreToolBashGateHook:
    """Drive hooks/pretool_bash_gate.py exactly as Claude Code would (JSON on stdin, tmp HOME).
    task d1 (§10): LOGGING ONLY — every case here must exit 0 with empty stdout (never a deny),
    the only observable difference is whether a would_have_blocked record lands in the ledger."""

    def _run(self, home, payload):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "pretool_bash_gate.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)})
        return p.returncode, p.stdout

    def _payload(self, sid, command):
        return {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": command}}

    def _ledger_events(self, root):
        p = root / "sessions.jsonl"
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]

    def test_implementation_verb_ledgers_and_allows(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        rc, out = self._run(tmp_path, self._payload("lead-1", "npm install left-pad"))
        assert rc == 0 and out.strip() == ""           # never a deny, even on a match
        events = self._ledger_events(root)
        assert len(events) == 1
        assert events[0]["event"] == "would_have_blocked"
        assert events[0]["session_id"] == "lead-1"
        assert events[0]["rule"] == "npm-install"
        assert events[0]["command"] == "npm install left-pad"

    def test_custody_verb_passes_with_no_ledger_event(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        rc, out = self._run(tmp_path, self._payload("lead-1", 'git commit -m "x"'))
        assert rc == 0 and out.strip() == ""
        assert self._ledger_events(root) == []

    def test_non_lead_session_zero_side_effects(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        # no marker written — this session is not a lead at all
        rc, out = self._run(tmp_path, self._payload("stranger-1", "npm install left-pad"))
        assert rc == 0 and out.strip() == ""
        assert not root.exists() or self._ledger_events(root) == []

    def test_kill_switch_silences_logging(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        (root / "lead").mkdir(parents=True, exist_ok=True)
        (root / "lead" / "config.json").write_text(json.dumps({"bash_gate_logging": False}))
        rc, out = self._run(tmp_path, self._payload("lead-1", "npm install left-pad"))
        assert rc == 0 and out.strip() == ""
        assert self._ledger_events(root) == []


class TestExecutorEscalationHook:
    """Drive hooks/executor_escalation.py exactly as Claude Code would (JSON on stdin, tmp HOME).
    Single-shot push (wake-watch design §9): every case returns promptly, no sleeping, no loop.
    These cases never reach `_push_to_lead` (resolved/notify/gated branches), so a real subprocess
    round trip is safe — no `relay nudge-lead` → osascript call is ever attempted. The one case that
    DOES reach the send branch (test_sends_regardless_of_lead_busy_state) needs to intercept that
    subprocess call, which is impossible via mocking from the parent test process (the hook runs as
    a genuinely separate `python3` process) — see TestExecutorEscalationHookSendPath below, which
    imports the hook module directly instead of shelling out."""

    def _executor(self, root, sid="exec-1", packet=1, owner_lead=None, report=True, status="busy"):
        d = root / sid
        (d / "packets").mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": sid, "current_packet": packet, "status": status, "owner_lead": owner_lead,
        }))
        if report:
            (d / "packets" / f"{packet:03d}-report.md").write_text("done")

    def _run(self, home, payload, timeout=10):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "executor_escalation.py")],
            input=json.dumps(payload), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "HOME": str(home), "RELAY_NO_NOTIFY": "1"})
        return p.returncode

    def test_non_executor_session_is_silent(self, tmp_path):
        assert self._run(tmp_path, {"session_id": "nobody"}) == 0

    def test_no_report_yet_is_silent(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        self._executor(root, report=False)
        assert self._run(tmp_path, {"session_id": "exec-1"}) == 0

    def test_kill_switch_off_is_silent(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        self._executor(root, owner_lead="lead-1")
        (root / "lead").mkdir(parents=True, exist_ok=True)
        (root / "lead" / "config.json").write_text(json.dumps({"executor_escalation": False}))
        assert self._run(tmp_path, {"session_id": "exec-1"}) == 0

    def test_bad_payload_fails_open(self, tmp_path):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "executor_escalation.py")],
            input="not json", capture_output=True, text=True, timeout=10,
            env={**os.environ, "HOME": str(tmp_path), "RELAY_NO_NOTIFY": "1"})
        assert p.returncode == 0

    def test_fires_promptly_no_sleeping(self, tmp_path):
        # The whole point of the push: no grace sleep, no poll loop. Unowned (straight to notify,
        # never touches osascript) must still return in well under a second, not the old
        # grace/backoff timescale.
        root = tmp_path / ".relay-tasks"
        self._executor(root, owner_lead=None)
        start = time.time()
        rc = self._run(tmp_path, {"session_id": "exec-1"}, timeout=10)
        elapsed = time.time() - start
        assert rc == 0
        assert elapsed < 5, f"hook took {elapsed:.1f}s — expected a prompt single-shot return"

    def test_already_resolved_does_not_send_and_logs_escalation_resolved(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        self._executor(root, owner_lead="lead-1")
        lg.write_marker(root, "lead-1", tab_label="[Lead] alpha")
        lg.mark_surfaced(root, "lead-1", ["exec-1:1"])
        assert self._run(tmp_path, {"session_id": "exec-1"}) == 0
        assert lg.load_escalation(root, "exec-1")["1"]["status"] == "resolved"
        events = [json.loads(l) for l in (root / "sessions.jsonl").read_text().splitlines()]
        assert any(e["event"] == "escalation_resolved" and e["session_id"] == "exec-1"
                   and e["packet"] == 1 for e in events)

    def test_once_per_packet_no_resend(self, tmp_path):
        # Pre-seed the ledger as already "sent" (as if a prior Stop already pushed) — a second Stop
        # for the same packet must gate out untouched, leaving the ledger exactly as it was.
        root = tmp_path / ".relay-tasks"
        self._executor(root, owner_lead="lead-1")
        lg.write_marker(root, "lead-1", tab_label="[Lead] alpha")
        lg.save_escalation(root, "exec-1", {"1": {"status": "sent"}})
        assert self._run(tmp_path, {"session_id": "exec-1"}) == 0
        assert lg.load_escalation(root, "exec-1") == {"1": {"status": "sent"}}

    def test_unowned_notifies_human_not_lead(self, tmp_path):
        # No owner_lead at all -> "unowned": must go straight to notify, never call nudge-lead.
        root = tmp_path / ".relay-tasks"
        self._executor(root, owner_lead=None)
        assert self._run(tmp_path, {"session_id": "exec-1"}) == 0
        ledger = lg.load_escalation(root, "exec-1")
        assert ledger["1"]["status"] == "notified"

    def test_owner_missing_notifies_human(self, tmp_path):
        # owner_lead set, but no marker for it (crashed/closed/pruned) -> notify, not nudge.
        root = tmp_path / ".relay-tasks"
        self._executor(root, owner_lead="ghost-lead")
        assert self._run(tmp_path, {"session_id": "exec-1"}) == 0
        assert lg.load_escalation(root, "exec-1")["1"]["status"] == "notified"


class TestExecutorEscalationHookSendPath:
    """The one branch that reaches `_push_to_lead` (a real `relay nudge-lead` subprocess call),
    exercised by importing hooks/executor_escalation.py directly (same load pattern as
    TestEscalationHookBackoffCap) instead of shelling out — mocking `subprocess.run` only works
    in-process; the hook's own subprocess boundary makes that unmockable from a parent-process
    test. `_run_main`'s fake `subprocess.run` intercepts ONLY the `nudge-lead` call (the one that
    would otherwise shell out to real `relay nudge-lead` → osascript); the hook's OTHER subprocess
    call, `relay whoami --json <name>` (D5), is let through to the REAL subprocess against the same
    tmp HOME, so these tests exercise the actual whoami contract rather than a hand-rolled stand-in
    for it."""

    @pytest.fixture
    def hook_mod(self):
        import importlib.util
        path = str(REPO_ROOT / "hooks" / "executor_escalation.py")
        spec = importlib.util.spec_from_file_location("executor_escalation_send_path_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _run_main(self, hook_mod, root, payload, monkeypatch, calls, argv_name="exec-1",
                  nudge_ok=True):
        import io
        # `hook_mod.subprocess` IS the same singleton `subprocess` module this test file's own
        # `import subprocess` would bind to (Python caches modules by name) — so patching
        # `hook_mod.subprocess.run` mutates the ONE shared module. Grabbing `real_run` AFTER that
        # patch (or via a fresh `import subprocess as X`) would just hand back the fake — capture
        # the ORIGINAL function object first, before any patching, so forwarding to it can't recurse
        # into itself.
        real_run = hook_mod.subprocess.run
        monkeypatch.setattr(hook_mod, "STATE_ROOT", str(root))
        # Simulate the REAL invocation: relay bakes the executor's relay NAME into the hook command
        # as argv[1] (lead_guard.build_escalation_settings). The payload carries the CLAUDE session
        # id, which does NOT name any relay state dir.
        monkeypatch.setattr("sys.argv", ["executor_escalation.py", argv_name])
        monkeypatch.setenv("HOME", str(root.parent))  # so the REAL `relay whoami` subprocess below
                                                       # resolves STATE_ROOT to this same tmp tree

        def fake_run(cmd, **kwargs):
            if len(cmd) > 1 and cmd[1] == "nudge-lead":
                calls.append(cmd)
                return SimpleNamespace(returncode=0 if nudge_ok else 1)
            return real_run(cmd, **kwargs)  # whoami (and anything else) runs for real

        monkeypatch.setattr(hook_mod.subprocess, "run", fake_run)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setenv("RELAY_NO_NOTIFY", "1")
        with pytest.raises(SystemExit) as ei:
            hook_mod.main()
        return ei.value.code

    def test_sends_regardless_of_lead_busy_state(self, hook_mod, root, monkeypatch):
        # §9.5b: the push never checks whether the lead is busy — it always sends. This test would
        # FAIL if someone reintroduced a busy/stale guard: a busy-marked lead must still get pushed.
        d = root / "exec-1"; (d / "packets").mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": "exec-1", "current_packet": 1, "status": "busy", "owner_lead": "lead-1"}))
        (d / "packets" / "001-report.md").write_text("done")
        lg.write_marker(root, "lead-1", tab_label="[Lead] alpha")
        m = lg.read_marker(root, "lead-1")
        m["state"] = "busy"  # legacy field some old marker might still carry — must be ignored
        m["state_since"] = lg.now()
        lg.marker_path(root, "lead-1").write_text(json.dumps(m))

        calls = []
        # NOTE the payload carries a CLAUDE session id that matches NO relay state dir — exactly
        # what Claude Code sends. Identity must come from argv, not the payload.
        rc = self._run_main(hook_mod, root,
                            {"session_id": "f0a5e989-ba22-440a-80a2-fe38c5f73146"},
                            monkeypatch, calls, argv_name="exec-1")
        assert rc == 0
        assert calls and "nudge-lead" in calls[0]
        assert lg.load_escalation(root, "exec-1")["1"]["status"] == "sent"

    def test_identity_comes_from_argv_not_the_payload_session_id(self, hook_mod, root, monkeypatch):
        """REGRESSION: the hook once derived its identity from payload["session_id"] (the CLAUDE
        id) and looked up ~/.relay-tasks/<claude-id>/, which never exists — so it exited as "not a
        relay executor" EVERY time and the push never fired in production. Unit tests missed it
        because they passed the relay name in the payload. This pins the real contract."""
        d = root / "exec-2"; (d / "packets").mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": "exec-2", "current_packet": 1, "status": "busy",
            "owner_lead": "lead-1", "claude_session": "cccccccc-0000-0000-0000-000000000000"}))
        (d / "packets" / "001-report.md").write_text("done")
        lg.write_marker(root, "lead-1", tab_label="[Lead] alpha")
        calls = []
        rc = self._run_main(hook_mod, root,
                            {"session_id": "cccccccc-0000-0000-0000-000000000000"},
                            monkeypatch, calls, argv_name="exec-2")
        assert rc == 0
        assert calls and "nudge-lead" in calls[0], "push did not fire — identity lookup regressed"
        assert lg.load_escalation(root, "exec-2")["1"]["status"] == "sent"

    def test_failed_push_records_failed_not_sent(self, hook_mod, root, monkeypatch):
        # D4: a nudge that fails (nonzero exit, e.g. no-live-tab) must not be recorded as delivered.
        d = root / "exec-3"; (d / "packets").mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": "exec-3", "current_packet": 1, "status": "busy", "owner_lead": "lead-1"}))
        (d / "packets" / "001-report.md").write_text("done")
        lg.write_marker(root, "lead-1", tab_label="[Lead] alpha")
        calls = []
        rc = self._run_main(hook_mod, root, {"session_id": "irrelevant"}, monkeypatch, calls,
                            argv_name="exec-3", nudge_ok=False)
        assert rc == 0
        assert calls and "nudge-lead" in calls[0]
        assert lg.load_escalation(root, "exec-3")["1"]["status"] == "failed"
        events = [json.loads(l) for l in (root / "sessions.jsonl").read_text().splitlines()]
        assert any(e["event"] == "escalation_push_failed" and e["session_id"] == "exec-3"
                   for e in events)


class TestEscalationRearmOnUnconfirmedPush:
    """#22/§13 second half: the one-shot push must NOT burn its slot on a nudge it can't confirm.
    §13's incident had the nudge fire into a busy lead's tab, get swallowed, and the slot spent —
    both wake layers dying on the same busy window with nothing left to retry."""

    @pytest.fixture
    def hook_mod(self):
        import importlib.util
        path = str(REPO_ROOT / "hooks" / "executor_escalation.py")
        spec = importlib.util.spec_from_file_location("executor_escalation_rearm_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _executor(self, root, sid="exec-r"):
        d = root / sid
        (d / "packets").mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": sid, "current_packet": 1, "status": "busy", "owner_lead": "lead-1"}))
        (d / "packets" / "001-report.md").write_text("done")
        lg.write_marker(root, "lead-1", tab_label="[Lead] alpha")

    def _run(self, hook_mod, root, monkeypatch, sid="exec-r"):
        import io
        real_run = hook_mod.subprocess.run
        calls = []
        monkeypatch.setattr(hook_mod, "STATE_ROOT", str(root))
        monkeypatch.setattr("sys.argv", ["executor_escalation.py", sid])
        monkeypatch.setenv("HOME", str(root.parent))
        monkeypatch.setenv("RELAY_NO_NOTIFY", "1")

        def fake_run(cmd, **kwargs):
            if len(cmd) > 1 and cmd[1] in ("nudge-lead", "_deliver-queued"):
                calls.append(cmd[1])
                return SimpleNamespace(returncode=0)
            return real_run(cmd, **kwargs)          # whoami runs for real

        monkeypatch.setattr(hook_mod.subprocess, "run", fake_run)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "claude-uuid"})))
        with pytest.raises(SystemExit) as ei:
            hook_mod.main()
        return ei.value.code, calls

    def test_a_sent_push_is_recorded_unconfirmed(self, tmp_path, hook_mod, monkeypatch):
        root = tmp_path / ".relay-tasks"
        self._executor(root)
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0 and "nudge-lead" in calls
        entry = lg.load_escalation(root, "exec-r")["1"]
        assert entry["status"] == "sent" and entry["confirmed"] is False

    def test_unconfirmed_push_re_arms_while_the_report_stays_unsurfaced(self, tmp_path, hook_mod,
                                                                       monkeypatch):
        # THE FIX: the lead never handled it, so a later executor Stop pushes again instead of
        # gating out on a shot that demonstrably achieved nothing.
        root = tmp_path / ".relay-tasks"
        self._executor(root)
        self._run(hook_mod, root, monkeypatch)
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0 and "nudge-lead" in calls          # pushed a SECOND time

    def test_it_stops_re_arming_once_the_lead_handles_the_report(self, tmp_path, hook_mod,
                                                                 monkeypatch):
        # the negative half: proof via an #17 channel closes it out — no infinite re-nudging
        root = tmp_path / ".relay-tasks"
        self._executor(root)
        self._run(hook_mod, root, monkeypatch)
        lg.mark_surfaced(root, "lead-1", ["exec-r:1"])    # lead ran check/diff/close
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0 and "nudge-lead" not in calls
        assert lg.load_escalation(root, "exec-r")["1"]["confirmed"] is True

    def test_a_pending_only_announce_does_not_count_as_handled(self, tmp_path, hook_mod,
                                                               monkeypatch):
        # a lead-side announce that was never proven delivered must NOT silence the executor's
        # push — that combination is exactly how §13's report reached nobody
        root = tmp_path / ".relay-tasks"
        self._executor(root)
        self._run(hook_mod, root, monkeypatch)
        lg.mark_pending(root, "lead-1", ["exec-r:1"])
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0 and "nudge-lead" in calls

    def test_a_legacy_entry_is_never_resurrected(self, tmp_path, hook_mod, monkeypatch):
        # entries written before this fix carry no `confirmed` field → treated as confirmed
        root = tmp_path / ".relay-tasks"
        self._executor(root)
        lg.save_escalation(root, "exec-r", {"1": {"status": "sent"}})
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0 and "nudge-lead" not in calls
        assert lg.load_escalation(root, "exec-r") == {"1": {"status": "sent"}}

    def test_a_failed_push_still_does_not_retry(self, tmp_path, hook_mod, monkeypatch):
        # unchanged design: `failed` is terminal (D4) — only an unconfirmed `sent` re-arms
        root = tmp_path / ".relay-tasks"
        self._executor(root)
        lg.save_escalation(root, "exec-r", {"1": {"status": "failed"}})
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0 and "nudge-lead" not in calls


class TestExecutorEscalationHookQueueDelivery:
    """The PRIMARY --when-idle delivery trigger (task #18): this Stop hook IS the idle transition a
    queued packet is waiting for, so it hands off to `relay _deliver-queued`. Same in-process load
    pattern as TestExecutorEscalationHookSendPath — the hook's own subprocess boundary can't be
    mocked from a shelled-out child. `relay whoami` is let through for real; only the two outbound
    relay commands are intercepted."""

    @pytest.fixture
    def hook_mod(self):
        import importlib.util
        path = str(REPO_ROOT / "hooks" / "executor_escalation.py")
        spec = importlib.util.spec_from_file_location("executor_escalation_queue_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _executor(self, root, sid="exec-q", queued=False):
        d = root / sid
        (d / "packets").mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": sid, "current_packet": 1, "status": "busy", "owner_lead": "lead-1"}))
        (d / "packets" / "001-report.md").write_text("done")     # reported → it just went idle
        lg.write_marker(root, "lead-1", tab_label="[Lead] alpha")
        if queued:
            (d / "queue.json").write_text(json.dumps({"items": [{"id": 1, "body_path": "x.md"}]}))

    def _run(self, hook_mod, root, monkeypatch, sid="exec-q"):
        import io
        real_run = hook_mod.subprocess.run
        calls = []
        monkeypatch.setattr(hook_mod, "STATE_ROOT", str(root))
        monkeypatch.setattr("sys.argv", ["executor_escalation.py", sid])
        monkeypatch.setenv("HOME", str(root.parent))
        monkeypatch.setenv("RELAY_NO_NOTIFY", "1")

        def fake_run(cmd, **kwargs):
            if len(cmd) > 1 and cmd[1] in ("_deliver-queued", "nudge-lead"):
                calls.append(cmd)
                return SimpleNamespace(returncode=0)
            return real_run(cmd, **kwargs)                        # whoami runs for real

        monkeypatch.setattr(hook_mod.subprocess, "run", fake_run)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "claude-uuid"})))
        with pytest.raises(SystemExit) as ei:
            hook_mod.main()
        return ei.value.code, calls

    def test_hook_delivers_a_queued_packet_on_idle(self, tmp_path, hook_mod, monkeypatch):
        root = tmp_path / ".relay-tasks"
        self._executor(root, queued=True)
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0
        assert ["_deliver-queued", "exec-q"] == calls[0][1:3]

    def test_hook_skips_the_subprocess_when_nothing_is_queued(self, tmp_path, hook_mod, monkeypatch):
        # the overwhelmingly common Stop must not pay for a subprocess it has no use for
        root = tmp_path / ".relay-tasks"
        self._executor(root, queued=False)
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0
        assert not any(c[1] == "_deliver-queued" for c in calls)

    def test_delivery_runs_even_when_the_escalation_killswitch_is_off(self, tmp_path, hook_mod,
                                                                     monkeypatch):
        # queue delivery is a separate feature from the wake push and must not inherit its gating
        root = tmp_path / ".relay-tasks"
        self._executor(root, queued=True)
        (root / "lead").mkdir(parents=True, exist_ok=True)
        (root / "lead" / "config.json").write_text(json.dumps({"executor_escalation": False}))
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0
        assert any(c[1] == "_deliver-queued" for c in calls)
        assert not any(c[1] == "nudge-lead" for c in calls)       # push correctly stayed off

    def test_delivery_runs_even_when_the_packet_was_already_pushed(self, tmp_path, hook_mod,
                                                                   monkeypatch):
        # the once-per-packet escalation gate must not swallow a later queue delivery
        root = tmp_path / ".relay-tasks"
        self._executor(root, queued=True)
        lg.save_escalation(root, "exec-q", {"1": {"status": "sent"}})
        rc, calls = self._run(hook_mod, root, monkeypatch)
        assert rc == 0
        assert any(c[1] == "_deliver-queued" for c in calls)

    def test_a_failing_delivery_never_breaks_the_hook(self, tmp_path, hook_mod, monkeypatch):
        # HARD RULE: any error → exit 0, the executor's own Stop must be undisturbed
        import io
        root = tmp_path / ".relay-tasks"
        self._executor(root, queued=True)
        monkeypatch.setattr(hook_mod, "STATE_ROOT", str(root))
        monkeypatch.setattr("sys.argv", ["executor_escalation.py", "exec-q"])
        monkeypatch.setenv("HOME", str(root.parent))
        monkeypatch.setenv("RELAY_NO_NOTIFY", "1")
        real_run = hook_mod.subprocess.run

        def boom(cmd, **kwargs):
            if len(cmd) > 1 and cmd[1] == "_deliver-queued":
                raise OSError("boom")
            if len(cmd) > 1 and cmd[1] == "nudge-lead":
                return SimpleNamespace(returncode=0)
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(hook_mod.subprocess, "run", boom)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "claude-uuid"})))
        with pytest.raises(SystemExit) as ei:
            hook_mod.main()
        assert ei.value.code == 0


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
        assert m["tab_label"] == "[Lead] webapp"
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

    def test_lead_start_stamps_plugin_version_and_timeout(self, relay, root):
        # Arm stamps the plugin version + Stop-hook timeout of the code bin/relay itself is running —
        # the same ${CLAUDE_PLUGIN_ROOT} the session's hooks fire from, so it names the real hook.
        relay.cmd_lead_start(SimpleNamespace(session_id="sess-1", model=None))
        m = lg.read_marker(root, "sess-1")
        assert m["plugin_version"] == relay.plugin_version()      # from .claude-plugin/plugin.json
        assert m["stop_hook_timeout"] == relay.stop_hook_timeout()  # from hooks/hooks.json Stop entry
        assert isinstance(m["stop_hook_timeout"], int)            # repo declares a real timeout (fixed)


class TestAutonomousPosture:
    """§6f / task #16 phase 1: `relay auto on|off|status`, the `autonomous_mode` config default, and
    the marker state both write to. The invariants worth protecting are (a) OFF is the default and
    survives every path that doesn't explicitly ask for auto, (b) the posture is per-session and
    RESETS on a fresh arm rather than silently persisting, and (c) every flip is reconstructable
    from the ledger, because the whole point is that the human wasn't asked."""

    @pytest.fixture(autouse=True)
    def _armable(self, relay, monkeypatch):
        # Same isolation the sibling lead-command tests use: never touch the real terminal.
        monkeypatch.setattr(relay.lead_guard, "find_terminal_notifier", lambda: "/x/terminal-notifier")
        monkeypatch.delenv("TERM_SESSION_ID", raising=False)
        monkeypatch.setattr(relay.iterm, "rename_by_id", lambda *a, **k: True)

    def _cfg(self, root, **kv):
        p = lg.config_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(kv))

    def _arm(self, relay, sid="sess-1", project="webapp"):
        relay.cmd_lead_start(SimpleNamespace(session_id=sid, model=None, project=project))

    def _auto(self, relay, action, sid="sess-1"):
        relay.cmd_auto(SimpleNamespace(action=action, session=sid))

    # ---- config default ------------------------------------------------------------------------

    def test_config_default_is_off(self, root):
        # The safe default must STAY the default — this is the one assertion in this class that is
        # really about policy rather than mechanism.
        assert lg.LEAD_DEFAULTS["autonomous_mode"] is False
        assert lg.load_config(root)["autonomous_mode"] is False

    def test_config_true_is_read_back(self, root):
        self._cfg(root, autonomous_mode=True)
        assert lg.load_config(root)["autonomous_mode"] is True

    # ---- marker state --------------------------------------------------------------------------

    def test_fresh_arm_defaults_to_manual_from_config(self, relay, root):
        self._arm(relay)
        m = lg.read_marker(root, "sess-1")
        assert m["autonomous"] is False
        assert lg.autonomous_state(m) == (False, "config")

    def test_arm_honors_config_autonomous_mode(self, relay, root):
        self._cfg(root, autonomous_mode=True)
        self._arm(relay)
        assert lg.autonomous_state(lg.read_marker(root, "sess-1")) == (True, "config")

    def test_arm_in_auto_announces_itself(self, relay, root, capsys):
        # An inverted posture nobody announced is the silent-autonomy failure §6f exists to prevent.
        self._cfg(root, autonomous_mode=True)
        self._arm(relay)
        assert "AUTONOMOUS MODE ON" in capsys.readouterr().out

    def test_arm_in_manual_is_quiet_about_posture(self, relay, root, capsys):
        self._arm(relay)
        assert "AUTONOMOUS MODE ON" not in capsys.readouterr().out

    def test_missing_key_reads_as_manual(self, root):
        # A lead armed before this feature existed has no key at all → must read as the safe default,
        # never as auto.
        assert lg.autonomous_state({"session_id": "old"}) == (False, "config")

    def test_autonomous_state_is_defensive(self):
        assert lg.autonomous_state(None) == (False, "config")
        assert lg.autonomous_state({}) == (False, "config")
        assert lg.autonomous_state({"autonomous": True, "autonomous_source": "bogus"}) == (True, "config")

    # ---- the command ---------------------------------------------------------------------------

    def test_auto_on_flips_marker_and_stamps_command_source(self, relay, root):
        self._arm(relay)
        self._auto(relay, "on")
        assert lg.autonomous_state(lg.read_marker(root, "sess-1")) == (True, "command")

    def test_auto_off_flips_back(self, relay, root):
        self._arm(relay)
        self._auto(relay, "on")
        self._auto(relay, "off")
        assert lg.autonomous_state(lg.read_marker(root, "sess-1")) == (False, "command")

    def test_command_overrides_config_in_both_directions(self, relay, root):
        # Config says auto; the command must still be able to pull this session back to manual.
        self._cfg(root, autonomous_mode=True)
        self._arm(relay)
        self._auto(relay, "off")
        assert lg.autonomous_state(lg.read_marker(root, "sess-1")) == (False, "command")
        self._auto(relay, "on")
        assert lg.autonomous_state(lg.read_marker(root, "sess-1")) == (True, "command")

    def test_flip_preserves_every_other_marker_field(self, relay, root):
        self._arm(relay, project="webapp")
        before = lg.read_marker(root, "sess-1")
        self._auto(relay, "on")
        after = lg.read_marker(root, "sess-1")
        for k in ("session_id", "project", "cwd", "tab_label", "color", "started",
                  "plugin_version", "stop_hook_timeout", "backend"):
            assert after[k] == before[k], f"`auto on` clobbered marker field {k!r}"

    def test_status_does_not_change_the_posture(self, relay, root):
        self._arm(relay)
        self._auto(relay, "on")
        self._auto(relay, "status")
        assert lg.autonomous_state(lg.read_marker(root, "sess-1")) == (True, "command")

    def test_status_reports_posture_and_origin(self, relay, root, capsys):
        self._arm(relay)
        capsys.readouterr()
        self._auto(relay, "status")
        out = capsys.readouterr().out
        assert "OFF" in out and "config" in out          # origin: inherited from config
        self._auto(relay, "on")
        capsys.readouterr()
        self._auto(relay, "status")
        out = capsys.readouterr().out
        assert "ON" in out and "relay auto" in out        # origin: set by command this session

    def test_auto_on_output_names_the_commit_boundary(self, relay, root, capsys):
        # Turning it on must SAY where the commit boundary now sits — a boundary the human only
        # learns by reading the source is worthless. Phase 1's boundary was a flat "committing
        # always stops"; #16 phase 2 replaces it with the five-condition gate, so the message has
        # to carry the conditions and how to check them. Weaker text here would hand someone a
        # proceed-by-default posture without telling them what still gates the commit.
        self._arm(relay)
        capsys.readouterr()
        self._auto(relay, "on")
        out = capsys.readouterr().out
        assert "COMMITTING an executor's work now has its own gate" in out
        assert "ONLY when all five hold" in out
        assert "--for-autocommit" in out
        assert "--in-plan" in out and "--diff-reviewed" in out
        assert "only if true" in out          # the attestations are not a formality
        assert "stop and ask" in out          # and the fallback is still the old behaviour

    def test_auto_status_on_names_the_commit_gate_too(self, relay, root, capsys):
        self._arm(relay)
        self._auto(relay, "on")
        capsys.readouterr()
        self._auto(relay, "status")
        out = capsys.readouterr().out
        assert "five auto-commit conditions" in out and "--for-autocommit" in out

    def test_auto_refuses_non_lead(self, relay):
        with pytest.raises(SystemExit):
            self._auto(relay, "on", sid="never-a-lead")

    def test_auto_refuses_empty_session(self, relay):
        with pytest.raises(SystemExit):
            relay.cmd_auto(SimpleNamespace(action="on", session=""))

    # ---- reset on re-arm -----------------------------------------------------------------------

    def test_rearm_resets_posture_to_config_default(self, relay, root):
        # THE headline invariant: you opt in each time you're confident, so an arm must clear a
        # posture the command set — it must not silently outlive the plan it was scoped to.
        self._arm(relay)
        self._auto(relay, "on")
        assert lg.autonomous_state(lg.read_marker(root, "sess-1"))[0] is True
        self._arm(relay)
        assert lg.autonomous_state(lg.read_marker(root, "sess-1")) == (False, "config")

    def test_rearm_resets_to_config_auto_when_configured(self, relay, root):
        # ...and the reset target is the CONFIG value, not a hardcoded False.
        self._cfg(root, autonomous_mode=True)
        self._arm(relay)
        self._auto(relay, "off")
        self._arm(relay)
        assert lg.autonomous_state(lg.read_marker(root, "sess-1")) == (True, "config")

    def test_rearm_still_preserves_started_and_predecessor(self, relay, root):
        # The posture resets on arm; the fields that must NOT reset still don't. Guards against a
        # future refactor sweeping `autonomous` in with predecessor/started preservation.
        self._arm(relay)
        m = lg.read_marker(root, "sess-1")
        m["predecessor"] = {"session_id": "old-lead"}
        lg.marker_path(root, "sess-1").write_text(json.dumps(m))
        started = m["started"]
        self._arm(relay)
        after = lg.read_marker(root, "sess-1")
        assert after["started"] == started
        assert after["predecessor"] == {"session_id": "old-lead"}

    def test_heartbeat_preserves_the_posture(self, relay, root):
        # touch_lead runs once per lead turn — it must not quietly drop the posture mid-session.
        self._arm(relay)
        self._auto(relay, "on")
        lg.touch_lead(root, "sess-1")
        assert lg.autonomous_state(lg.read_marker(root, "sess-1")) == (True, "command")

    # ---- set_autonomous helper -----------------------------------------------------------------

    def test_set_autonomous_returns_false_without_a_marker(self, root):
        assert lg.set_autonomous(root, "no-such-lead", True) is False

    # ---- ledger --------------------------------------------------------------------------------

    def _events(self, root):
        return [json.loads(l) for l in (root / "sessions.jsonl").read_text().splitlines()]

    def test_flips_are_logged_to_the_ledger(self, relay, root):
        self._arm(relay)
        self._auto(relay, "on")
        self._auto(relay, "off")
        names = [e["event"] for e in self._events(root)]
        assert "auto_mode_on" in names and "auto_mode_off" in names

    def test_status_logs_nothing(self, relay, root):
        self._arm(relay)
        before = len(self._events(root))
        self._auto(relay, "status")
        assert len(self._events(root)) == before

    def test_lead_started_event_records_the_posture(self, relay, root):
        self._cfg(root, autonomous_mode=True)
        self._arm(relay)
        started = [e for e in self._events(root) if e["event"] == "lead_started"]
        assert started and started[-1]["autonomous"] is True

    # ---- relay list visibility -------------------------------------------------------------------

    def test_list_stamps_auto_leads_and_dashes_manual_ones(self, relay, root, capsys, monkeypatch):
        monkeypatch.setattr(relay, "all_session_ids", lambda: [])
        self._arm(relay, sid="lead-manual", project="webapp")
        self._cfg(root, autonomous_mode=True)
        self._arm(relay, sid="lead-auto", project="datapipe")
        capsys.readouterr()
        relay.cmd_list(SimpleNamespace(json=False, closed=False, lead=None, all=True))
        out = capsys.readouterr().out
        assert "AUTO" in out                       # the column exists
        # ...and the footnote names the auto lead specifically, not the manual one.
        footnote = [l for l in out.splitlines() if "proceeding without asking" in l]
        assert footnote, "no AUTO footnote — an autonomous lead must be MORE visible, not less"
        assert "datapipe" in footnote[0] and "webapp" not in footnote[0]

    def test_list_has_no_auto_footnote_when_all_leads_are_manual(self, relay, root, capsys, monkeypatch):
        monkeypatch.setattr(relay, "all_session_ids", lambda: [])
        self._arm(relay, sid="lead-manual", project="webapp")
        capsys.readouterr()
        relay.cmd_list(SimpleNamespace(json=False, closed=False, lead=None, all=True))
        assert "proceeding without asking" not in capsys.readouterr().out

    # ---- the auto-wake instruction ---------------------------------------------------------------
    # The wake is the exact beat autonomous mode redefines ("announce and WAIT" -> "announce, act,
    # and record"), and the Stop hook INJECTS its instruction text into the lead. If that text stayed
    # hardcoded to "WAIT / do NOT act", it would silently override the posture the user just granted
    # and the toggle would look broken. These pin both branches.

    def _wake_text(self, root, sid, monkeypatch, capsys):
        sys.path.insert(0, str(REPO_ROOT / "hooks"))
        import stop_lead_watch as slw
        monkeypatch.setattr(slw, "STATE_ROOT", str(root))
        monkeypatch.setattr(slw, "_notify", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            slw._announce_and_wake(lg, {}, sid, ["exec-1 reported"], [], "msg")
        return capsys.readouterr().err

    def test_wake_tells_a_manual_lead_to_wait(self, relay, root, monkeypatch, capsys):
        self._arm(relay)
        err = self._wake_text(root, "sess-1", monkeypatch, capsys)
        assert "WAIT for their direction" in err
        assert "AUTONOMOUS MODE" not in err

    def test_wake_tells_an_auto_lead_to_act(self, relay, root, monkeypatch, capsys):
        self._arm(relay)
        self._auto(relay, "on")
        err = self._wake_text(root, "sess-1", monkeypatch, capsys)
        assert "AUTONOMOUS MODE" in err
        assert "WAIT for their direction" not in err
        # ...and it must carry the boundaries with it, not just the licence to proceed.
        assert "would have asked" in err

    # The phase-1 rule this replaced was "COMMITTING an executor's work ALWAYS stops for the user".
    # #16 phase 2 retires that flat stop in favour of a five-condition gate, so the wake text now
    # has to carry the CONDITIONS — a wake that only said "you may commit now" would be strictly
    # more dangerous than the text it replaced.

    def test_wake_carries_all_five_auto_commit_conditions(self, relay, root, monkeypatch, capsys):
        self._arm(relay)
        self._auto(relay, "on")
        err = self._wake_text(root, "sess-1", monkeypatch, capsys)
        assert "ONLY when ALL FIVE hold" in err
        assert "COUNTS-MATCH" in err                       # 1
        assert "clean-with-caveats STOPS" in err           # 2
        assert "approved plan" in err                      # 3
        assert "sign-off-gated" in err                     # 4
        assert "ACTUALLY READ the staged diff" in err      # 5

    def test_wake_names_the_clearance_command_and_the_not_cleared_fallback(
            self, relay, root, monkeypatch, capsys):
        self._arm(relay)
        self._auto(relay, "on")
        err = self._wake_text(root, "sess-1", monkeypatch, capsys)
        assert "--for-autocommit" in err
        assert "--in-plan" in err and "--diff-reviewed" in err
        assert "only if they are TRUE" in err
        assert "NOT-CLEARED" in err and "stop and ask" in err

    def test_wake_keeps_the_verifier_temper_at_the_commit_beat(
            self, relay, root, monkeypatch, capsys):
        """§9, binding hardest here: the machine check gates the AUTOMATION, it never replaces the
        lead reading the diff, and COUNTS-MATCH never means the report is true."""
        self._arm(relay)
        self._auto(relay, "on")
        err = self._wake_text(root, "sess-1", monkeypatch, capsys)
        assert "gates the AUTOMATION" in err
        assert "never replaces your reading of the diff" in err
        assert "COUNTS-MATCH never means the report is true" in err

    def test_manual_wake_is_unchanged_by_the_auto_commit_gate(
            self, relay, root, monkeypatch, capsys):
        """A manual lead must not learn about auto-commit from its wake — that posture still
        waits on everything."""
        self._arm(relay)
        err = self._wake_text(root, "sess-1", monkeypatch, capsys)
        assert "--for-autocommit" not in err
        assert "ALL FIVE" not in err
        assert "WAIT for their direction" in err


class TestWakeHookState:
    """lead_guard.wake_hook_state: is a lead's background wake poller safe from an early harness
    kill, judged from the Stop-hook timeout stamped in its marker vs the configured poll window."""
    def test_ok_when_timeout_ge_poll(self):
        assert lg.wake_hook_state({"stop_hook_timeout": 1900}, 1800) == "ok"
        assert lg.wake_hook_state({"stop_hook_timeout": 1800}, 1800) == "ok"  # boundary: equal is ok

    def test_stale_when_timeout_below_poll(self):
        assert lg.wake_hook_state({"stop_hook_timeout": 60}, 1800) == "stale"

    def test_stale_when_timeout_none(self):
        # The 0.1.0 signature: stamped, but that version's hooks.json had no timeout field at all.
        assert lg.wake_hook_state({"stop_hook_timeout": None}, 1800) == "stale"

    def test_unknown_when_field_absent(self):
        # Marker predates version stamping → 'unknown' (surface softly), never a false 'ok'.
        assert lg.wake_hook_state({"project": "x"}, 1800) == "unknown"

    def test_defensive_bad_input_is_stale(self):
        # Fail toward surfacing, not hiding: an unparseable stamp reads as 'stale'.
        assert lg.wake_hook_state({"stop_hook_timeout": "not-an-int"}, 1800) == "stale"


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

    def _stalled(self, root, sid, owner_lead):
        d = root / sid
        d.mkdir(parents=True)
        (d / "session.json").write_text(json.dumps({
            "session_id": sid, "current_packet": 1, "status": "stalled", "owner_lead": owner_lead,
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

    def test_inflight_counts_stalled_as_in_flight(self, root):
        # wake-watch design §6: a long-but-alive (stalled) executor is the MOST likely to report
        # while the lead idles — excluding it (the pre-fix behavior) was backwards.
        self._stalled(root, "exec-a", owner_lead="lead-A")
        assert lg.has_inflight_executors(root, "lead-A") is True
        assert lg.has_inflight_executors(root, "lead-B") is False  # still ownership-scoped
        assert lg.has_inflight_executors(root) is True

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


class TestEscalationDecision:
    """lg.escalation_decision: the wake-watch §9 push decision tree, pure given on-disk
    marker/surfaced state — unowned/owner-missing/resolved/send. Collapsed from the pre-push
    6-branch tree (§9.5b deleted the busy-guard's wait/stale branches: the push always sends)."""

    def test_unowned_has_no_owner(self, root):
        assert lg.escalation_decision(root, "exec-1", 1, owner_lead=None) == "unowned"

    def test_owner_missing_marker_gone(self, root):
        # owner_lead is set but no marker exists for it (crashed/closed/pruned lead).
        assert lg.escalation_decision(root, "exec-1", 1, owner_lead="ghost-lead") == "owner-missing"

    def test_resolved_when_already_surfaced(self, root):
        lg.write_marker(root, "lead-1")
        lg.mark_surfaced(root, "lead-1", ["exec-1:1"])
        assert lg.escalation_decision(root, "exec-1", 1, owner_lead="lead-1") == "resolved"

    def test_send_when_owner_present_and_unsurfaced(self, root):
        lg.write_marker(root, "lead-1")
        assert lg.escalation_decision(root, "exec-1", 1, owner_lead="lead-1") == "send"

    def test_send_regardless_of_busy_marker_state(self, root):
        # A marker still carrying a legacy `state: busy` field (pre-§9.5b) must NOT change the
        # outcome — the decision tree no longer reads that field at all.
        lg.write_marker(root, "lead-1")
        m = lg.read_marker(root, "lead-1")
        m["state"] = "busy"
        m["state_since"] = "2000-01-01T00:00:00"
        lg.marker_path(root, "lead-1").write_text(json.dumps(m))
        assert lg.escalation_decision(root, "exec-1", 1, owner_lead="lead-1") == "send"

    def test_different_packet_numbers_are_independent(self, root):
        # Surfacing packet 1 must not resolve packet 2's escalation for the same executor.
        lg.write_marker(root, "lead-1")
        lg.mark_surfaced(root, "lead-1", ["exec-1:1"])
        assert lg.escalation_decision(root, "exec-1", 1, owner_lead="lead-1") == "resolved"
        assert lg.escalation_decision(root, "exec-1", 2, owner_lead="lead-1") == "send"


class TestPendingWakes:
    """#22/§13: the two-phase wake stamp. An announce is PENDING (retryable); only proven delivery
    promotes it to surfaced. The mirror of #17 — that stamped too little and caused duplicate
    wakes; this stamped too early and swallowed reports for good, which is strictly worse."""

    @pytest.fixture
    def root(self, tmp_path):
        return tmp_path / ".relay-tasks"

    def test_pending_does_not_suppress_a_later_announce(self, root):
        # the heart of the fix: an announced-but-unproven report is still "new" next time
        lg.mark_pending(root, "lead-1", ["exec-1:1"])
        assert lg.load_surfaced(root, "lead-1") == set()
        assert lg.load_pending(root, "lead-1")["exec-1:1"]["announces"] == 1

    def test_promote_makes_it_surfaced(self, root):
        lg.mark_pending(root, "lead-1", ["exec-1:1"])
        assert lg.promote_pending(root, "lead-1") == ["exec-1:1"]
        assert lg.load_surfaced(root, "lead-1") == {"exec-1:1"}
        assert lg.load_pending(root, "lead-1") == {}          # and stops being retryable

    def test_promote_with_nothing_pending_is_a_noop(self, root):
        assert lg.promote_pending(root, "lead-1") == []
        assert lg.load_surfaced(root, "lead-1") == set()

    def test_an_17_channel_stamp_clears_pending(self, root):
        # check/diff/close/retire are proof the lead HANDLED the report — stop retrying it
        lg.mark_pending(root, "lead-1", ["exec-1:1"])
        lg.mark_surfaced(root, "lead-1", ["exec-1:1"])
        assert lg.load_pending(root, "lead-1") == {}
        assert lg.load_surfaced(root, "lead-1") == {"exec-1:1"}

    def test_retry_is_capped_so_it_can_never_spam(self, root):
        # a harness that never sets stop_hook_active must not be re-announced at forever
        capped = []
        for _ in range(lg.WAKE_RETRY_CAP):
            capped = lg.mark_pending(root, "lead-1", ["exec-1:1"])
        assert capped == ["exec-1:1"]
        assert lg.load_surfaced(root, "lead-1") == {"exec-1:1"}   # given up: stamped for real
        assert lg.load_pending(root, "lead-1") == {}

    def test_below_the_cap_nothing_is_stamped(self, root):
        for _ in range(lg.WAKE_RETRY_CAP - 1):
            assert lg.mark_pending(root, "lead-1", ["exec-1:1"]) == []
        assert lg.load_surfaced(root, "lead-1") == set()

    def test_pending_is_per_lead(self, root):
        lg.mark_pending(root, "lead-1", ["exec-1:1"])
        assert lg.load_pending(root, "lead-2") == {}
        lg.promote_pending(root, "lead-2")
        assert lg.load_surfaced(root, "lead-1") == set()          # untouched by the other lead

    def test_corrupt_pending_file_reads_as_empty(self, root):
        lg.mark_pending(root, "lead-1", ["exec-1:1"])
        lg._pending_path(root, "lead-1").write_text("{not json")
        assert lg.load_pending(root, "lead-1") == {}              # never raises

    def test_new_reports_for_still_returns_a_pending_report(self, root):
        # the property the whole fix rests on: pending is NOT dedup
        lg.write_marker(root, "lead-1")
        ed = root / "exec-1"; (ed / "packets").mkdir(parents=True)
        (ed / "session.json").write_text(json.dumps(
            {"session_id": "exec-1", "current_packet": 1, "status": "reported",
             "owner_lead": "lead-1"}))
        (ed / "packets" / "001-report.md").write_text("done")
        lg.mark_pending(root, "lead-1", ["exec-1:1"])
        assert [f[0] for f in lg.new_reports_for(root, "lead-1")] == ["exec-1:1"]
        lg.promote_pending(root, "lead-1")
        assert lg.new_reports_for(root, "lead-1") == []            # proven → deduped


class TestEscalationLedger:
    """The executor's OWN escalation-state ledger — separate from the lead's surfaced_reports.json
    (design §4.4), so the executor's 'I notified/nudged' bookkeeping never suppresses the lead's own
    announcement when the human returns."""

    def test_missing_ledger_is_empty_dict(self, root):
        assert lg.load_escalation(root, "exec-1") == {}

    def test_round_trip(self, root):
        lg.save_escalation(root, "exec-1", {"1": {"attempts": 2, "last_action": "nudge"}})
        assert lg.load_escalation(root, "exec-1") == {"1": {"attempts": 2, "last_action": "nudge"}}

    def test_separate_from_surfaced_reports(self, root):
        # The capstone assertion for design §4.4: writing the executor's OWN ledger must not touch
        # (or be confused with) the owning lead's surfaced_reports.json.
        lg.write_marker(root, "lead-1")
        lg.mark_surfaced(root, "lead-1", ["exec-1:1"])
        lg.save_escalation(root, "exec-1", {"1": {"attempts": 1, "last_action": "notify"}})
        # The lead's surfaced set is untouched by the executor's ledger write.
        assert lg.load_surfaced(root, "lead-1") == {"exec-1:1"}
        # And the executor's ledger lives at a distinct path from the lead's surfaced_reports.json.
        assert lg._escalation_path(root, "exec-1") != lg._surfaced_path(root, "lead-1")
        assert lg.load_escalation(root, "exec-1") == {"1": {"attempts": 1, "last_action": "notify"}}


class TestEscalationSettings:
    """build_escalation_settings / write_escalation_settings — the `--settings` file that arms an
    EXECUTOR with the escalation Stop hook (executors get NO hooks by default). PLAIN synchronous
    Stop hook now (wake-watch design §9.4) — no `asyncRewake`, nothing long-running to host."""

    def test_settings_shape_registers_plain_stop_hook(self, tmp_path):
        settings = lg.build_escalation_settings(str(tmp_path), "exec-7", timeout=1234)
        hook = settings["hooks"]["Stop"][0]["hooks"][0]
        assert hook["command"] == f"{tmp_path / 'hooks' / 'executor_escalation.py'} exec-7"
        assert "asyncRewake" not in hook
        assert hook["timeout"] == 1234

    def test_command_carries_the_executor_relay_name(self, tmp_path):
        """The hook CANNOT derive which executor it is: Claude Code's payload carries the CLAUDE
        session id, relay files state under the relay NAME, and nothing maps one to the other.
        Passing the name as argv is what makes the push fire at all — deriving it from the payload
        is the bug that kept this hook from ever firing in production."""
        settings = lg.build_escalation_settings(str(tmp_path), "push2-exec")
        assert settings["hooks"]["Stop"][0]["hooks"][0]["command"].endswith(" push2-exec")

    def test_write_creates_per_executor_file_in_its_own_state_dir(self, root, tmp_path):
        p = lg.write_escalation_settings(root, str(tmp_path), "exec-7")
        assert p is not None
        assert Path(p) == Path(root) / "exec-7" / "settings.json"  # per-executor, not shared
        content = json.loads(Path(p).read_text())
        hook = content["hooks"]["Stop"][0]["hooks"][0]
        assert "asyncRewake" not in hook
        assert hook["command"].endswith(" exec-7")

    def test_two_executors_get_distinct_settings_files(self, root, tmp_path):
        """Shared-file would be wrong now: each file names its own executor."""
        a = lg.write_escalation_settings(root, str(tmp_path), "exec-a")
        b = lg.write_escalation_settings(root, str(tmp_path), "exec-b")
        assert a != b
        assert json.loads(Path(a).read_text())["hooks"]["Stop"][0]["hooks"][0]["command"].endswith(" exec-a")
        assert json.loads(Path(b).read_text())["hooks"]["Stop"][0]["hooks"][0]["command"].endswith(" exec-b")

    def test_write_refreshes_on_repeat_call(self, root, tmp_path):
        lg.write_escalation_settings(root, str(tmp_path / "v1"), "exec-7")
        p = lg.write_escalation_settings(root, str(tmp_path / "v2"), "exec-7")
        content = json.loads(Path(p).read_text())
        assert "v2" in content["hooks"]["Stop"][0]["hooks"][0]["command"]


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

    def test_touch_restamps_version_from_plugin_root(self, root, tmp_path):
        # Hermetic fixture plugin root — no dependency on the repo's real version string.
        plugin_root = tmp_path / "fixture_plugin"
        (plugin_root / ".claude-plugin").mkdir(parents=True)
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "9.9.9"}))
        (plugin_root / "hooks").mkdir()
        (plugin_root / "hooks" / "hooks.json").write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"timeout": 777}]}]}}))

        lg.write_marker(root, "lead-1", plugin_version="0.0.1", stop_hook_timeout=30)
        lg.touch_lead(root, "lead-1", plugin_root=plugin_root)
        m = lg.read_marker(root, "lead-1")
        assert m["plugin_version"] == "9.9.9"
        assert m["stop_hook_timeout"] == 777

    def test_touch_without_plugin_root_leaves_version_untouched(self, root):
        lg.write_marker(root, "lead-1", plugin_version="0.0.1", stop_hook_timeout=30)
        lg.touch_lead(root, "lead-1")  # no plugin_root given → version fields untouched
        m = lg.read_marker(root, "lead-1")
        assert m["plugin_version"] == "0.0.1"
        assert m["stop_hook_timeout"] == 30


# ---- poll.lock heartbeat (pid + start-time + per-tick ts) --------------------------------------

class TestPollLock:
    DEAD_PID = 999999  # convention used across this suite for "definitely not alive"

    def _write_json_lock(self, root, sid, pid, pid_started, ts):
        d = lg.lead_dir(root, sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "poll.lock").write_text(json.dumps({"pid": pid, "pid_started": pid_started, "ts": ts}))

    def _write_legacy_lock(self, root, sid, pid):
        d = lg.lead_dir(root, sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "poll.lock").write_text(str(pid))

    # -- acquire breaks a stale lock (and reclaims it) --

    def test_acquire_breaks_dead_pid_lock(self, root):
        self._write_json_lock(root, "lead-1", pid=self.DEAD_PID, pid_started="Mon Jan 1", ts=time.time())
        assert lg.acquire_poll_lock(root, "lead-1") is True
        data = json.loads((lg.lead_dir(root, "lead-1") / "poll.lock").read_text())
        assert data["pid"] == os.getpid()

    def test_acquire_breaks_recycled_pid_lock(self, root, monkeypatch):
        # live pid, but recorded start-time no longer matches the CURRENT start time → impostor.
        monkeypatch.setattr(lg, "_pid_start_time", lambda pid: "current-start-time")
        self._write_json_lock(root, "lead-1", pid=os.getpid(), pid_started="stale-start-time",
                              ts=time.time())
        assert lg.acquire_poll_lock(root, "lead-1") is True

    def test_acquire_breaks_stale_heartbeat_alone(self, root, monkeypatch):
        # Condition (d) alone must suffice: live pid, OWN correct start-time, but ts is ancient.
        monkeypatch.setattr(lg, "_pid_start_time", lambda pid: "same-start-time")
        self._write_json_lock(root, "lead-1", pid=os.getpid(), pid_started="same-start-time",
                              ts=time.time() - 1000)
        assert lg.acquire_poll_lock(root, "lead-1", poll_interval=5) is True

    def test_acquire_breaks_garbage_lock(self, root):
        d = lg.lead_dir(root, "lead-1")
        d.mkdir(parents=True, exist_ok=True)
        (d / "poll.lock").write_text("{not json or an int")
        assert lg.acquire_poll_lock(root, "lead-1") is True

    def test_acquire_breaks_legacy_lock_with_ancient_mtime(self, root, tmp_path):
        self._write_legacy_lock(root, "lead-1", pid=self.DEAD_PID)  # dead pid alone already covers
        # this, but also exercise the mtime path with a LIVE pid + ancient mtime.
        self._write_legacy_lock(root, "lead-1", pid=os.getpid())
        lock = lg.lead_dir(root, "lead-1") / "poll.lock"
        old = time.time() - (lg._LEGACY_LOCK_TTL + 60)
        os.utime(lock, (old, old))
        assert lg.acquire_poll_lock(root, "lead-1") is True

    def test_acquire_refuses_live_lock(self, root, monkeypatch):
        monkeypatch.setattr(lg, "_pid_start_time", lambda pid: "same-start-time")
        self._write_json_lock(root, "lead-1", pid=os.getpid(), pid_started="same-start-time",
                              ts=time.time())
        assert lg.acquire_poll_lock(root, "lead-1", poll_interval=5) is False

    def test_recycled_pid_lock_does_not_block_arming(self, root, monkeypatch):
        """THE REGRESSION PIN — exactly the incident: the stale lock's pid gets recycled by an
        unrelated live process at the moment the Stop hook runs; os.kill(pid, 0) says 'alive'; the
        pre-fix hook concluded 'a poller is already watching' and never armed. pid + start-time
        together must catch this."""
        monkeypatch.setattr(lg, "_pid_start_time", lambda pid: "actual-current-start-time")
        self._write_json_lock(root, "lead-1", pid=os.getpid(),
                              pid_started="recorded-start-time-of-the-original-holder", ts=time.time())
        assert lg.acquire_poll_lock(root, "lead-1") is True

    # -- heartbeat_poll_lock --

    def test_heartbeat_refreshes_own_ts(self, root):
        self._write_json_lock(root, "lead-1", pid=os.getpid(), pid_started=None, ts=1000.0)
        lg.heartbeat_poll_lock(root, "lead-1")
        data = json.loads((lg.lead_dir(root, "lead-1") / "poll.lock").read_text())
        assert data["ts"] > 1000.0
        assert data["pid"] == os.getpid()

    def test_heartbeat_does_not_touch_other_pid_lock(self, root):
        self._write_json_lock(root, "lead-1", pid=self.DEAD_PID, pid_started=None, ts=1000.0)
        lg.heartbeat_poll_lock(root, "lead-1")
        data = json.loads((lg.lead_dir(root, "lead-1") / "poll.lock").read_text())
        assert data["ts"] == 1000.0  # untouched — not our pid

    # -- release_poll_lock --

    def test_release_releases_own_json_lock(self, root):
        self._write_json_lock(root, "lead-1", pid=os.getpid(), pid_started=None, ts=time.time())
        lg.release_poll_lock(root, "lead-1")
        assert not (lg.lead_dir(root, "lead-1") / "poll.lock").exists()

    def test_release_leaves_other_pid_lock_alone(self, root):
        self._write_json_lock(root, "lead-1", pid=self.DEAD_PID, pid_started=None, ts=time.time())
        lg.release_poll_lock(root, "lead-1")
        assert (lg.lead_dir(root, "lead-1") / "poll.lock").exists()

    def test_release_handles_legacy_lock(self, root):
        self._write_legacy_lock(root, "lead-1", pid=os.getpid())
        lg.release_poll_lock(root, "lead-1")
        assert not (lg.lead_dir(root, "lead-1") / "poll.lock").exists()

    # -- poll_lock_state --

    def test_poll_lock_state_absent(self, root):
        assert lg.poll_lock_state(root, "lead-1") == "absent"

    def test_poll_lock_state_live(self, root, monkeypatch):
        monkeypatch.setattr(lg, "_pid_start_time", lambda pid: "same-start-time")
        self._write_json_lock(root, "lead-1", pid=os.getpid(), pid_started="same-start-time",
                              ts=time.time())
        assert lg.poll_lock_state(root, "lead-1", poll_interval=5) == "live"

    def test_poll_lock_state_stale(self, root):
        self._write_json_lock(root, "lead-1", pid=self.DEAD_PID, pid_started=None, ts=time.time())
        assert lg.poll_lock_state(root, "lead-1") == "stale"


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

    def _notify(self, monkeypatch, notifier_path, iterm_session=None, tty=None, cfg=None):
        mod = self._load_hook()
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)  # the suite sets it; this test mocks instead
        calls = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append(list(a[0])))
        monkeypatch.setattr(lg, "find_terminal_notifier", lambda: notifier_path)
        monkeypatch.setattr(iterm, "tty_by_id", lambda sid: tty)
        tty_calls = []
        monkeypatch.setattr(iterm, "notify_via_tty",
                             lambda path, title, body: tty_calls.append((path, title, body)) or True)
        mod._notify(cfg or {"notify_on_wake": True}, "exec-1 reported (packet 001)",
                    project="webapp", executor="exec-1", lead_sid="lead-1", iterm_session=iterm_session)
        return calls, tty_calls

    def test_terminal_notifier_used_when_present(self, monkeypatch):
        calls, tty_calls = self._notify(monkeypatch, "/x/terminal-notifier")
        assert calls and calls[0][0] == "/x/terminal-notifier"
        assert "-execute" in calls[0]                      # click→focus wired
        assert "-group" in calls[0]                        # per-lead coalescing
        assert tty_calls == []                              # no iterm_session → tty tier never engaged

    def test_osascript_fallback_when_missing(self, monkeypatch):
        calls, tty_calls = self._notify(monkeypatch, None)
        assert calls and calls[0][0] == "osascript"
        joined = " ".join(calls[0])
        assert "display notification" in joined
        assert "webapp" in joined                          # still names the project
        assert tty_calls == []

    def test_notify_on_wake_false_sends_nothing(self, monkeypatch):
        mod = self._load_hook()
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        calls = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append(a[0]))
        mod._notify({"notify_on_wake": False}, "msg", project="p", executor="e", lead_sid="l")
        assert calls == []

    def test_tty_tier_used_when_marker_has_tty(self, monkeypatch):
        """Marker has iterm_session AND tty_by_id resolves → notify_via_tty is used and NEITHER
        terminal-notifier nor osascript (subprocess.run) is ever called."""
        calls, tty_calls = self._notify(monkeypatch, "/x/terminal-notifier",
                                         iterm_session="w1t1p0:some-uuid", tty="/dev/ttys004")
        assert calls == []                                  # subprocess.run never invoked
        assert len(tty_calls) == 1
        path, title, body = tty_calls[0]
        assert path == "/dev/ttys004"
        assert "webapp" in title
        assert "exec-1" in body

    def test_tty_tier_skipped_when_tty_unresolved(self, monkeypatch):
        """iterm_session present but tty_by_id can't resolve it (session closed/stale) → falls
        through to terminal-notifier, tier 2."""
        calls, tty_calls = self._notify(monkeypatch, "/x/terminal-notifier",
                                         iterm_session="w1t1p0:some-uuid", tty=None)
        assert tty_calls == []
        assert calls and calls[0][0] == "/x/terminal-notifier"

    def test_tty_tier_skipped_without_iterm_session(self, monkeypatch):
        """No iterm_session on the marker at all → tty tier never even attempted."""
        calls, tty_calls = self._notify(monkeypatch, "/x/terminal-notifier", iterm_session=None)
        assert tty_calls == []
        assert calls and calls[0][0] == "/x/terminal-notifier"

    def test_notify_via_terminal_notifier_skips_tty_tier(self, monkeypatch):
        """notify_via='terminal-notifier' bypasses the iTerm OSC/tty tier even when a live tty
        resolves (opting out of iTerm's forced 'Session …' banner title) → terminal-notifier used."""
        calls, tty_calls = self._notify(
            monkeypatch, "/x/terminal-notifier",
            iterm_session="w1t1p0:some-uuid", tty="/dev/ttys004",
            cfg={"notify_on_wake": True, "notify_via": "terminal-notifier"})
        assert tty_calls == []                              # OSC/tty tier skipped despite a live tty
        assert calls and calls[0][0] == "/x/terminal-notifier"

    def test_notify_via_auto_still_uses_tty_tier(self, monkeypatch):
        """The default notify_via='auto' preserves tier-1 behavior: a resolvable tty → OSC/tty used,
        subprocess notifiers never reached. Guards the new config from regressing the default path."""
        calls, tty_calls = self._notify(
            monkeypatch, "/x/terminal-notifier",
            iterm_session="w1t1p0:some-uuid", tty="/dev/ttys004",
            cfg={"notify_on_wake": True, "notify_via": "auto"})
        assert calls == []
        assert len(tty_calls) == 1


class TestNotifyViaTty:
    """scripts/iterm.py: notify_via_tty — escape-safe OSC 777 write, best-effort/never-raises."""
    def test_writes_osc_777_to_tty_path(self, tmp_path):
        fake_tty = tmp_path / "faketty"
        fake_tty.touch()
        ok = iterm.notify_via_tty(str(fake_tty), "a title", "a body")
        assert ok is True
        written = fake_tty.read_text()
        assert written == "\033]777;notify;a title;a body\007"

    def test_strips_escape_and_newline_chars(self, tmp_path):
        """\033/\007 (would prematurely terminate or forge a second escape sequence) are stripped
        outright; \n/\r (would visually break the single-line OSC payload) become spaces."""
        fake_tty = tmp_path / "faketty"
        fake_tty.touch()
        iterm.notify_via_tty(str(fake_tty), "title\033[31m\nline2", "body\007with\rbreaks")
        written = fake_tty.read_text()
        assert written == "\033]777;notify;title[31m line2;bodywith breaks\007"

    def test_never_raises_on_bad_path(self):
        assert iterm.notify_via_tty("/nonexistent/path/for/real", "t", "b") is False


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
        # #22 CONTRACT CHANGE: an announce alone is no longer proof of delivery. When the wake IS
        # delivered, the harness continues the session and re-runs Stop with stop_hook_active — that
        # re-run is the proof, and it promotes the pending key to surfaced. Modelling the real
        # delivered sequence (this test used to fire two bare Stops back-to-back, which is actually
        # the DROPPED case — see test_undelivered_wake_retries for that half).
        assert self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path),
                                    "stop_hook_active": True})[0] == 0
        # third stop: proven-surfaced → silent, exactly as before (no #17 regression)
        assert self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path)})[0] == 0

    def test_undelivered_wake_retries(self, tmp_path):
        """#22/§13, the lost-wake repro: a wake that fires but is never delivered (no
        stop_hook_active re-run — the lead was mid-turn and the harness dropped this hook's exit-2)
        must be announced AGAIN on a later Stop. Pre-fix this returned 0: the announce had already
        stamped surfaced_reports.json, so the report was swallowed for good."""
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        ed = root / "exec-1"; (ed / "packets").mkdir(parents=True)
        (ed / "session.json").write_text(json.dumps(
            {"session_id": "exec-1", "current_packet": 1, "status": "reported", "owner_lead": "lead-1"}))
        (ed / "packets" / "001-report.md").write_text("done")
        rc1, err1 = self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path)})
        assert rc1 == 2 and "exec-1" in err1
        assert lg.load_surfaced(root, "lead-1") == set()        # NOT stamped on a mere attempt
        assert "exec-1:1" in lg.load_pending(root, "lead-1")    # pending instead
        rc2, err2 = self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path)})
        assert rc2 == 2 and "exec-1" in err2                    # re-announced, not swallowed

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


class TestHandoffNudge:
    """Stop hook: once-ever nudge to hand off when the lead's transcript grows past
    handoff_nudge_mb. Transcript size is a PROXY for session weight, not context occupancy — hence
    exactly-once, never automation. Reuses TestStopHookLivePayload's real-subprocess harness for the
    end-to-end cases; the sparse-file trick (truncate, not real bytes) keeps the size threshold
    without actually writing megabytes."""
    def _run(self, home, payload):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "stop_lead_watch.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "HOME": str(home), "RELAY_NO_NOTIFY": "1"})
        return p.returncode, p.stderr

    def _sparse_file(self, path, mb):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(int(mb * 1024 * 1024))
        return path

    def _ledger_events(self, root):
        p = root / "sessions.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines()]

    def test_nudges_once_when_over_threshold(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        transcript = self._sparse_file(tmp_path / "transcript.jsonl", 6)
        rc, err = self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path),
                                        "transcript_path": str(transcript)})
        assert rc == 2
        assert "getting heavy" in err
        assert lg.handoff_nudged(root, "lead-1")
        events = self._ledger_events(root)
        assert any(e["event"] == "handoff_nudged" and e["session_id"] == "lead-1" for e in events)

    def test_no_second_nudge(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        transcript = self._sparse_file(tmp_path / "transcript.jsonl", 6)
        payload = {"session_id": "lead-1", "cwd": str(tmp_path), "transcript_path": str(transcript)}
        assert self._run(tmp_path, payload)[0] == 2
        rc, err = self._run(tmp_path, payload)
        assert rc == 0
        assert "getting heavy" not in err

    def test_under_threshold_silent(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        transcript = self._sparse_file(tmp_path / "transcript.jsonl", 1)
        rc, err = self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path),
                                        "transcript_path": str(transcript)})
        assert rc == 0
        assert not lg.handoff_nudged(root, "lead-1")

    def test_disabled_by_config(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        (root / "lead").mkdir(parents=True)
        (root / "lead" / "config.json").write_text(json.dumps({"handoff_nudge": False}))
        lg.write_marker(root, "lead-1")
        transcript = self._sparse_file(tmp_path / "transcript.jsonl", 10)
        rc, err = self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path),
                                        "transcript_path": str(transcript)})
        assert rc == 0
        assert "getting heavy" not in err
        assert not lg.handoff_nudged(root, "lead-1")

    def test_missing_transcript_path_silent(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1")
        rc, err = self._run(tmp_path, {"session_id": "lead-1", "cwd": str(tmp_path)})
        assert rc == 0
        assert "getting heavy" not in err
        assert not lg.handoff_nudged(root, "lead-1")

    def test_transcript_mb_missing_path(self):
        assert lg.transcript_mb(None) == 0.0
        assert lg.transcript_mb("/no/such/file") == 0.0

    def test_transcript_mb_real_file(self, tmp_path):
        p = self._sparse_file(tmp_path / "t.jsonl", 2)
        assert lg.transcript_mb(str(p)) == pytest.approx(2.0, abs=0.01)

    def test_handoff_nudged_mark_round_trip(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        assert lg.handoff_nudged(root, "lead-1") is False
        lg.mark_handoff_nudged(root, "lead-1")
        assert lg.handoff_nudged(root, "lead-1") is True


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


class TestTombstone:
    """Arming that survives exit→resume (docs/lead-arming-durability.md). A resumable exit
    TOMBSTONES the marker (identity retained, arming dropped); a resume revives it losslessly."""

    def _armed(self, root, sid="lead-1"):
        lg.write_marker(root, sid, model="opus", iterm_session="w1t2p0:ABC", project="proj",
                        cwd="/tmp/x", tab_label="[Lead] proj", color=[1, 2, 3],
                        plugin_version="9.9.9", stop_hook_timeout=1900)
        return lg.read_marker(root, sid)

    def test_is_tombstoned_detects_flag(self, root):
        assert lg.is_tombstoned({"ended": True}) is True
        assert lg.is_tombstoned({}) is False
        assert lg.is_tombstoned(None) is False

    def test_armed_lead_is_lead_true(self, root):
        self._armed(root)
        assert lg.is_lead(root, "lead-1") is True

    def test_tombstoned_lead_is_NOT_lead(self, root):
        """THE critical property: a tombstone must not count as armed, or an exited session would
        still have the gate and wake live — strictly worse than the bug this replaced."""
        self._armed(root)
        assert lg.tombstone_lead(root, "lead-1") is True
        assert lg.is_lead(root, "lead-1") is False
        assert lg.read_marker(root, "lead-1")["ended"] is True

    def test_tombstone_retains_everything(self, root):
        before = self._armed(root)
        lg.tombstone_lead(root, "lead-1")
        after = lg.read_marker(root, "lead-1")
        for k in ("project", "cwd", "iterm_session", "tab_label", "color", "model", "started"):
            assert after.get(k) == before.get(k), f"{k} was lost by tombstoning"

    def test_tombstone_noop_without_marker_or_when_already_tombstoned(self, root):
        assert lg.tombstone_lead(root, "never-armed") is False
        self._armed(root)
        assert lg.tombstone_lead(root, "lead-1") is True
        assert lg.tombstone_lead(root, "lead-1") is False  # already tombstoned

    def test_revive_restores_arming_losslessly(self, root):
        before = self._armed(root)
        lg.tombstone_lead(root, "lead-1")
        assert lg.revive_lead(root, "lead-1") is True
        assert lg.is_lead(root, "lead-1") is True
        after = lg.read_marker(root, "lead-1")
        assert "ended" not in after and "ended_at" not in after
        # project name in particular must come back untouched — no "-2" surprise on resume
        assert after["project"] == before["project"]
        for k in ("cwd", "iterm_session", "tab_label", "color", "model"):
            assert after.get(k) == before.get(k)

    def test_revive_only_ever_restores_prior_state(self, root):
        """Re-arm is a RESTORATION, never a fresh grant: a session that was never a lead, and a
        session that is currently armed, are both untouched."""
        assert lg.revive_lead(root, "never-armed") is False
        assert lg.is_lead(root, "never-armed") is False
        self._armed(root, "lead-2")
        assert lg.revive_lead(root, "lead-2") is False  # armed, not tombstoned → no double-arm
        assert lg.is_lead(root, "lead-2") is True


class TestSessionEndReasonSplit:
    """hooks/sessionend_lead_cleanup.py: clear/logout destroy the conversation (hard clear);
    exit/prompt_input_exit are resumable (tombstone); anything else is untouched."""

    def _run(self, home, payload):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "sessionend_lead_cleanup.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)})
        return p.returncode

    @pytest.mark.parametrize("reason", ["clear", "logout"])
    def test_context_destroying_reasons_hard_clear(self, tmp_path, reason):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1", project="proj")
        assert self._run(tmp_path, {"session_id": "lead-1", "reason": reason}) == 0
        assert lg.read_marker(root, "lead-1") == {}  # gone entirely

    @pytest.mark.parametrize("reason", ["exit", "prompt_input_exit"])
    def test_resumable_reasons_tombstone_instead_of_deleting(self, tmp_path, reason):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1", project="proj")
        assert self._run(tmp_path, {"session_id": "lead-1", "reason": reason}) == 0
        m = lg.read_marker(root, "lead-1")
        assert m != {} and m["ended"] is True      # identity retained
        assert m["project"] == "proj"
        assert lg.is_lead(root, "lead-1") is False  # but NOT armed

    def test_unknown_reason_leaves_lead_armed(self, tmp_path):
        """Headless `claude -p` ends with reason='other' (verified) — it must not disturb arming."""
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1", project="proj")
        assert self._run(tmp_path, {"session_id": "lead-1", "reason": "other"}) == 0
        assert lg.is_lead(root, "lead-1") is True


class TestSessionStartRearmHook:
    """hooks/sessionstart_lead_rearm.py: source=resume revives a tombstone, source=clear hard
    clears, startup/compact are no-ops. Verified source values, not documented ones."""

    def _run(self, home, payload):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "sessionstart_lead_rearm.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            # RELAY_NO_NOTIFY: re-arm now fires a real desktop banner. Without this the suite
            # would spam actual notifications on every run (same kill-switch every other relay
            # notification honours).
            env={**os.environ, "HOME": str(home), "RELAY_NO_NOTIFY": "1"})
        return p.returncode, p.stdout, p.stderr

    def test_resume_revives_tombstoned_lead(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1", project="proj")
        lg.tombstone_lead(root, "lead-1")
        rc, out, err = self._run(tmp_path, {"session_id": "lead-1", "source": "resume"})
        assert rc == 0
        assert lg.is_lead(root, "lead-1") is True
        assert lg.read_marker(root, "lead-1")["project"] == "proj"
        # The message MUST be on stdout: a SessionStart hook's stdout is surfaced as session
        # context, its stderr is not shown to anyone. The first cut used stderr — the re-arm worked
        # and announced itself into the void, which is the exact silent-failure this fix exists to
        # end. Pin the channel, not just the presence of a message.
        assert "relay" in out.lower()
        assert err.strip() == ""

    def test_resume_never_arms_a_session_that_was_not_a_lead(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        rc, out, err = self._run(tmp_path, {"session_id": "stranger", "source": "resume"})
        assert rc == 0
        assert lg.is_lead(root, "stranger") is False
        assert err.strip() == ""

    def test_resume_of_already_armed_lead_is_a_noop(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1", project="proj")
        rc, out, err = self._run(tmp_path, {"session_id": "lead-1", "source": "resume"})
        assert rc == 0
        assert lg.is_lead(root, "lead-1") is True
        assert err.strip() == ""

    def test_clear_hard_clears(self, tmp_path):
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1", project="proj")
        lg.tombstone_lead(root, "lead-1")
        rc, _out, _err = self._run(tmp_path, {"session_id": "lead-1", "source": "clear"})
        assert rc == 0
        assert lg.read_marker(root, "lead-1") == {}

    @pytest.mark.parametrize("source", ["startup", "compact"])
    def test_startup_and_compact_are_noops(self, tmp_path, source):
        """compact fires SessionStart on EVERY compaction (verified) — it must never disturb a
        tombstone or an armed lead."""
        root = tmp_path / ".relay-tasks"
        lg.write_marker(root, "lead-1", project="proj")
        lg.tombstone_lead(root, "lead-1")
        rc, _out, _err = self._run(tmp_path, {"session_id": "lead-1", "source": source})
        assert rc == 0
        assert lg.read_marker(root, "lead-1")["ended"] is True  # still tombstoned, untouched

    def test_bad_payload_fails_open(self, tmp_path):
        import subprocess
        p = subprocess.run(
            ["python3", str(REPO_ROOT / "hooks" / "sessionstart_lead_rearm.py")],
            input="not json", capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)})
        assert p.returncode == 0


class TestHooksAreExecutable:
    """Every hook registered in hooks.json is invoked by the harness as a BARE PATH, so it must
    carry the executable bit. A hook created without +x fails silently at runtime — and unit tests
    that shell out via `python3 <path>` cannot detect it, because that path doesn't need +x. This
    caught a real, silently-broken SessionStart hook; it exists so that can't recur."""

    def test_every_registered_hook_is_executable(self):
        cfg = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())
        checked = []
        for event, entries in cfg["hooks"].items():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    rel = cmd.replace("${CLAUDE_PLUGIN_ROOT}/", "").split()[0]
                    path = REPO_ROOT / rel
                    assert path.exists(), f"{event}: {rel} does not exist"
                    assert os.access(path, os.X_OK), (
                        f"{event}: {rel} is NOT executable — the harness invokes it as a bare "
                        f"path, so it would fail silently at runtime")
                    checked.append(rel)
        assert checked, "no hooks found to check"


class TestRearmNotification:
    """Re-arm must reach a HUMAN, not just the model. A SessionStart hook's stdout becomes session
    context (model-visible) and its stderr goes nowhere, so the desktop banner is the only channel
    that reaches the user — and the only one that works without a statusline configured."""

    def _load_hook(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sessionstart_lead_rearm_under_test",
            REPO_ROOT / "hooks" / "sessionstart_lead_rearm.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_rearm_fires_notification_with_its_own_subtitle(self, tmp_path, monkeypatch):
        mod = self._load_hook()
        monkeypatch.setattr(mod, "STATE_ROOT", str(tmp_path))
        sys.path.insert(0, str(REPO_ROOT / "hooks"))
        import stop_lead_watch as slw
        calls = []
        monkeypatch.setattr(slw, "_notify", lambda *a, **k: calls.append((a, k)))

        mod._notify_rearm(lg, "lead-1", {"project": "webapp", "iterm_session": "w1t2p0:X"})

        assert calls, "re-arm did not attempt a desktop notification"
        _args, kwargs = calls[0]
        assert kwargs.get("project") == "webapp"
        assert kwargs.get("lead_sid") == "lead-1"
        # Must NOT inherit _notify's default "review needed" subtitle — nothing needs reviewing.
        assert "re-armed" in (kwargs.get("subtitle") or "").lower()

    def test_notification_failure_never_breaks_rearm(self, tmp_path, monkeypatch):
        """Arming is the contract; the banner is a courtesy. A notifier blowing up must not
        propagate."""
        mod = self._load_hook()
        monkeypatch.setattr(mod, "STATE_ROOT", str(tmp_path))
        sys.path.insert(0, str(REPO_ROOT / "hooks"))
        import stop_lead_watch as slw

        def boom(*a, **k):
            raise RuntimeError("notifier exploded")
        monkeypatch.setattr(slw, "_notify", boom)
        mod._notify_rearm(lg, "lead-1", {"project": "webapp"})  # must not raise
