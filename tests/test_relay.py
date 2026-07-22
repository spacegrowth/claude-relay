"""
Layer 1 (pure Python, no iTerm/API, CI-able) unit tests for bin/relay's state management:
TEMPLATE_FOOTER consistency, ledger/session.json read-write, packet numbering, and the
busy/reported/stalled/dead transition logic in _check_one.

Run: pytest tests/test_relay.py -v
"""
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
    """Load bin/relay as a module (it has no .py extension since it's a real executable, so the
    loader must be given explicitly), with STATE_ROOT patched to an isolated tmp directory so
    tests never touch ~/.relay-tasks."""
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


class TestSlugify:
    def test_basic(self, relay):
        assert relay.slugify("Bridge MCP Fixlist") == "bridge-mcp-fixlist"

    def test_empty_falls_back(self, relay):
        assert relay.slugify("!!!") == "task"


class TestTemplateFooterConsistency:
    """The direct proof that 'the lead always communicates the same way': the GATES/REPORT
    FORMAT section must be byte-identical regardless of the task-specific packet body."""

    def test_footer_identical_across_different_bodies(self, relay):
        p1 = relay.build_packet("Fix the auth bug in login.py", "/tmp/a/001-report.md", "/path/to/relay diff sess-1", "file:///tmp/a/001-diff.html")
        p2 = relay.build_packet("Implement the new charts grid layout, multi-file", "/tmp/b/002-report.md", "/path/to/relay diff sess-2", "file:///tmp/b/002-diff.html")

        footer1 = p1.split("---\n(relay")[1]
        footer2 = p2.split("---\n(relay")[1]
        # Strip the three legitimately-varying lines (report path, diff_cmd, and diff_url) before comparing.
        footer1_norm = footer1.replace("/tmp/a/001-report.md", "REPORT_PATH").replace("/path/to/relay diff sess-1", "DIFF_CMD").replace("file:///tmp/a/001-diff.html", "DIFF_URL")
        footer2_norm = footer2.replace("/tmp/b/002-report.md", "REPORT_PATH").replace("/path/to/relay diff sess-2", "DIFF_CMD").replace("file:///tmp/b/002-diff.html", "DIFF_URL")
        assert footer1_norm == footer2_norm

    def test_footer_contains_required_sections(self, relay):
        p = relay.build_packet("do the thing", "/tmp/x/001-report.md", "/path/to/relay diff sess-x", "file:///tmp/x/001-diff.html")
        assert "STAGE, NEVER COMMIT" in p
        assert "ONE LOGICAL DELIVERABLE" in p
        assert "/tmp/x/001-report.md" in p
        assert "UNVERIFIED" in p
        assert "VERY FIRST LINE" in p
        assert "relay diff" in p
        assert "sess-x" in p
        assert "diff: file://" in p

    def test_footer_contains_required_tldr_block(self, relay):
        """§6a (task #6): the auto-appended REPORT FORMAT must require a TL;DR block with
        Status/Risk flags/UNVERIFIED/Changed fields, and the UNVERIFIED line must be called out
        as mandatory-even-when-none so its absence is machine-detectable."""
        p = relay.build_packet("do the thing", "/tmp/x/001-report.md", "/path/to/relay diff sess-x", "file:///tmp/x/001-diff.html")
        assert "TL;DR" in p
        assert "Status: clean / clean-with-caveats / blocked / partial" in p
        assert "Risk flags:" in p
        assert "Risk flags: none" in p
        assert "UNVERIFIED:" in p
        assert "UNVERIFIED: none" in p
        assert "MANDATORY" in p
        assert "Changed:" in p
        # TL;DR block must precede the detailed report body it summarizes.
        assert p.index("Status: clean") < p.index("What changed (file:line")

    def test_sentinel_line_contains_correct_diff_url(self, relay):
        p = relay.build_packet("task body", "/tmp/sess-a/001-report.md", "/path/to/relay diff sess-a", "file:///tmp/sess-a/001-diff.html")
        # Verify sentinel line contains the diff URL
        assert "✅ [relay] — staged + report written — diff: file:///tmp/sess-a/001-diff.html — idle, awaiting the lead's review." in p

    def test_packet_002_diff_url_correct_packet_number(self, relay):
        # cmd_send path: packet 002's footer must carry 002-diff.html, not 001
        p = relay.build_packet("second packet", "/tmp/sess-b/002-report.md", "/path/to/relay diff sess-b", "file:///tmp/sess-b/002-diff.html")
        # Verify the sentinel contains 002-diff.html
        assert "002-diff.html" in p
        assert "001-diff.html" not in p
        # Verify sentinel line is present with correct URL
        assert "diff: file:///tmp/sess-b/002-diff.html" in p


class TestPacketNumbering:
    def test_first_packet_is_one(self, relay):
        assert relay.next_packet_number("nonexistent-session") == 1

    def test_increments_past_existing(self, relay, tmp_path):
        d = relay.packets_dir("sess-1")
        d.mkdir(parents=True)
        (d / "001-packet.md").write_text("x")
        (d / "002-packet.md").write_text("x")
        assert relay.next_packet_number("sess-1") == 3


class TestPidAlive:
    def test_current_process_is_alive(self, relay):
        assert relay.pid_alive(os.getpid()) is True

    def test_bogus_pid_is_not_alive(self, relay):
        # A PID very unlikely to exist.
        assert relay.pid_alive(999999) is False

    def test_none_is_not_alive(self, relay):
        assert relay.pid_alive(None) is False


class TestCheckTransitions:
    """_check_one drives busy -> reported/stalled/dead. These are monkeypatched against
    iterm.is_alive so no real AppleScript/iTerm call happens."""

    def _make_session(self, relay, session_id, status="busy", pid=None, busy_since=None):
        relay.session_dir(session_id).mkdir(parents=True)
        relay.packets_dir(session_id).mkdir(parents=True)
        relay.write_session(session_id, {
            "session_id": session_id,
            "worktree": "/tmp/wt",
            "topic": "test",
            "scope": "test",
            "tab_label": f"relay-{session_id}",
            "model": None,
            "pid": pid,
            "status": status,
            "current_packet": 1,
            "busy_since": busy_since or relay.now(),
            "superseded_by": None,
            "created": relay.now(),
            "updated": relay.now(),
        })

    def test_busy_with_report_becomes_reported(self, relay):
        self._make_session(relay, "s1", pid=os.getpid())
        (relay.packets_dir("s1") / "001-report.md").write_text("done")
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            result = relay._check_one("s1")
        assert result["status"] == "reported"

    def test_title_miss_with_live_pid_is_not_dead(self, relay):
        # REGRESSION: a tab-title match miss must NOT override a live process. Claude Code mutates
        # its own tab title while working, so a miss is not death. Live pid + no report → busy.
        # (Replaces the old test_dead_tab_wins_over_everything, which asserted the bug itself.)
        self._make_session(relay, "s2", pid=os.getpid())
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            result = relay._check_one("s2")
        assert result["status"] == "busy"

    def test_reported_survives_title_miss_and_dead_pid(self, relay):
        # A written report is a completed deliverable: reported even if the tab title no longer
        # matches AND the process has since exited. This is the case that was falsely showing
        # `dead reported=True` — a live report must win.
        self._make_session(relay, "s2b", pid=999999)  # not alive
        (relay.packets_dir("s2b") / "001-report.md").write_text("done")
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            result = relay._check_one("s2b")
        assert result["status"] == "reported"

    def test_dead_only_when_pid_gone_and_tab_closed(self, relay):
        # `dead` means genuinely gone: process exited AND tab closed, with no report.
        self._make_session(relay, "s2c", pid=999999)  # not alive
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            result = relay._check_one("s2c")
        assert result["status"] == "dead"

    def test_process_gone_tab_open_no_report_becomes_stalled(self, relay):
        # Process exited with no report, but the tab is still open → stalled (go look at it), not dead.
        self._make_session(relay, "s3", pid=999999)  # bogus, not alive
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            result = relay._check_one("s3")
        assert result["status"] == "stalled"

    def test_busy_process_alive_no_report_stays_busy(self, relay):
        self._make_session(relay, "s4", pid=os.getpid())
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            result = relay._check_one("s4")
        assert result["status"] == "busy"

    def test_stale_busy_past_threshold_becomes_stalled(self, relay):
        old_time = "2020-01-01T00:00:00"
        self._make_session(relay, "s5", pid=os.getpid(), busy_since=old_time)
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            result = relay._check_one("s5")
        assert result["status"] == "stalled"

    def test_closed_session_is_not_touched(self, relay):
        self._make_session(relay, "s6", status="closed")
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            result = relay._check_one("s6")
        assert result["status"] == "closed"  # dead-tab check must not override a manual close


class TestAutoTrust:
    def test_sets_flag_for_new_worktree(self, relay, tmp_path, monkeypatch):
        config_path = tmp_path / ".claude.json"
        config_path.write_text(json.dumps({"projects": {"/already/trusted": {"hasTrustDialogAccepted": True, "other": "keep-me"}}}))
        monkeypatch.setattr(relay.Path, "home", lambda: tmp_path)

        worktree = tmp_path / "some" / "worktree"
        worktree.mkdir(parents=True)
        relay.auto_trust(str(worktree))

        result = json.loads(config_path.read_text())
        assert result["projects"][str(worktree.resolve())]["hasTrustDialogAccepted"] is True
        # Must not clobber unrelated existing entries.
        assert result["projects"]["/already/trusted"]["other"] == "keep-me"

    def test_missing_config_file_is_swallowed(self, relay, tmp_path, monkeypatch):
        monkeypatch.setattr(relay.Path, "home", lambda: tmp_path)  # no .claude.json exists here
        relay.auto_trust(str(tmp_path))  # must not raise


class TestLedger:
    def test_append_ledger_writes_jsonl_line(self, relay):
        relay.append_ledger("spawned", session_id="s1", topic="test")
        lines = relay.LEDGER.read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "spawned"
        assert rec["session_id"] == "s1"


class TestPacketSummary:
    """packet_summary + build_pointer_message: the executor's first message states the ask up front,
    stays single-line, and gracefully degrades to the bare pointer when there's no usable gist."""
    def test_first_heading_is_the_gist(self, relay):
        assert relay.packet_summary("# Fix the three login-page bugs\n\nDetails...") == "Fix the three login-page bugs"

    def test_collapses_whitespace_and_skips_blanks(self, relay):
        assert relay.packet_summary("\n\n   ##   Add   the  dark-mode  toggle  \nmore") == "Add the dark-mode toggle"

    def test_truncates_long_lines(self, relay):
        out = relay.packet_summary("x" * 500, limit=20)
        assert len(out) == 20 and out.endswith("…")

    def test_empty_body_gives_empty(self, relay):
        assert relay.packet_summary("\n   \n") == ""

    def test_pointer_prepends_summary_single_line(self, relay):
        msg = relay.build_pointer_message("/p/001-packet.md", "Fix the Both-view bugs")
        assert msg.startswith("Task — Fix the Both-view bugs. Read and follow")
        assert "\n" not in msg and "/p/001-packet.md" in msg

    def test_pointer_without_summary_is_bare(self, relay):
        msg = relay.build_pointer_message("/p/001-packet.md")
        assert msg.startswith("Read and follow") and "Task —" not in msg


class TestReportCommand:
    def _make(self, relay, sid="s1", packet=1, body="the report body"):
        relay.session_dir(sid).mkdir(parents=True)
        relay.packets_dir(sid).mkdir(parents=True)
        relay.write_session(sid, {"session_id": sid, "current_packet": packet, "status": "reported",
            "topic": "t", "worktree": "/w", "tab_label": f"relay-{sid}", "busy_since": relay.now()})
        (relay.packets_dir(sid) / f"{packet:03d}-report.md").write_text(body)

    def test_report_prints_framed_content(self, relay, capsys):
        self._make(relay, body="staged the fix; tests pass")
        relay.cmd_report(SimpleNamespace(session_id="s1", packet=None))
        out = capsys.readouterr().out
        assert "REPORT READY" in out         # the framed banner
        assert "staged the fix; tests pass" in out   # the actual report body
        assert "s1" in out

    def test_report_missing_errors(self, relay):
        relay.session_dir("s2").mkdir(parents=True)
        relay.write_session("s2", {"session_id": "s2", "current_packet": 1, "status": "busy",
            "topic": "t", "worktree": "/w", "tab_label": "relay-s2", "busy_since": relay.now()})
        with pytest.raises(SystemExit):
            relay.cmd_report(SimpleNamespace(session_id="s2", packet=None))

    def test_color_off_when_not_tty(self, relay):
        # Tests capture stdout (not a tty) → no ANSI escapes leak into output.
        assert relay.c("hello", "green", "bold") == "hello"


class TestSpawnSkipPerms:
    """cmd_spawn's --dangerously-skip-permissions decision: per-spawn flag wins, else config default
    (executor_skip_permissions, default False). iterm.spawn/auto_trust/read_pid are mocked so no
    real iTerm/AppleScript/~/.claude.json is touched."""
    def _run(self, relay, tmp_path, cfg=None, skip_perms=None):
        pkt = tmp_path / "packet.md"; pkt.write_text("do the thing")
        if cfg is not None:
            (relay.STATE_ROOT / "lead").mkdir(parents=True, exist_ok=True)
            (relay.STATE_ROOT / "lead" / "config.json").write_text(json.dumps(cfg))
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123):
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path), topic="t", packet=str(pkt),
                model=None, name="s1", scope=None, skip_perms=skip_perms, pane=None))
        return captured

    def test_default_is_no_skip(self, relay, tmp_path):
        assert self._run(relay, tmp_path)["skip_perms"] is False           # no config, no flag

    def test_config_true_skips(self, relay, tmp_path):
        assert self._run(relay, tmp_path, cfg={"executor_skip_permissions": True})["skip_perms"] is True

    def test_no_skip_flag_overrides_config_on(self, relay, tmp_path):
        # config says skip, but --no-skip-perms (skip_perms=False) forces prompting
        assert self._run(relay, tmp_path, cfg={"executor_skip_permissions": True}, skip_perms=False)["skip_perms"] is False

    def test_rejects_missing_worktree(self, relay, tmp_path):
        # a relative/bogus worktree must fail loudly, not produce a silently-broken `cd` in the tab
        pkt = tmp_path / "packet.md"; pkt.write_text("x")
        with pytest.raises(SystemExit) as ei:
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path / "nope"), topic="t", packet=str(pkt),
                model=None, name="s1", scope=None, skip_perms=None, pane=None))
        assert "worktree not found" in str(ei.value)

    def test_worktree_stored_absolute(self, relay, tmp_path):
        cap = self._run(relay, tmp_path)
        assert cap["cwd"] == str(tmp_path.resolve())              # spawn cd is absolute
        assert relay.read_session("s1")["worktree"] == str(tmp_path.resolve())

    def test_skip_flag_overrides_config_off(self, relay, tmp_path):
        assert self._run(relay, tmp_path, skip_perms=True)["skip_perms"] is True   # flag on, no config


class TestSpawnExecutorEscalation:
    """cmd_spawn arms the executor-side wake escalation Stop hook (wake-watch design §9) via
    `--settings` — executors get no hooks by default, so bin/relay must generate and pass this file
    explicitly at spawn time. iterm.spawn/auto_trust/read_pid mocked; no real iTerm touched."""

    def _run(self, relay, tmp_path, cfg=None):
        pkt = tmp_path / "packet.md"; pkt.write_text("do the thing")
        if cfg is not None:
            (relay.STATE_ROOT / "lead").mkdir(parents=True, exist_ok=True)
            (relay.STATE_ROOT / "lead" / "config.json").write_text(json.dumps(cfg))
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123):
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path), topic="t", packet=str(pkt),
                model=None, name="s1", scope=None, skip_perms=None, pane=None))
        return captured

    def test_settings_file_present_and_points_at_escalation_hook(self, relay, tmp_path):
        cap = self._run(relay, tmp_path)
        settings_file = cap.get("settings_file")
        assert settings_file is not None
        content = json.loads(Path(settings_file).read_text())
        hook = content["hooks"]["Stop"][0]["hooks"][0]
        assert "hooks/executor_escalation.py" in hook["command"]
        assert hook["command"].endswith(" s1")  # relay NAME passed as argv — see lead_guard
        assert "asyncRewake" not in hook  # plain synchronous push (§9.4) — nothing to host async

    def test_kill_switch_omits_settings_file(self, relay, tmp_path):
        cap = self._run(relay, tmp_path, cfg={"executor_escalation": False})
        assert cap.get("settings_file") is None

    def test_build_claude_cmd_includes_settings_flag(self, relay, tmp_path):
        # End-to-end through the real (unmocked) build_claude_cmd: --settings actually lands in the
        # launched command line, pointed at the written file.
        settings_path = relay.lead_guard.write_escalation_settings(
            relay.STATE_ROOT, relay._plugin_root(), "s1")
        cmd = relay.iterm.build_claude_cmd("do the thing", settings_file=settings_path)
        assert f"--settings {settings_path}" in cmd


class TestSpawnLayout:
    """cmd_spawn's pane-vs-tab decision: --pane/--tab wins, else config default (executor_layout,
    default "tab") — same tri-state pattern as TestSpawnSkipPerms. iterm.spawn/auto_trust/read_pid
    mocked so no real iTerm/AppleScript is touched."""
    def _run(self, relay, tmp_path, cfg=None, pane=None):
        pkt = tmp_path / "packet.md"; pkt.write_text("do the thing")
        if cfg is not None:
            (relay.STATE_ROOT / "lead").mkdir(parents=True, exist_ok=True)
            (relay.STATE_ROOT / "lead" / "config.json").write_text(json.dumps(cfg))
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123):
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path), topic="t", packet=str(pkt),
                model=None, name="s1", scope=None, skip_perms=None, pane=pane))
        return captured

    def test_default_is_tab(self, relay, tmp_path):
        assert self._run(relay, tmp_path)["layout"] == "tab"          # no config, no flag

    def test_config_pane(self, relay, tmp_path):
        assert self._run(relay, tmp_path, cfg={"executor_layout": "pane"})["layout"] == "pane"

    def test_tab_flag_overrides_config_pane(self, relay, tmp_path):
        # config says pane, but --tab (pane=False) forces a tab for this spawn
        assert self._run(relay, tmp_path, cfg={"executor_layout": "pane"}, pane=False)["layout"] == "tab"

    def test_pane_flag_overrides_config_tab(self, relay, tmp_path):
        assert self._run(relay, tmp_path, pane=True)["layout"] == "pane"   # flag on, no config


class TestSpawnModel:
    """cmd_spawn's model policy (LIVE INCIDENT, 2026-07-12): an executor spawned without --model
    must launch on relay's own executor_default_model, never the CLI's personal /model default, and
    a requested model above executor_model_ceiling is refused without --model-override. iterm.spawn/
    auto_trust/read_pid mocked so no real iTerm/AppleScript is touched."""
    def _run(self, relay, tmp_path, cfg=None, model=None, model_override=None, name="s1"):
        pkt = tmp_path / "packet.md"; pkt.write_text("do the thing")
        if cfg is not None:
            (relay.STATE_ROOT / "lead").mkdir(parents=True, exist_ok=True)
            (relay.STATE_ROOT / "lead" / "config.json").write_text(json.dumps(cfg))
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123):
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path), topic="t", packet=str(pkt),
                model=model, model_override=model_override, name=name, scope=None,
                skip_perms=None, pane=None))
        return captured

    def _ledger_events(self, relay):
        return [json.loads(l) for l in relay.LEDGER.read_text().splitlines()] if relay.LEDGER.exists() else []

    def test_omitted_model_launches_and_stores_config_default(self, relay, tmp_path):
        cap = self._run(relay, tmp_path)
        assert cap["model"] == "sonnet"                          # built-in default
        assert relay.read_session("s1")["model"] == "sonnet"     # honest display — never null

    def test_omitted_model_uses_configured_default(self, relay, tmp_path):
        cap = self._run(relay, tmp_path, cfg={"executor_default_model": "haiku"})
        assert cap["model"] == "haiku"
        assert relay.read_session("s1")["model"] == "haiku"

    def test_explicit_model_within_ceiling_passes_through(self, relay, tmp_path):
        cap = self._run(relay, tmp_path, model="opus")
        assert cap["model"] == "opus"
        assert relay.read_session("s1")["model"] == "opus"

    def test_model_above_ceiling_refused(self, relay, tmp_path):
        with pytest.raises(SystemExit) as ei:
            self._run(relay, tmp_path, model="claude-fable-5")
        assert "ceiling" in str(ei.value)
        assert "--model-override" in str(ei.value)
        assert relay.read_session("s1") is None    # refused before any session was recorded

    def test_model_above_ceiling_with_override_succeeds_and_ledgers(self, relay, tmp_path):
        cap = self._run(relay, tmp_path, model="claude-fable-5", model_override="benchmarking a hard case")
        assert cap["model"] == "claude-fable-5"
        events = self._ledger_events(relay)
        overrides = [e for e in events if e["event"] == "model_ceiling_override"]
        assert len(overrides) == 1
        assert overrides[0]["model"] == "claude-fable-5"
        assert overrides[0]["reason"] == "benchmarking a hard case"

    def test_unknown_tier_name_refused_by_default(self, relay, tmp_path):
        # A model string containing no tier word this list recognizes must fail CLOSED, not open —
        # tomorrow's new top-tier model is caught by default instead of silently sailing through.
        with pytest.raises(SystemExit) as ei:
            self._run(relay, tmp_path, model="some-future-model-xyz")
        assert "ceiling" in str(ei.value)

    def test_custom_ceiling_allows_higher_default_tier(self, relay, tmp_path):
        cap = self._run(relay, tmp_path, cfg={"executor_model_ceiling": "fable"}, model="opus")
        assert cap["model"] == "opus"   # opus <= fable ceiling, no override needed


class TestSpawnOwnership:
    """cmd_spawn stamps the executor's session.json with its owning lead + project. --lead wins,
    else $CLAUDE_CODE_SESSION_ID, else unowned (None). owner_project is read from the lead's marker.
    iterm.spawn/auto_trust/read_pid mocked so no real iTerm is touched."""
    def _spawn(self, relay, tmp_path, sid, lead=None, env=None):
        pkt = tmp_path / "packet.md"; pkt.write_text("do the thing")
        with mock.patch.object(relay.iterm, "spawn"), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.dict(os.environ, env or {}, clear=False):
            if env is not None and "CLAUDE_CODE_SESSION_ID" not in env:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path), topic="t", packet=str(pkt),
                model=None, name=sid, scope=None, skip_perms=None, pane=None, lead=lead))
        return relay.read_session(sid)

    def test_stamps_owner_from_lead_flag_and_marker(self, relay, tmp_path):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp")
        s = self._spawn(relay, tmp_path, "e1", lead="lead-1")
        assert s["owner_lead"] == "lead-1"
        assert s["owner_project"] == "webapp"

    def test_owner_lead_defaults_to_env_session_id(self, relay, tmp_path):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-env", project="proj-x")
        s = self._spawn(relay, tmp_path, "e1", env={"CLAUDE_CODE_SESSION_ID": "lead-env"})
        assert s["owner_lead"] == "lead-env"
        assert s["owner_project"] == "proj-x"

    def test_owned_but_lead_marker_missing_gives_none_project(self, relay, tmp_path):
        # owner_lead set, but that lead has no marker → project degrades to None (not an error)
        s = self._spawn(relay, tmp_path, "e1", lead="ghost-lead")
        assert s["owner_lead"] == "ghost-lead"
        assert s["owner_project"] is None

    def test_unowned_when_no_lead_and_no_env(self, relay, tmp_path):
        s = self._spawn(relay, tmp_path, "e1", lead=None, env={})
        assert s["owner_lead"] is None
        assert s["owner_project"] is None


class TestSpawnTabIdentity:
    """Spawned executors carry role-prefixed labels ([E] ...), record their backend, and
    inherit the owning lead's tab color so a lead's tabs group visually."""
    def _spawn(self, relay, tmp_path, lead=None):
        pkt = tmp_path / "p.md"
        pkt.write_text("do the thing")
        cap = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: cap.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=None), \
             mock.patch.object(relay, "read_iterm_id", return_value=None), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True):
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path), topic="t", packet=str(pkt),
                                            model=None, name="e1", scope=None, skip_perms=None,
                                            pane=None, lead=lead))
        return cap, relay.read_session("e1")

    def test_label_role_prefixed_and_backend_recorded(self, relay, tmp_path):
        cap, s = self._spawn(relay, tmp_path)
        assert cap["label"] == "[Exec] e1"
        assert s["tab_label"] == "[Exec] e1"
        assert s["backend"] in ("iterm", "terminal")

    def test_executor_inherits_lead_color(self, relay, tmp_path):
        color = relay.lead_guard.lead_color("lead-1")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", color=color)
        cap, _ = self._spawn(relay, tmp_path, lead="lead-1")
        assert cap["tab_color"] == color

    def test_unowned_spawn_uncolored(self, relay, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)  # truly unowned
        cap, _ = self._spawn(relay, tmp_path)
        assert cap["tab_color"] is None

    def test_packet_contains_diff_command_with_session_id(self, relay, tmp_path):
        pkt = tmp_path / "p.md"
        pkt.write_text("do the thing")
        with mock.patch.object(relay.iterm, "spawn"), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=None), \
             mock.patch.object(relay, "read_iterm_id", return_value=None), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True):
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path), topic="t", packet=str(pkt),
                                            model=None, name="e1", scope=None, skip_perms=None,
                                            pane=None, lead=None))
        packet_file = relay.packets_dir("e1") / "001-packet.md"
        packet_content = packet_file.read_text()
        assert "relay diff e1" in packet_content


class TestRelativeAge:
    """_relative_age renders a compact age and degrades to '-' for missing/old-format input."""
    def test_seconds(self, relay):
        past = relay.now()  # ~now → seconds granularity
        assert relay._relative_age(past).endswith("s ago")

    def test_minutes(self, relay):
        t = relay.time.mktime(relay.time.localtime()) - 12 * 60
        ts = relay.time.strftime("%Y-%m-%dT%H:%M:%S", relay.time.localtime(t))
        assert relay._relative_age(ts) == "12m ago"

    def test_hours(self, relay):
        t = relay.time.mktime(relay.time.localtime()) - 2 * 3600
        ts = relay.time.strftime("%Y-%m-%dT%H:%M:%S", relay.time.localtime(t))
        assert relay._relative_age(ts) == "2h ago"

    def test_days(self, relay):
        t = relay.time.mktime(relay.time.localtime()) - 3 * 86400
        ts = relay.time.strftime("%Y-%m-%dT%H:%M:%S", relay.time.localtime(t))
        assert relay._relative_age(ts) == "3d ago"

    def test_missing_is_dash(self, relay):
        assert relay._relative_age(None) == "-"
        assert relay._relative_age("") == "-"

    def test_old_format_is_dash_not_crash(self, relay):
        assert relay._relative_age("2026/07/07 10:00") == "-"


class TestCmdList:
    """cmd_list renders a LEADS section (always ALL leads) + a scoped EXECUTORS table with a
    PROJECT column, and --json returns {leads, executors} respecting --lead/--all scoping."""
    @pytest.fixture(autouse=True)
    def _procs_alive(self, relay, monkeypatch):
        # cmd_list now refreshes real liveness via _check_one (like `relay check`); these fixture
        # sessions have no live pid, so pretend alive to keep them `busy` and off real AppleScript.
        monkeypatch.setattr(relay, "pid_alive", lambda pid: True)

    def _exec(self, relay, sid, owner_lead=None, owner_project=None, status="busy"):
        relay.write_session(sid, {"session_id": sid, "current_packet": 1, "status": status,
            "topic": "t", "worktree": "/w", "scope": "", "model": "opus",
            "busy_since": relay.now(), "updated": relay.now(), "owner_lead": owner_lead, "owner_project": owner_project})

    def _args(self, json=False, lead=None, all=False, closed=False):
        return SimpleNamespace(json=json, lead=lead, all=all, closed=closed)

    def test_leads_section_rendered(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", model="opus", project="alpha")
        self._exec(relay, "e1", owner_lead="lead-1", owner_project="alpha")
        relay.cmd_list(self._args())
        out = capsys.readouterr().out
        assert "LEADS" in out and "alpha" in out and "lead-1" in out
        assert "ago" in out  # relative LAST ACTIVE age
        assert "EXECUTORS" in out and "PROJECT" in out

    def test_executor_project_column_shows_owner_project(self, relay, capsys):
        self._exec(relay, "e1", owner_lead="lead-1", owner_project="alpha")
        relay.cmd_list(self._args())
        assert "alpha" in capsys.readouterr().out

    def test_unowned_executor_project_is_dash(self, relay, capsys):
        self._exec(relay, "e1", owner_lead=None, owner_project=None)
        relay.cmd_list(self._args())
        out = capsys.readouterr().out
        assert "e1" in out and " - " in out  # dashed PROJECT cell

    def test_lead_scope_shows_owned_and_unowned_hides_other(self, relay, capsys):
        self._exec(relay, "mine", owner_lead="lead-1", owner_project="alpha")
        self._exec(relay, "theirs", owner_lead="lead-2", owner_project="beta")
        self._exec(relay, "free", owner_lead=None, owner_project=None)
        relay.cmd_list(self._args(lead="lead-1"))
        out = capsys.readouterr().out
        assert "mine" in out and "free" in out   # owned + unowned shown
        assert "theirs" not in out               # other lead's executor hidden

    def test_all_flag_shows_every_executor(self, relay, capsys):
        self._exec(relay, "mine", owner_lead="lead-1")
        self._exec(relay, "theirs", owner_lead="lead-2")
        relay.cmd_list(self._args(all=True, lead="lead-1"))
        out = capsys.readouterr().out
        assert "mine" in out and "theirs" in out

    def test_no_flag_shows_every_executor(self, relay, capsys):
        self._exec(relay, "mine", owner_lead="lead-1")
        self._exec(relay, "theirs", owner_lead="lead-2")
        relay.cmd_list(self._args())  # no --lead, no --all → back-compat: all executors
        out = capsys.readouterr().out
        assert "mine" in out and "theirs" in out

    def test_json_shape_has_leads_and_executors(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", model="opus", project="alpha")
        self._exec(relay, "e1", owner_lead="lead-1", owner_project="alpha")
        relay.cmd_list(self._args(json=True))
        data = json.loads(capsys.readouterr().out)
        assert set(data.keys()) == {"leads", "executors"}
        assert data["leads"][0]["session_id"] == "lead-1"
        assert data["executors"][0]["owner_lead"] == "lead-1"
        assert data["executors"][0]["owner_project"] == "alpha"

    def test_json_respects_lead_scoping(self, relay, capsys):
        self._exec(relay, "mine", owner_lead="lead-1")
        self._exec(relay, "theirs", owner_lead="lead-2")
        self._exec(relay, "free", owner_lead=None)
        relay.cmd_list(self._args(json=True, lead="lead-1"))
        data = json.loads(capsys.readouterr().out)
        ids = {e["session_id"] for e in data["executors"]}
        assert ids == {"mine", "free"}  # owned + unowned, other-lead's excluded

    def test_leads_always_shown_despite_lead_scope(self, relay, capsys):
        # LEADS section shows ALL leads even when EXECUTORS are scoped to one lead.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="alpha")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-2", project="beta")
        relay.cmd_list(self._args(json=True, lead="lead-1"))
        data = json.loads(capsys.readouterr().out)
        assert {m["session_id"] for m in data["leads"]} == {"lead-1", "lead-2"}

    def test_default_hides_terminal_rows_and_shows_footer(self, relay, capsys):
        # Default: closed/superseded/dead sessions hidden, footer shows count
        self._exec(relay, "live-1", status="busy")
        self._exec(relay, "closed-1", status="closed")
        relay.cmd_list(self._args())
        out = capsys.readouterr().out
        assert "live-1" in out
        assert "closed-1" not in out
        assert "(+1 closed/superseded/dead hidden" in out
        assert "--closed" in out
        assert "prune --dry-run" in out

    def test_closed_flag_shows_terminal_rows(self, relay, capsys):
        # --closed: include terminal rows
        self._exec(relay, "live-1", status="busy")
        self._exec(relay, "closed-1", status="closed")
        relay.cmd_list(self._args(closed=True))
        out = capsys.readouterr().out
        assert "live-1" in out
        assert "closed-1" in out

    def test_closed_flag_caps_terminal_rows_at_15_most_recent(self, relay, capsys):
        # --closed with 17 terminal sessions: show only 15 most recent by updated time
        self._exec(relay, "live-1", status="busy")
        # Create 17 closed sessions with different updated times
        for i in range(17):
            # Use a fixed base time and offset each by i seconds to control ordering
            base = "2024-01-01T12:00:00"
            import time as time_module
            t = time_module.strptime(base, "%Y-%m-%dT%H:%M:%S")
            t_offset = time_module.mktime(t) + i  # i seconds after base
            t_str = time_module.strftime("%Y-%m-%dT%H:%M:%S", time_module.localtime(t_offset))
            relay.write_session(f"closed-{i:02d}", {"session_id": f"closed-{i:02d}", "current_packet": 1,
                "status": "closed", "topic": "t", "worktree": "/w", "scope": "", "model": "opus",
                "updated": t_str, "owner_lead": None})
        relay.cmd_list(self._args(closed=True))
        out = capsys.readouterr().out
        # Should show live and the 15 most recent closed (closed-16 through closed-02)
        assert "live-1" in out
        assert "closed-16" in out  # most recent (highest i)
        assert "closed-02" in out  # 15th most recent
        assert "closed-01" not in out  # oldest, should be hidden
        assert "closed-00" not in out  # oldest, should be hidden
        assert "(…and 2 older closed sessions" in out

    def test_json_includes_all_terminal_rows_regardless_of_flags(self, relay, capsys):
        # --json: fully unfiltered, includes all rows regardless of --closed or default
        self._exec(relay, "live-1", status="busy")
        self._exec(relay, "closed-1", status="closed")
        relay.cmd_list(self._args(json=True))
        data = json.loads(capsys.readouterr().out)
        ids = {e["session_id"] for e in data["executors"]}
        assert ids == {"live-1", "closed-1"}

    def test_footer_absent_when_no_terminal_rows(self, relay, capsys):
        # No footer if there are no terminal sessions to hide
        self._exec(relay, "live-1", status="busy")
        self._exec(relay, "live-2", status="reported")
        relay.cmd_list(self._args())
        out = capsys.readouterr().out
        assert "live-1" in out
        assert "live-2" in out
        assert "closed/superseded/dead hidden" not in out
        assert "…and" not in out


class TestVersionTuple:
    """_version_tuple: real numeric ordering for version strings, not lexical — "0.3.14" sorts
    BELOW "0.3.9" as strings, which is exactly the trap the stale-hooks footnote must not fall into."""

    def test_semver_tuple_ordering_beats_lexical_trap(self, relay):
        assert relay._version_tuple("0.3.14") > relay._version_tuple("0.3.9")
        assert relay._version_tuple("0.3.14") == (0, 3, 14)

    def test_garbage_returns_none(self, relay):
        assert relay._version_tuple("not-a-version") is None
        assert relay._version_tuple(None) is None
        assert relay._version_tuple("") is None


class TestStaleHooksFootnote:
    """cmd_list's stale-hooks footnote (wake bug #3): a lead active within 6h whose stamped
    plugin_version parses LOWER than the installed plugin_version() means /reload-plugins did not
    re-point its hooks — current hooks re-stamp on every lead turn, so a recent-but-stale stamp is
    proof, not just suspicion."""
    @pytest.fixture(autouse=True)
    def _procs_alive(self, relay, monkeypatch):
        monkeypatch.setattr(relay, "pid_alive", lambda pid: True)

    def _mk_lead(self, relay, sid, stamped_version, last_active, project="webapp"):
        relay.lead_guard.write_marker(relay.STATE_ROOT, sid, project=project, plugin_version=stamped_version)
        mp = relay.lead_guard.marker_path(relay.STATE_ROOT, sid)
        m = json.loads(mp.read_text())
        m["last_active"] = last_active
        mp.write_text(json.dumps(m))

    def _args(self, json=False, lead=None, all=False, closed=False):
        return SimpleNamespace(json=json, lead=lead, all=all, closed=closed)

    def test_fires_for_recent_lead_stamped_older(self, relay, capsys, monkeypatch):
        monkeypatch.setattr(relay, "plugin_version", lambda: "0.3.14")
        self._mk_lead(relay, "lead-1", "0.3.9", relay.now())  # active now, stamp stale
        relay.cmd_list(self._args())
        out = capsys.readouterr().out
        assert "stale hooks" in out

    def test_silent_when_stamp_matches_installed(self, relay, capsys, monkeypatch):
        monkeypatch.setattr(relay, "plugin_version", lambda: "0.3.14")
        self._mk_lead(relay, "lead-1", "0.3.14", relay.now())
        relay.cmd_list(self._args())
        assert "stale hooks" not in capsys.readouterr().out

    def test_silent_when_lead_inactive(self, relay, capsys, monkeypatch):
        monkeypatch.setattr(relay, "plugin_version", lambda: "0.3.14")
        self._mk_lead(relay, "lead-1", "0.3.9", "2000-01-01T00:00:00")  # ancient last_active
        relay.cmd_list(self._args())
        assert "stale hooks" not in capsys.readouterr().out

    def test_silent_when_stamp_unparseable(self, relay, capsys, monkeypatch):
        monkeypatch.setattr(relay, "plugin_version", lambda: "0.3.14")
        self._mk_lead(relay, "lead-1", "garbage", relay.now())
        relay.cmd_list(self._args())
        assert "stale hooks" not in capsys.readouterr().out


class TestListAlignment:
    """Table cell alignment: long values are truncated with ellipsis and never shift columns after them.
    capsys is not a tty, so ANSI color codes are off — no escape-stripping needed."""
    @pytest.fixture(autouse=True)
    def _procs_alive(self, relay, monkeypatch):
        # Like TestCmdList, pretend processes are alive to keep sessions busy (off real AppleScript).
        monkeypatch.setattr(relay, "pid_alive", lambda pid: True)

    def _exec(self, relay, sid, topic="short", owner_lead=None, owner_project=None, status="busy"):
        relay.write_session(sid, {"session_id": sid, "current_packet": 1, "status": status,
            "topic": topic, "worktree": "/w", "scope": "", "model": "opus",
            "busy_since": relay.now(), "owner_lead": owner_lead, "owner_project": owner_project})

    def _args(self, json=False, lead=None, all=False):
        return SimpleNamespace(json=json, lead=lead, all=all)

    def test_long_topic_truncated_and_aligned(self, relay, capsys):
        # Two executors: one with a 60-char topic, one with a short one.
        # The EXECUTORS table should have columns aligned even though one topic is very long.
        long_topic = "fix: split-view bugs (draggable sep, panel default, extra) extra"  # 60 chars
        self._exec(relay, "e-long", topic=long_topic)
        self._exec(relay, "e-short", topic="short")
        relay.cmd_list(self._args())
        out = capsys.readouterr().out
        # The long topic row should contain "…" (truncation marker).
        assert "…" in out
        # Extract the EXECUTORS section and check alignment:
        # The "REPORTED" header should align with where "yes"/"no" appears in data rows.
        lines = out.split("\n")
        exec_start = None
        for i, line in enumerate(lines):
            if "EXECUTORS" in line:
                exec_start = i
                break
        assert exec_start is not None
        # Header is at exec_start + 1
        header_line = lines[exec_start + 1]
        # Find the column start of "REPORTED" in the header
        reported_col_start = header_line.find("REPORTED")
        assert reported_col_start >= 0
        # Data rows are at exec_start + 2 onwards; check that "yes"/"no" aligns with REPORTED header
        for data_line in lines[exec_start + 2:]:
            if not data_line.strip():
                continue
            # The reported value should start at the same column as the header
            reported_value_start = None
            for match_str in ["yes", "no"]:
                # Find the rightmost occurrence of yes/no in the line (it's the REPORTED column)
                idx = data_line.rfind(match_str)
                if idx >= 0:
                    reported_value_start = idx
                    break
            if reported_value_start is not None:
                # Allow a small tolerance for alignment (within 1-2 chars, accounting for padding)
                assert abs(reported_value_start - reported_col_start) <= 2, \
                    f"REPORTED misaligned: header at {reported_col_start}, value at {reported_value_start} in '{data_line}'"

    def test_lead_uuid_never_truncated(self, relay, capsys):
        # A lead session with a 36-char UUID should never be truncated in the LEADS table.
        # UUIDs are what users copy for `relay resume`, so truncation would break the workflow.
        uuid_36 = "12345678-1234-1234-1234-123456789012"  # exactly 36 chars
        relay.lead_guard.write_marker(relay.STATE_ROOT, uuid_36, model="opus", project="alpha")
        relay.cmd_list(self._args())
        out = capsys.readouterr().out
        # The full UUID should appear intact (no "…" for this session in LEADS).
        assert uuid_36 in out
        assert out.count("…") == 0  # LEADS section should have no truncation (no long values)

    def test_busy_elapsed_not_truncated(self, relay, capsys):
        # A session with status "busy" and 25 minutes elapsed should render as "busy 25m" (8 chars),
        # not truncated to "busy …" (8 chars). The width calc must use the rendered status string,
        # not the raw "busy" (4 chars).
        import time
        now = time.time()
        elapsed_25m_ago = now - (25 * 60)
        self._exec(relay, "e-busy", status="busy", topic="test")
        # Overwrite the session to set busy_since_epoch to 25 minutes ago
        relay.write_session("e-busy", {
            "session_id": "e-busy",
            "current_packet": 1,
            "status": "busy",
            "topic": "test",
            "worktree": "/w",
            "scope": "",
            "model": "opus",
            "busy_since": relay.now(),
            "busy_since_epoch": elapsed_25m_ago,
        })
        relay.cmd_list(self._args())
        out = capsys.readouterr().out
        # "busy 25m" should appear intact; "busy …" should NOT appear
        assert "busy 25m" in out
        assert "busy …" not in out


class TestFocus:
    """cmd_focus: executor-only, via a bounded title match on the relay label (the ccsessions
    osascript select). iterm.focus mocked (no real AppleScript)."""
    def _exec(self, relay, sid="e1", tab_label="relay-e1"):
        relay.session_dir(sid).mkdir(parents=True)
        relay.write_session(sid, {"session_id": sid, "current_packet": 1, "status": "busy",
            "topic": "t", "worktree": "/w", "tab_label": tab_label, "busy_since": relay.now()})

    def test_executor_focus_matches_by_label(self, relay):
        self._exec(relay, tab_label="relay-e1")
        with mock.patch.object(relay.iterm, "focus", return_value=True) as title:
            relay.cmd_focus(SimpleNamespace(session_id="e1"))
        title.assert_called_once_with("relay-e1", None, None)  # label + backend handle + pid

    def test_executor_no_match_errors_with_revive_hint(self, relay, capsys):
        self._exec(relay)
        with mock.patch.object(relay.iterm, "focus", return_value=False):
            with pytest.raises(SystemExit) as ei:
                relay.cmd_focus(SimpleNamespace(session_id="e1"))
        assert "resume" in str(ei.value) and "restart" in str(ei.value)  # points to revival

    def test_lead_is_focusable_by_its_tab_label(self, relay):
        # Leads now get a stable relay-controlled tab label at /relay:mode → focusable like executors.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="alpha",
                                      tab_label="[Lead] alpha")
        with mock.patch.object(relay.iterm, "focus", return_value=True) as focus:
            relay.cmd_focus(SimpleNamespace(session_id="lead-1"))
        focus.assert_called_once_with("[Lead] alpha")

    def test_lead_with_no_label_errors(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1")  # armed before rename → no label
        with mock.patch.object(relay.iterm, "focus", return_value=False):
            with pytest.raises(SystemExit):
                relay.cmd_focus(SimpleNamespace(session_id="lead-1"))

    def test_unknown_session_errors(self, relay):
        with pytest.raises(SystemExit):
            relay.cmd_focus(SimpleNamespace(session_id="nope"))


class TestNudgeLead:
    """cmd_nudge_lead: `relay send` pointed at a LEAD's own tab (wake-watch design §9). ALWAYS
    sends — §9.5b spiked that typing into a busy lead is harmless, so there is no busy/stale guard
    left; a marker still carrying a legacy `state: busy` field must be ignored.

    Sends go through `backend.by_name(marker["backend"])`, NOT `relay.iterm` (the caller's ambient
    guess) — mocking that ambient-selected module would be environment-dependent (it resolves
    differently depending on the machine/shell $TERM_PROGRAM the suite happens to run under; see
    scripts/backend.py), so every test here pins the concrete backend module via
    `relay.backend.by_name(...)` instead, exactly like cmd_nudge_lead itself does post-D2/D3."""

    def _lead(self, relay, sid="lead-1", tab_label="[Lead] alpha", state=None, state_since=None,
              backend="iterm"):
        relay.lead_guard.write_marker(relay.STATE_ROOT, sid, project="alpha", tab_label=tab_label,
                                      backend=backend)
        if state is not None:
            m = relay.lead_guard.read_marker(relay.STATE_ROOT, sid)
            m["state"] = state
            if state_since is not None:
                m["state_since"] = state_since
            relay.lead_guard.marker_path(relay.STATE_ROOT, sid).write_text(json.dumps(m))

    def test_idle_lead_gets_nudged(self, relay):
        self._lead(relay, state="idle")
        with mock.patch.object(relay.backend.by_name("iterm"), "send", return_value=True) as send:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="wake up"))
        send.assert_called_once_with("[Lead] alpha", "wake up", None)

    def test_no_state_defaults_and_nudges(self, relay):
        self._lead(relay)  # no state stamped at all
        with mock.patch.object(relay.backend.by_name("iterm"), "send", return_value=True) as send:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        send.assert_called_once()

    def test_busy_marked_lead_still_gets_nudged(self, relay):
        # This test would FAIL if someone reintroduced a busy/stale guard — a legacy `state: busy`
        # marker must not block the send.
        self._lead(relay, state="busy", state_since=relay.now())
        with mock.patch.object(relay.backend.by_name("iterm"), "send", return_value=True) as send:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        send.assert_called_once()

    def test_stale_busy_marked_lead_still_gets_nudged(self, relay):
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3600))
        self._lead(relay, state="busy", state_since=old)
        with mock.patch.object(relay.backend.by_name("iterm"), "send", return_value=True) as send:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        send.assert_called_once()

    def test_no_live_tab_reports_no_live_tab(self, relay):
        # is_alive is mocked FALSE deliberately: the refusal path now probes liveness to tell a dead
        # tab apart from a live-but-uninjectable one, so leaving it unmocked would make a real
        # osascript call and flip this test to `cannot-inject` on any machine that happens to have a
        # tab named "[Lead] alpha".
        bk = relay.backend.by_name("iterm")
        with mock.patch.object(bk, "send", return_value=False), \
             mock.patch.object(bk, "is_alive", return_value=False):
            self._lead(relay)
            with pytest.raises(SystemExit) as ei:
                relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        assert "no-live-tab" in str(ei.value)

    def test_alive_but_uninjectable_tab_reports_cannot_inject_not_no_live_tab(self, relay):
        # Terminal.app's send() is unconditionally False by design (it cannot type into a running
        # process), so a HEALTHY Terminal-hosted lead used to be reported as "no-live-tab" — a dead
        # tab and a live one failing for a totally different reason looked identical. Verified live
        # against a real Terminal window (is_alive True / send False) before this test was written.
        bk = relay.backend.by_name("terminal")
        with mock.patch.object(bk, "send", return_value=False), \
             mock.patch.object(bk, "is_alive", return_value=True):
            self._lead(relay, backend="terminal")
            with pytest.raises(SystemExit) as ei:
                relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        msg = str(ei.value)
        assert "cannot-inject" in msg
        assert "no-live-tab" not in msg

    def test_unknown_lead_errors(self, relay):
        with pytest.raises(SystemExit):
            relay.cmd_nudge_lead(SimpleNamespace(lead="nope", message="hi"))

    def test_sends_via_markers_recorded_backend_not_ambient_guess(self, relay, monkeypatch):
        # Defect A, reproduced live: an executor running under Terminal.app selects the `terminal`
        # backend for ITSELF (ambient), but the lead being nudged is actually iTerm-hosted. The send
        # must go through the marker's OWN recorded backend, never the caller's ambient guess — so
        # even swapping the ambient `relay.iterm` for a decoy that would fail must not matter.
        self._lead(relay, backend="iterm")
        decoy_calls = []
        monkeypatch.setattr(relay, "iterm",
                            SimpleNamespace(send=lambda *a, **k: decoy_calls.append(a) or False,
                                            NAME="decoy"))
        with mock.patch.object(relay.backend.by_name("iterm"), "send", return_value=True) as send:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        assert not decoy_calls, "ambient (wrong) backend was used instead of the marker's recorded one"
        send.assert_called_once_with("[Lead] alpha", "hi", None)

    def test_backend_missing_probes_and_sends_via_the_one_with_a_live_tab(self, relay):
        # D3.2: a marker armed before D2 has backend=None — every currently-armed lead on a real
        # machine, per the packet. Probe rather than guess: exactly one backend reports a live tab.
        self._lead(relay, backend=None)
        with mock.patch.object(relay.backend.by_name("terminal"), "is_alive", return_value=False), \
             mock.patch.object(relay.backend.by_name("iterm"), "is_alive", return_value=True), \
             mock.patch.object(relay.backend.by_name("iterm"), "send", return_value=True) as send:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        send.assert_called_once_with("[Lead] alpha", "hi", None)

    def test_backend_missing_and_ambiguous_probe_falls_back_to_ambient(self, relay, monkeypatch):
        # Both backends claim a live tab (shouldn't normally happen, but the probe must degrade to
        # the old ambient-guess behavior rather than pick arbitrarily).
        self._lead(relay, backend=None)
        with mock.patch.object(relay.backend.by_name("terminal"), "is_alive", return_value=True), \
             mock.patch.object(relay.backend.by_name("iterm"), "is_alive", return_value=True), \
             mock.patch.object(relay.iterm, "send", return_value=True) as send:
            relay.cmd_nudge_lead(SimpleNamespace(lead="lead-1", message="hi"))
        send.assert_called_once()


class TestWhoami:
    """`relay whoami [<token>]` (D1) — resolve a lead's session id, an executor's relay name, or an
    executor's `claude_session` uuid to a single answer: who is this, and (for an executor) who is
    its lead. Read-only over EXISTING state only — no new reverse index (Boundaries)."""

    def _exec(self, relay, name="term-exec", owner_lead=None, packet=1, report=True,
             claude_session="9b7ae35f-2cf1-4e5a-9487-11acb785ab31", status="reported"):
        relay.write_session(name, {
            "session_id": name, "owner_lead": owner_lead, "current_packet": packet,
            "status": status, "claude_session": claude_session, "topic": "t", "worktree": "/w",
        })
        d = relay.packets_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        if report:
            (d / f"{packet:03d}-report.md").write_text("done")
        return name

    def test_resolves_executor_by_relay_name(self, relay, capsys):
        self._exec(relay, name="term-exec")
        relay.cmd_whoami(SimpleNamespace(token="term-exec", json=False))
        out = capsys.readouterr().out
        assert "name       : term-exec" in out
        assert "role       : executor" in out

    def test_resolves_executor_by_claude_session_uuid(self, relay, capsys):
        self._exec(relay, name="term-exec", claude_session="uuid-123")
        relay.cmd_whoami(SimpleNamespace(token="uuid-123", json=False))
        out = capsys.readouterr().out
        assert "name       : term-exec" in out

    def test_resolves_lead_by_session_id(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="claude-relay",
                                      tab_label="[Lead] claude-relay", backend="iterm")
        relay.cmd_whoami(SimpleNamespace(token="lead-1", json=False))
        out = capsys.readouterr().out
        assert "name       : claude-relay" in out
        assert "role       : lead" in out

    def test_default_token_falls_back_to_env(self, relay, capsys, monkeypatch):
        self._exec(relay, name="term-exec", claude_session="env-uuid")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-uuid")
        relay.cmd_whoami(SimpleNamespace(token=None, json=False))
        out = capsys.readouterr().out
        assert "name       : term-exec" in out

    def test_unresolvable_token_exits_nonzero_with_message(self, relay):
        with pytest.raises(SystemExit) as ei:
            relay.cmd_whoami(SimpleNamespace(token="totally-unknown", json=False))
        assert "could not resolve" in str(ei.value)

    def test_no_token_and_no_env_exits(self, relay, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        with pytest.raises(SystemExit):
            relay.cmd_whoami(SimpleNamespace(token=None, json=False))

    def test_json_executor_shape(self, relay, capsys):
        self._exec(relay, name="term-exec", owner_lead="lead-1", claude_session="uuid-xyz")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="claude-relay",
                                      tab_label="[Lead] claude-relay", iterm_session="w0t1p0:X",
                                      backend="iterm")
        relay.cmd_whoami(SimpleNamespace(token="term-exec", json=True))
        data = json.loads(capsys.readouterr().out)
        assert data["name"] == "term-exec"
        assert data["session"] == "uuid-xyz"
        assert data["role"] == "executor"
        assert data["owner_lead"] == "lead-1"
        assert data["current_packet"] == 1
        assert data["report_path"].endswith("001-report.md")
        assert os.path.isabs(data["report_path"])
        assert data["report_exists"] is True
        assert data["lead_backend"] == "iterm"
        assert data["lead_tab_label"] == "[Lead] claude-relay"
        assert data["lead_iterm_session"] == "w0t1p0:X"

    def test_json_executor_report_not_yet_written(self, relay, capsys):
        self._exec(relay, name="term-exec", report=False)
        relay.cmd_whoami(SimpleNamespace(token="term-exec", json=True))
        data = json.loads(capsys.readouterr().out)
        assert data["report_exists"] is False

    def test_json_executor_unowned_lead_fields_are_null(self, relay, capsys):
        self._exec(relay, name="term-exec", owner_lead=None)
        relay.cmd_whoami(SimpleNamespace(token="term-exec", json=True))
        data = json.loads(capsys.readouterr().out)
        assert data["owner_lead"] is None
        assert data["lead_backend"] is None
        assert data["lead_tab_label"] is None

    def test_json_lead_shape_lists_execs(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="claude-relay",
                                      tab_label="[Lead] claude-relay", backend="iterm")
        self._exec(relay, name="term-exec", owner_lead="lead-1", status="reported")
        self._exec(relay, name="relay-wake-push", owner_lead="lead-1", status="reported",
                  claude_session="uuid-2")
        relay.cmd_whoami(SimpleNamespace(token="lead-1", json=True))
        data = json.loads(capsys.readouterr().out)
        assert data["role"] == "lead"
        assert data["session"] == "lead-1"
        assert data["backend"] == "iterm"
        assert data["tab_label"] == "[Lead] claude-relay"
        names = {e["name"] for e in data["execs"]}
        assert names == {"term-exec", "relay-wake-push"}


class TestCloseTab:
    """cmd_close now kills the executor's process and closes its iTerm tab (report's on disk). Mocks
    iterm.close / pid_alive / os.kill / time.sleep — no real iTerm or signals."""
    def _mk(self, relay, sid="e1", pid=12345):
        relay.session_dir(sid).mkdir(parents=True)
        relay.write_session(sid, {"session_id": sid, "status": "busy",
            "tab_label": "relay-e1", "pid": pid, "current_packet": 1, "topic": "t",
            "worktree": "/w", "busy_since": relay.now()})

    def _args(self, **over):
        base = dict(session_id="e1", supersede=None, self_session=None, keep_tab=False)
        base.update(over)
        return SimpleNamespace(**base)

    def test_close_kills_process_and_closes_tab(self, relay):
        self._mk(relay)
        # pid_alive: True while deciding to kill, False once polled after SIGTERM — _kill_and_wait
        # must WAIT for actual death (a fixed sleep raced Terminal's confirm sheet), then close.
        with mock.patch.object(relay.iterm, "close", return_value=True) as close, \
             mock.patch.object(relay, "pid_alive", side_effect=[True, False]), \
             mock.patch.object(relay.os, "kill") as kill, \
             mock.patch.object(relay.time, "sleep"):
            relay.cmd_close(self._args())
        assert relay.read_session("e1")["status"] == "closed"
        close.assert_called_once_with("relay-e1", None, 12345)   # label + backend handle + pid
        kill.assert_called_once_with(12345, relay.signal.SIGTERM)  # TERM first; no KILL (it died)

    def test_close_notes_tab_that_closed_itself(self, relay, capsys):
        # iTerm auto-closes the tab when the exec'd process dies → close() finds nothing. That is
        # success ("closed itself"), NOT the lingering-tab Cmd-W warning (spurious note seen live).
        self._mk(relay)
        with mock.patch.object(relay.iterm, "close", return_value=False), \
             mock.patch.object(relay.iterm, "is_alive", return_value=False), \
             mock.patch.object(relay, "pid_alive", side_effect=[True, False]), \
             mock.patch.object(relay.os, "kill"), \
             mock.patch.object(relay.time, "sleep"):
            relay.cmd_close(self._args())
        out = capsys.readouterr().out
        assert "closed itself" in out and "Cmd-W" not in out

    def test_keep_tab_leaves_tab_and_process(self, relay):
        self._mk(relay)
        with mock.patch.object(relay.iterm, "close") as close, \
             mock.patch.object(relay, "pid_alive", return_value=True), \
             mock.patch.object(relay.os, "kill") as kill:
            relay.cmd_close(self._args(keep_tab=True))
        assert relay.read_session("e1")["status"] == "closed"
        close.assert_not_called()
        kill.assert_not_called()


class TestRestartResume:
    """cmd_restart (fresh session, re-run packet) / cmd_resume (--resume, keep context). iterm.spawn,
    auto_trust, read_pid, read_iterm_id, pid_alive are mocked — no real iTerm/claude."""
    def _mk(self, relay, sid="e1", claude_session="uuid-old", packet=True):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)  # also creates session_dir
        relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
            "tab_label": "relay-e1", "model": None, "pid": 999, "iterm_session": "w0t0p0:OLD",
            "claude_session": claude_session, "status": "dead", "current_packet": 1,
            "busy_since": relay.now(), "created": relay.now(), "updated": relay.now()})
        if packet:
            (relay.packets_dir(sid) / "001-packet.md").write_text("do the work")

    def _run(self, relay, fn, sid="e1", alive=False, force=False):
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True), \
             mock.patch.object(relay, "pid_alive", return_value=alive):
            fn(SimpleNamespace(session_id=sid, force=force))
        return captured

    def test_restart_fresh_uuid_and_reruns_packet(self, relay):
        self._mk(relay)
        cap = self._run(relay, relay.cmd_restart)
        assert cap["resume_id"] is None
        assert cap["session_uuid"] and cap["session_uuid"] != "uuid-old"   # a fresh conversation
        assert "001-packet.md" in cap["prompt"]                            # re-runs the packet
        assert relay.read_session("e1")["claude_session"] == cap["session_uuid"]
        assert relay.read_session("e1")["status"] == "busy" and relay.read_session("e1")["pid"] == 123

    def test_resume_uses_resume_id_and_keeps_claude_session(self, relay):
        self._mk(relay, claude_session="uuid-keep")
        cap = self._run(relay, relay.cmd_resume)
        assert cap["resume_id"] == "uuid-keep"
        assert cap["session_uuid"] is None
        assert "staged work" in cap["prompt"].lower()                      # continue-nudge
        assert relay.read_session("e1")["claude_session"] == "uuid-keep"   # unchanged

    def test_resume_without_claude_session_errors(self, relay):
        self._mk(relay, claude_session=None)
        with pytest.raises(SystemExit):
            self._run(relay, relay.cmd_resume)

    def test_restart_missing_packet_errors(self, relay):
        self._mk(relay, packet=False)
        with pytest.raises(SystemExit):
            self._run(relay, relay.cmd_restart)

    def test_alive_without_force_refuses_and_does_not_launch(self, relay):
        self._mk(relay)
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "pid_alive", return_value=True):
            with pytest.raises(SystemExit):
                relay.cmd_restart(SimpleNamespace(session_id="e1", force=False))
        assert not captured  # refused before launching

    def test_relaunch_clears_stale_pid_file(self, relay):
        self._mk(relay)
        relay.pid_path("e1").write_text("999")  # stale
        self._run(relay, relay.cmd_restart)
        assert relay.read_session("e1")["pid"] == 123  # picked up the NEW pid, not the stale file

    def test_restart_fills_missing_model_with_config_default(self, relay):
        # `_mk`'s fixture session has "model": None (a legacy/never-set-explicitly record) — restart
        # is a FRESH launch (new session_uuid), so it must get relay's own default policy applied,
        # same as a first spawn, and persist it (not leave `-` in `relay list` a second time).
        self._mk(relay)
        cap = self._run(relay, relay.cmd_restart)
        assert cap["model"] == "sonnet"
        assert relay.read_session("e1")["model"] == "sonnet"

    def test_restart_respects_configured_default(self, relay):
        self._mk(relay)
        (relay.STATE_ROOT / "lead").mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "lead" / "config.json").write_text(json.dumps({"executor_default_model": "haiku"}))
        cap = self._run(relay, relay.cmd_restart)
        assert cap["model"] == "haiku"
        assert relay.read_session("e1")["model"] == "haiku"

    def test_restart_keeps_existing_explicit_model(self, relay):
        self._mk(relay)
        s = relay.read_session("e1")
        s["model"] = "opus"
        relay.write_session("e1", s)
        cap = self._run(relay, relay.cmd_restart)
        assert cap["model"] == "opus"                              # not overwritten by the default
        assert relay.read_session("e1")["model"] == "opus"

    def test_resume_does_not_rewrite_existing_session_model(self, relay):
        # A resume (`resume_id` set, no fresh `session_uuid`) reopens the SAME conversation — its
        # model is exempt from relay's spawn-time policy entirely, whatever it already was.
        self._mk(relay)
        s = relay.read_session("e1")
        s["model"] = "opus"
        relay.write_session("e1", s)
        cap = self._run(relay, relay.cmd_resume)
        assert cap["model"] == "opus"
        assert relay.read_session("e1")["model"] == "opus"

    def test_resume_does_not_fill_missing_model(self, relay):
        # `_mk`'s fixture session has "model": None — resume must leave it exactly that way, not
        # apply the default-fill logic that only fresh (restart/spawn) launches get.
        self._mk(relay)
        cap = self._run(relay, relay.cmd_resume)
        assert cap["model"] is None
        assert relay.read_session("e1")["model"] is None


class TestResumeLead:
    """cmd_resume routing a crashed LEAD (marker present, no session.json) → restores its OWN Claude
    conversation via `claude --resume <sid>`. iterm.spawn is mocked so no real iTerm/claude."""
    def _run(self, relay, sid, force=False, iterm_id=None):
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "read_iterm_id_at", return_value=iterm_id):
            relay.cmd_resume(SimpleNamespace(session_id=sid, force=force))
        return captured

    def test_lead_resume_spawns_with_sid_as_resume_id(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", model="opus",
                                      project="webapp", cwd="/w/dv")
        cap = self._run(relay, "lead-1")
        assert cap["resume_id"] == "lead-1"          # resume the lead's OWN session
        assert cap["cwd"] == "/w/dv"                 # reopen in the marker's cwd
        assert cap["model"] == "opus"
        assert "webapp" in cap["prompt"]         # nudge mentions the project
        assert "/relay:list" in cap["prompt"]        # nudge tells it to reconstruct in-flight work

    def test_lead_resume_uses_lead_dir_pidfile_no_session_json(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w")
        cap = self._run(relay, "lead-1")
        # pidfile lives under the lead's OWN dir, NOT a bogus executor session dir.
        assert cap["pidfile"] == str(relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1") / "pid")
        assert not (relay.session_dir("lead-1") / "session.json").exists()  # no executor state
        assert relay.read_session("lead-1") is None

    def test_lead_resume_refreshes_last_active(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w")
        before = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        old = "2000-01-01T00:00:00"
        mp = relay.lead_guard.marker_path(relay.STATE_ROOT, "lead-1")
        m = json.loads(mp.read_text()); m["last_active"] = old; mp.write_text(json.dumps(m))
        self._run(relay, "lead-1")
        after = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert after["last_active"] != old            # refreshed
        assert after["project"] == "p" and after["cwd"] == "/w"  # preserved

    def test_unknown_id_still_errors(self, relay):
        with pytest.raises(SystemExit) as ei:
            self._run(relay, "nobody")
        assert "no such session" in str(ei.value)

    def test_lead_resume_restores_label_color_and_marker_fields(self, relay):
        # The restored tab must reuse the lead-start label scheme AND the marker must keep
        # tab_label/color — dropping them here used to break `relay focus <lead>` after a restore.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd="/w",
                                      tab_label="[Lead] webapp", color=[1, 2, 3])
        cap = self._run(relay, "lead-1")
        assert cap["label"] == "[Lead] webapp"
        assert cap["tab_color"] == [1, 2, 3]
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["tab_label"] == "[Lead] webapp"
        assert m["color"] == [1, 2, 3]

    def test_lead_resume_nudge_instructs_rearming(self, relay):
        # The restored session's id may differ from the marker's (claude --resume can fork a fresh
        # id), so the nudge must tell the lead to re-run lead-start under its CURRENT id — without
        # this, hooks silently miss the old marker and lead mode is inactive while looking armed.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w")
        cap = self._run(relay, "lead-1")
        assert "lead-start" in cap["prompt"]
        assert "$CLAUDE_CODE_SESSION_ID" in cap["prompt"]
        assert "close --self lead-1" in cap["prompt"]     # clear the stale marker if the id changed

    def test_lead_resume_refuses_when_alive_without_force(self, relay):
        # A recorded live pid means the lead is still open → a second `claude --resume` of the SAME
        # conversation must be refused (mirrors the executor guard).
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w")
        pid_file = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1") / "pid"
        pid_file.write_text(str(os.getpid()))            # a genuinely alive pid
        with pytest.raises(SystemExit) as ei:
            self._run(relay, "lead-1")
        assert "--force" in str(ei.value)

    def test_lead_resume_captures_iterm_session(self, relay):
        # The fresh tab's backend session id must be captured into the marker — without this, a
        # restored lead permanently loses adjacent-tab placement, tier-1 tty notifications, and
        # _lead_alive's tty probe until it happens to re-arm in-tab.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w")
        cap = self._run(relay, "lead-1", iterm_id="w9t9p0:RESTORED")
        assert cap["iterm_id_file"] == str(relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1") / "iterm_id")
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["iterm_session"] == "w9t9p0:RESTORED"

    def test_lead_resume_capture_failure_leaves_none(self, relay):
        # Capture is best-effort: a timeout must not block the restore or raise.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w")
        self._run(relay, "lead-1", iterm_id=None)
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["iterm_session"] is None
        events = [json.loads(l)["event"] for l in relay.LEDGER.read_text().splitlines()]
        assert "lead_resumed" in events

    def test_lead_resume_force_overrides_alive(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w")
        (relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1") / "pid").write_text(str(os.getpid()))
        cap = self._run(relay, "lead-1", force=True)
        assert cap["resume_id"] == "lead-1"              # launched despite the live pid

    def test_executor_resume_unchanged_uses_claude_session(self, relay):
        # An executor sid still routes to the executor path (claude_session, not the sid).
        relay.packets_dir("e1").mkdir(parents=True, exist_ok=True)
        relay.write_session("e1", {"session_id": "e1", "worktree": "/w", "topic": "t", "scope": "t",
            "tab_label": "relay-e1", "model": None, "pid": 999, "iterm_session": None,
            "claude_session": "cs-exec", "status": "dead", "current_packet": 1,
            "busy_since": relay.now(), "created": relay.now(), "updated": relay.now()})
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True), \
             mock.patch.object(relay, "pid_alive", return_value=False):
            relay.cmd_resume(SimpleNamespace(session_id="e1", force=False))
        assert captured["resume_id"] == "cs-exec"     # executor uses its captured claude_session


class TestSend:
    """cmd_send: refresh liveness before the busy-guard (bug-a), and resume-fallback when the tab
    is gone (bug-b). iterm.send / iterm.spawn / iterm.is_alive and the pid/iterm-id readers are all
    mocked, so no real iTerm/claude is touched."""

    def _mk(self, relay, sid="e1", status="busy", pid=None, claude_session="cs-x", report=False):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)  # also creates session_dir
        relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
            "tab_label": "relay-e1", "model": None, "pid": pid, "iterm_session": "w0t0p0:OLD",
            "claude_session": claude_session, "status": status, "current_packet": 1,
            "busy_since": relay.now(), "created": relay.now(), "updated": relay.now()})
        # The current packet already on disk, so next_packet_number() advances to 002.
        (relay.packets_dir(sid) / "001-packet.md").write_text("first packet")
        # The stored current_packet's report — present iff `report`, which is what _check_one reads.
        if report:
            (relay.packets_dir(sid) / "001-report.md").write_text("done")

    def _packet(self, relay, tmp_path):
        p = tmp_path / "next-packet.md"
        p.write_text("# Follow-up\n\nDo the next thing.")
        return str(p)

    def test_stored_busy_but_reported_on_disk_proceeds(self, relay, tmp_path):
        # BUG-A: stored status is `busy` (never refreshed since spawn) but a report exists on disk.
        # cmd_send must refresh (→ reported) and PROCEED, not refuse.
        self._mk(relay, status="busy", pid=os.getpid(), report=True)
        sent = {}
        with mock.patch.object(relay.iterm, "send", side_effect=lambda *a: sent.update(label=a[0]) or True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        assert sent.get("label") == "relay-e1"             # the send actually happened
        s = relay.read_session("e1")
        assert s["status"] == "busy" and s["current_packet"] == 2  # new packet is now in flight

    def test_genuinely_busy_still_refused(self, relay, tmp_path):
        # A process-alive, no-report session is really mid-turn → still refused (no mid-turn inject).
        self._mk(relay, status="busy", pid=os.getpid(), report=False)
        with mock.patch.object(relay.iterm, "send", return_value=True) as send, \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            with pytest.raises(SystemExit) as ei:
                relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        assert "busy" in str(ei.value)
        send.assert_not_called()                           # never injected

    def test_send_fails_with_claude_session_resumes(self, relay, tmp_path):
        # BUG-B: reported session whose tab was since closed → iterm.send fails → resume-fallback
        # relaunches with resume_id == claude_session and delivers the packet; NOT marked dead.
        self._mk(relay, status="reported", pid=999999, claude_session="cs-x", report=True)
        cap = {}
        with mock.patch.object(relay.iterm, "send", return_value=False), \
             mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: cap.update(kw)), \
             mock.patch.object(relay.iterm, "is_alive", return_value=False), \
             mock.patch.object(relay.iterm, "close", return_value=False), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        assert cap["resume_id"] == "cs-x"                  # resumed the pinned conversation
        assert "002-packet.md" in cap["prompt"]            # and delivered the new packet
        s = relay.read_session("e1")
        assert s["status"] == "busy" and s["current_packet"] == 2
        assert s["status"] != "dead"

    def test_send_fallback_kills_still_alive_old_process_first(self, relay, tmp_path):
        # Terminal backend case: send can't inject, so the fallback resumes — but the OLD process
        # is still alive at its prompt. It must be SIGTERMed and its tab closed BEFORE the resume,
        # or two live copies of the same conversation would run at once.
        self._mk(relay, status="reported", pid=os.getpid(), claude_session="cs-x", report=True)
        cap = {}
        with mock.patch.object(relay.iterm, "send", return_value=False), \
             mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: cap.update(kw)), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True), \
             mock.patch.object(relay.iterm, "close", return_value=True) as close, \
             mock.patch.object(relay, "session_pid_alive", return_value=True), \
             mock.patch.object(relay, "pid_alive", return_value=False), \
             mock.patch.object(relay.os, "kill") as kill, \
             mock.patch.object(relay.time, "sleep"), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        assert kill.call_args_list[0].args[1] == relay.signal.SIGTERM  # old process TERMed first
        close.assert_called_once()                         # old tab closed
        assert cap["resume_id"] == "cs-x"                  # then the conversation resumed

    def test_send_fails_without_claude_session_marks_dead(self, relay, tmp_path):
        # No captured Claude session → today's behavior: mark dead, tell them to spawn.
        self._mk(relay, status="reported", pid=999999, claude_session=None, report=True)
        with mock.patch.object(relay.iterm, "send", return_value=False), \
             mock.patch.object(relay.iterm, "spawn") as spawn, \
             mock.patch.object(relay.iterm, "is_alive", return_value=False):
            with pytest.raises(SystemExit) as ei:
                relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        assert "dead" in str(ei.value)
        spawn.assert_not_called()                          # no resume attempted
        assert relay.read_session("e1")["status"] == "dead"

    def test_send_to_already_dead_with_claude_session_resumes(self, relay, tmp_path):
        # A session `relay check` ALREADY marked dead, but with a pinned conversation → send must
        # take the resume path (context + staged work back, packet delivered), not demand a spawn.
        self._mk(relay, status="dead", pid=999999, claude_session="cs-x", report=True)
        cap = {}
        with mock.patch.object(relay.iterm, "send", return_value=True) as send, \
             mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: cap.update(kw)), \
             mock.patch.object(relay.iterm, "is_alive", return_value=False), \
             mock.patch.object(relay.iterm, "close", return_value=False), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        send.assert_not_called()                           # tab known gone — no blind type attempt
        assert cap["resume_id"] == "cs-x"
        assert "002-packet.md" in cap["prompt"]
        s = relay.read_session("e1")
        assert s["status"] == "busy" and s["current_packet"] == 2

    def test_send_to_already_dead_without_claude_session_refuses(self, relay, tmp_path):
        self._mk(relay, status="dead", pid=999999, claude_session=None, report=True)
        with mock.patch.object(relay.iterm, "send") as send, \
             mock.patch.object(relay.iterm, "spawn") as spawn:
            with pytest.raises(SystemExit) as ei:
                relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        assert "relay spawn" in str(ei.value)
        send.assert_not_called()
        spawn.assert_not_called()

    def test_send_to_closed_session_resumes(self, relay, tmp_path):
        # A CLOSED session (deliberately retired by the lead) with a pinned conversation is revivable:
        # send treats its tab as gone, skips the doomed live type, and RESUMES it — reopening the same
        # conversation with the new packet, full context + staged work back. NOT a fresh spawn.
        self._mk(relay, status="closed", pid=999999, claude_session="cs-x", report=True)
        cap = {}
        with mock.patch.object(relay.iterm, "send", return_value=True) as send, \
             mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: cap.update(kw)), \
             mock.patch.object(relay.iterm, "is_alive", return_value=False), \
             mock.patch.object(relay.iterm, "close", return_value=False), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        send.assert_not_called()                           # closed tab known gone — no blind type
        assert cap["resume_id"] == "cs-x"                  # resumed the pinned conversation
        assert "002-packet.md" in cap["prompt"]            # and delivered the new packet
        s = relay.read_session("e1")
        assert s["status"] == "busy" and s["current_packet"] == 2

    def test_send_to_closed_session_without_claude_session_refuses(self, relay, tmp_path):
        # Closed + no pinned conversation → nothing to resume; must point to `relay spawn`.
        self._mk(relay, status="closed", pid=999999, claude_session=None, report=True)
        with mock.patch.object(relay.iterm, "send") as send, \
             mock.patch.object(relay.iterm, "spawn") as spawn:
            with pytest.raises(SystemExit) as ei:
                relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        assert "closed" in str(ei.value) and "relay spawn" in str(ei.value)
        send.assert_not_called()
        spawn.assert_not_called()


class TestSessionPidAlive:
    """The pid-reuse guard: a recorded process start time that no longer matches means the OS
    recycled the pid to an unrelated process → NOT our executor (and never SIGTERM it)."""
    def test_matching_start_time_is_alive(self, relay):
        pid = os.getpid()
        assert relay.session_pid_alive({"pid": pid, "pid_started": relay.pid_start_time(pid)}) is True

    def test_recycled_pid_start_mismatch_is_dead(self, relay):
        assert relay.session_pid_alive(
            {"pid": os.getpid(), "pid_started": "Sat Jan  1 00:00:00 2000"}) is False

    def test_no_recorded_start_falls_back_to_kill_check(self, relay):
        assert relay.session_pid_alive({"pid": os.getpid()}) is True
        assert relay.session_pid_alive({"pid": None}) is False

    def test_pid_start_time_none_for_missing_pid(self, relay):
        assert relay.pid_start_time(999999) is None


class TestPrune:
    """`relay prune` deletes terminal-status (closed/superseded/dead) session dirs older than
    --days; live sessions, recent terminal ones, and the ledger stay."""
    OLD = "2000-01-01T00:00:00"

    def _mk(self, relay, sid, status, updated):
        relay.packets_dir(sid).mkdir(parents=True)
        relay.write_session(sid, {"session_id": sid, "status": status, "current_packet": 1,
            "topic": "t", "worktree": "/w", "scope": "", "tab_label": f"relay-{sid}",
            "busy_since": relay.now(), "updated": updated})

    def test_prunes_old_terminal_keeps_live_and_recent(self, relay, capsys):
        self._mk(relay, "old-closed", "closed", self.OLD)
        self._mk(relay, "old-dead", "dead", self.OLD)
        self._mk(relay, "old-busy", "busy", self.OLD)          # live → never pruned
        self._mk(relay, "new-closed", "closed", relay.now())   # terminal but recent → kept
        relay.cmd_prune(SimpleNamespace(days=7, dry_run=False))
        assert relay.read_session("old-closed") is None
        assert relay.read_session("old-dead") is None
        assert relay.read_session("old-busy") is not None
        assert relay.read_session("new-closed") is not None
        events = [json.loads(l)["event"] for l in relay.LEDGER.read_text().splitlines()]
        assert events.count("pruned") == 2

    def test_dry_run_deletes_nothing(self, relay, capsys):
        self._mk(relay, "old-closed", "closed", self.OLD)
        relay.cmd_prune(SimpleNamespace(days=7, dry_run=True))
        assert "would prune" in capsys.readouterr().out
        assert relay.read_session("old-closed") is not None


class TestLeadPrune:
    """`relay prune` also clears dead lead markers ("ghosts": tabs closed/crashed without
    /relay:stop, so the marker is never cleaned up) older than --days. Triple guard: never the
    calling lead, must fail the liveness probe, AND must be stale — a wrongly-dead verdict deletes
    live lead state, worse than a stale row."""
    OLD = "2000-01-01T00:00:00"

    def _mk_lead(self, relay, sid, last_active, project="proj"):
        relay.lead_guard.write_marker(relay.STATE_ROOT, sid, project=project)
        mp = relay.lead_guard.marker_path(relay.STATE_ROOT, sid)
        m = json.loads(mp.read_text())
        m["last_active"] = last_active
        mp.write_text(json.dumps(m))

    def _ledger_events(self, relay):
        if not relay.LEDGER.exists():
            return []
        return [json.loads(l)["event"] for l in relay.LEDGER.read_text().splitlines()]

    def test_prunes_stale_dead_lead(self, relay):
        self._mk_lead(relay, "ghost-1", self.OLD)
        with mock.patch.object(relay, "_lead_alive", return_value=False):
            relay.cmd_prune(SimpleNamespace(days=7, dry_run=False))
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "ghost-1") == {}
        assert "lead_pruned" in self._ledger_events(relay)

    def test_keeps_alive_lead(self, relay):
        self._mk_lead(relay, "alive-1", self.OLD)
        with mock.patch.object(relay, "_lead_alive", return_value=True):
            relay.cmd_prune(SimpleNamespace(days=7, dry_run=False))
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "alive-1") != {}

    def test_keeps_recent_dead_lead(self, relay):
        self._mk_lead(relay, "recent-1", relay.now())
        with mock.patch.object(relay, "_lead_alive", return_value=False):
            relay.cmd_prune(SimpleNamespace(days=7, dry_run=False))
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "recent-1") != {}

    def test_never_prunes_calling_lead(self, relay, monkeypatch):
        self._mk_lead(relay, "self-lead", self.OLD)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "self-lead")
        with mock.patch.object(relay, "_lead_alive", return_value=False):
            relay.cmd_prune(SimpleNamespace(days=7, dry_run=False))
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "self-lead") != {}

    def test_dry_run_deletes_nothing(self, relay, capsys):
        self._mk_lead(relay, "ghost-1", self.OLD, project="dry-proj")
        with mock.patch.object(relay, "_lead_alive", return_value=False):
            relay.cmd_prune(SimpleNamespace(days=7, dry_run=True))
        out = capsys.readouterr().out
        assert "would prune" in out
        assert "dry-proj" in out
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "ghost-1") != {}

    def test_absent_last_active_treated_ancient(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "bare-lead", project="p")
        mp = relay.lead_guard.marker_path(relay.STATE_ROOT, "bare-lead")
        m = json.loads(mp.read_text())
        del m["last_active"]
        del m["started"]
        mp.write_text(json.dumps(m))
        with mock.patch.object(relay, "_lead_alive", return_value=False):
            relay.cmd_prune(SimpleNamespace(days=7, dry_run=False))
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "bare-lead") == {}


class TestDiff:
    """`relay diff <sid> [--open] [--all]`: renders the session's staged diff to
    <packets_dir>/NNN-diff.html, scoped by default to files mentioned in the current packet
    report. Real git repos in tmp_path (same pattern as test_lead_guard.py's TestCommitSurfacing)
    — no mocking of git itself, only of `open` (--open) since that's a real OS call."""

    def _git(self, repo, *args):
        import subprocess
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    def _repo_with_staged_changes(self, tmp_path, files):
        """A real git repo at tmp_path/repo with each of `files` (name -> content) committed once,
        then modified and `git add`ed — i.e. genuinely staged changes for `git diff --staged`."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        for name, content in files.items():
            (repo / name).write_text(content)
        self._git(repo, "add", *files.keys())
        self._git(repo, "commit", "-m", "init")
        for name, content in files.items():
            (repo / name).write_text(content + "\nmodified\n")
        self._git(repo, "add", *files.keys())
        return repo

    def _mk_session(self, relay, sid, repo, packet=1, report_text=None):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        relay.write_session(sid, {"session_id": sid, "worktree": str(repo), "topic": "t",
            "scope": "t", "tab_label": f"relay-{sid}", "status": "reported",
            "current_packet": packet, "busy_since": relay.now()})
        if report_text is not None:
            (relay.packets_dir(sid) / f"{packet:03d}-report.md").write_text(report_text)

    def test_report_scoped_filtering(self, relay, tmp_path):
        # 3 changed files; report mentions 2 of them → page contains only those 2.
        repo = self._repo_with_staged_changes(tmp_path, {"a.py": "a", "b.py": "b", "c.py": "c"})
        self._mk_session(relay, "e1", repo,
                         report_text="Touched a.py:1 and also b.py — the rest of the module "
                                      "is unrelated to this change.")
        relay.cmd_diff(SimpleNamespace(session_id="e1", open=False, all=False))
        out = (relay.packets_dir("e1") / "001-diff.html").read_text()
        assert "a.py" in out and "b.py" in out
        assert "c.py" not in out
        assert "unfiltered" not in out

    def test_all_bypasses_filtering(self, relay, tmp_path):
        repo = self._repo_with_staged_changes(tmp_path, {"a.py": "a", "b.py": "b", "c.py": "c"})
        self._mk_session(relay, "e1", repo, report_text="Touched a.py only.")
        relay.cmd_diff(SimpleNamespace(session_id="e1", open=False, all=True))
        out = (relay.packets_dir("e1") / "001-diff.html").read_text()
        assert "a.py" in out and "b.py" in out and "c.py" in out
        assert "unfiltered" not in out  # --all is an explicit choice, not a degraded fallback

    def test_missing_report_falls_back_unfiltered_with_note(self, relay, tmp_path):
        repo = self._repo_with_staged_changes(tmp_path, {"a.py": "a", "b.py": "b"})
        self._mk_session(relay, "e1", repo, report_text=None)  # no NNN-report.md written
        relay.cmd_diff(SimpleNamespace(session_id="e1", open=False, all=False))
        out = (relay.packets_dir("e1") / "001-diff.html").read_text()
        assert "a.py" in out and "b.py" in out
        assert "unfiltered — report not found/parsable" in out

    def test_report_matching_nothing_staged_falls_back_unfiltered(self, relay, tmp_path):
        # Report exists and parses fine, but mentions files that aren't actually staged.
        repo = self._repo_with_staged_changes(tmp_path, {"a.py": "a", "b.py": "b"})
        self._mk_session(relay, "e1", repo, report_text="Touched some_other_file.py, not here.")
        relay.cmd_diff(SimpleNamespace(session_id="e1", open=False, all=False))
        out = (relay.packets_dir("e1") / "001-diff.html").read_text()
        assert "a.py" in out and "b.py" in out
        assert "unfiltered — report not found/parsable" in out

    def test_output_path_and_stdout(self, relay, tmp_path, capsys):
        repo = self._repo_with_staged_changes(tmp_path, {"a.py": "a"})
        self._mk_session(relay, "e1", repo, report_text="a.py changed.")
        relay.cmd_diff(SimpleNamespace(session_id="e1", open=False, all=False))
        expected = relay.packets_dir("e1") / "001-diff.html"
        assert expected.exists()
        assert str(expected) in capsys.readouterr().out

    def test_open_flag_calls_open(self, relay, tmp_path):
        repo = self._repo_with_staged_changes(tmp_path, {"a.py": "a"})
        self._mk_session(relay, "e1", repo, report_text="a.py changed.")
        real_run = relay.subprocess.run
        opened = []

        def spy(cmd, *a, **kw):
            if cmd[0] == "open":
                opened.append(cmd)
                return mock.DEFAULT
            return real_run(cmd, *a, **kw)

        with mock.patch.object(relay.subprocess, "run", side_effect=spy):
            relay.cmd_diff(SimpleNamespace(session_id="e1", open=True, all=False))
        assert opened == [["open", str(relay.packets_dir("e1") / "001-diff.html")]]

    def test_unknown_session_errors(self, relay):
        with pytest.raises(SystemExit):
            relay.cmd_diff(SimpleNamespace(session_id="nope", open=False, all=False))

    def test_diff_output_includes_file_url(self, relay, tmp_path, capsys):
        import urllib.parse
        repo = self._repo_with_staged_changes(tmp_path, {"a.py": "a"})
        self._mk_session(relay, "e1", repo, report_text="a.py changed.")
        relay.cmd_diff(SimpleNamespace(session_id="e1", open=False, all=False))
        output = capsys.readouterr().out
        expected_path = relay.packets_dir("e1") / "001-diff.html"
        # Both plain path and file:// URL should be in output
        assert str(expected_path) in output
        expected_url = f"file://{urllib.parse.quote(str(expected_path), safe='/')}"
        assert expected_url in output

    def test_diff_url_encodes_spaces_as_percent20(self, relay, tmp_path, capsys):
        import urllib.parse
        # Create a session with spaces in its path (via session_id with spaces)
        repo = self._repo_with_staged_changes(tmp_path, {"a.py": "a"})
        self._mk_session(relay, "e1 with spaces", repo, report_text="a.py changed.")
        relay.cmd_diff(SimpleNamespace(session_id="e1 with spaces", open=False, all=False))
        output = capsys.readouterr().out
        # Verify %20 is in the URL line (not raw spaces)
        lines = output.strip().split('\n')
        url_line = lines[1]  # second line is the file:// URL
        assert "file://" in url_line
        assert "%20" in url_line
        # Check that spaces are encoded in the URL part
        url_part = url_line.split("file://")[1]
        assert " " not in url_part  # no raw spaces after file://

    def test_diff_url_round_trip_unquote(self, relay, tmp_path, capsys):
        import urllib.parse
        repo = self._repo_with_staged_changes(tmp_path, {"a.py": "a"})
        self._mk_session(relay, "e1", repo, report_text="a.py changed.")
        relay.cmd_diff(SimpleNamespace(session_id="e1", open=False, all=False))
        output = capsys.readouterr().out
        expected_path = relay.packets_dir("e1") / "001-diff.html"
        lines = output.strip().split('\n')
        url_line = lines[1]  # second line is the file:// URL
        # Extract the URL part (remove "file://" prefix)
        url_encoded_path = url_line.replace("file://", "")
        # Unquote and verify it equals the original path
        unquoted = urllib.parse.unquote(url_encoded_path)
        assert unquoted == str(expected_path)


class TestSurfacedDedupOnReview:
    """§5b / task #17: the at-Stop wake path (lg.new_reports_for + lg.mark_surfaced) is the ONLY
    writer of a lead's surfaced_reports.json before this fix. Reviewing a report through any other
    channel — `relay check`, `relay diff`, `relay close` — left the dedup unstamped, so a report
    already reviewed in a user-prompted turn (e.g. after the push escalation nudged the lead) was
    still "new" at the lead's next at-Stop check and re-announced. These tests drive the real
    lg.new_reports_for/lg.load_surfaced dedup the Stop hook itself reads, not a reimplementation."""

    def _mk_owned_session(self, relay, sid, lead_sid, packet=1, reported=True, extra=None):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        data = {
            "session_id": sid, "worktree": "/tmp/wt", "topic": "t", "scope": "t",
            "tab_label": f"relay-{sid}", "status": "reported" if reported else "busy",
            "current_packet": packet, "busy_since": relay.now(), "owner_lead": lead_sid,
        }
        if extra:
            data.update(extra)
        relay.write_session(sid, data)
        if reported:
            (relay.packets_dir(sid) / f"{packet:03d}-report.md").write_text("Fixed it. Staged.")

    def test_check_dedups_report_the_at_stop_path_would_otherwise_reannounce(self, relay):
        self._mk_owned_session(relay, "e1", "lead-1")
        # Before any review: the at-Stop path's own check sees it as fresh.
        assert [k for k, *_ in relay.lead_guard.new_reports_for(relay.STATE_ROOT, "lead-1")] == ["e1:1"]
        relay.cmd_check(SimpleNamespace(session_id="e1", all=False, json=True))
        # After `relay check` reviewed it: the at-Stop path must no longer see it as new.
        assert relay.lead_guard.new_reports_for(relay.STATE_ROOT, "lead-1") == []

    def test_diff_dedups_report(self, relay, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        self._mk_owned_session(relay, "e2", "lead-1", extra={"worktree": str(repo)})
        assert [k for k, *_ in relay.lead_guard.new_reports_for(relay.STATE_ROOT, "lead-1")] == ["e2:1"]
        relay.cmd_diff(SimpleNamespace(session_id="e2", open=False, all=False))
        assert relay.lead_guard.new_reports_for(relay.STATE_ROOT, "lead-1") == []

    def test_close_dedups_report(self, relay):
        self._mk_owned_session(relay, "e3", "lead-1")
        assert [k for k, *_ in relay.lead_guard.new_reports_for(relay.STATE_ROOT, "lead-1")] == ["e3:1"]
        relay.cmd_close(SimpleNamespace(session_id="e3", self_session=None, supersede=None,
                                        keep_tab=True))
        assert relay.lead_guard.new_reports_for(relay.STATE_ROOT, "lead-1") == []

    def test_new_report_after_review_still_surfaces(self, relay):
        """Anti-over-suppression: reviewing packet 1 must not silently swallow packet 2's report
        once the executor is re-sent and reports again — a genuinely NEW report must still wake."""
        self._mk_owned_session(relay, "e4", "lead-1", packet=1)
        relay.cmd_check(SimpleNamespace(session_id="e4", all=False, json=True))
        assert relay.lead_guard.new_reports_for(relay.STATE_ROOT, "lead-1") == []
        # Executor moves to packet 2 and reports again.
        s = relay.read_session("e4")
        s["current_packet"] = 2
        s["status"] = "reported"
        relay.write_session("e4", s)
        (relay.packets_dir("e4") / "002-report.md").write_text("Second fix. Staged.")
        fresh = relay.lead_guard.new_reports_for(relay.STATE_ROOT, "lead-1")
        assert [k for k, *_ in fresh] == ["e4:2"]

    def test_check_without_report_does_not_mark_surfaced(self, relay):
        # No report yet (busy) → nothing to dedup; must not pre-emptively suppress the eventual
        # report (the backlog's explicit "do not mark on the mere nudge" warning, mirrored here for
        # "do not mark before there's even anything to review").
        self._mk_owned_session(relay, "e5", "lead-1", reported=False)
        relay.cmd_check(SimpleNamespace(session_id="e5", all=False, json=True))
        assert relay.lead_guard.load_surfaced(relay.STATE_ROOT, "lead-1") == set()

    def test_check_unowned_session_is_a_noop(self, relay):
        # No owner_lead recorded → nothing to stamp anywhere; must not raise.
        relay.packets_dir("e6").mkdir(parents=True, exist_ok=True)
        relay.write_session("e6", {
            "session_id": "e6", "worktree": "/tmp/wt", "topic": "t", "scope": "t",
            "tab_label": "relay-e6", "status": "reported", "current_packet": 1,
            "busy_since": relay.now(),
        })
        (relay.packets_dir("e6") / "001-report.md").write_text("done")
        relay.cmd_check(SimpleNamespace(session_id="e6", all=False, json=True))  # must not raise


class TestReportPointsToDiff:
    """`relay report` appends a discoverability line pointing at `relay diff --open` only when a
    staged diff actually exists — never generates the HTML itself."""

    def _git(self, repo, *args):
        import subprocess
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    def _mk_session(self, relay, sid, repo):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        relay.write_session(sid, {"session_id": sid, "worktree": str(repo), "topic": "t",
            "scope": "t", "tab_label": f"relay-{sid}", "status": "reported",
            "current_packet": 1, "busy_since": relay.now()})
        (relay.packets_dir(sid) / "001-report.md").write_text("did the thing")

    def test_pointer_line_appears_with_staged_diff(self, relay, tmp_path, capsys):
        repo = tmp_path / "repo"; repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "a.py").write_text("a")
        self._git(repo, "add", "a.py")
        self._git(repo, "commit", "-m", "init")
        (repo / "a.py").write_text("a\nchanged\n")
        self._git(repo, "add", "a.py")
        self._mk_session(relay, "e1", repo)
        relay.cmd_report(SimpleNamespace(session_id="e1", packet=None))
        out = capsys.readouterr().out
        assert "relay diff e1 --open" in out
        # No HTML must be generated just from calling `relay report` — discoverability only.
        assert not (relay.packets_dir("e1") / "001-diff.html").exists()

    def test_pointer_absent_when_nothing_staged(self, relay, tmp_path, capsys):
        repo = tmp_path / "repo"; repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "a.py").write_text("a")
        self._git(repo, "add", "a.py")
        self._git(repo, "commit", "-m", "init")   # nothing staged after this
        self._mk_session(relay, "e1", repo)
        relay.cmd_report(SimpleNamespace(session_id="e1", packet=None))
        out = capsys.readouterr().out
        assert "relay diff" not in out


class TestAdoption:
    """_maybe_adopt re-parents an executor's owner_lead to the CALLING lead at each claim point
    (send/resume/restart) and via the explicit `relay adopt` command — the fix for the handoff bug
    where owner_lead is stamped once at spawn and never updated, silently killing auto-wake for
    whichever lead inherits the executor. iterm.send/spawn/is_alive and the pid/iterm-id readers are
    mocked so no real iTerm/claude is touched."""

    def _mk(self, relay, sid="e1", owner_lead=None, status="reported", claude_session="cs-x"):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)  # also creates session_dir
        relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
            "tab_label": f"relay-{sid}", "model": None, "pid": None, "iterm_session": "w0t0p0:OLD",
            "claude_session": claude_session, "status": status, "current_packet": 1,
            "owner_lead": owner_lead, "owner_project": None,
            "busy_since": relay.now(), "created": relay.now(), "updated": relay.now()})
        (relay.packets_dir(sid) / "001-packet.md").write_text("first packet")
        (relay.packets_dir(sid) / "001-report.md").write_text("done")

    def _mk_resumable(self, relay, sid="e1", owner_lead=None, claude_session="uuid-old"):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)  # also creates session_dir
        relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
            "tab_label": "relay-e1", "model": None, "pid": 999, "iterm_session": "w0t0p0:OLD",
            "claude_session": claude_session, "status": "dead", "current_packet": 1,
            "owner_lead": owner_lead, "owner_project": None,
            "busy_since": relay.now(), "created": relay.now(), "updated": relay.now()})
        (relay.packets_dir(sid) / "001-packet.md").write_text("do the work")

    def _run_relaunch(self, relay, fn, sid="e1", alive=False, force=False):
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True), \
             mock.patch.object(relay, "pid_alive", return_value=alive):
            fn(SimpleNamespace(session_id=sid, force=force))
        return captured

    def _packet(self, relay, tmp_path):
        p = tmp_path / "next-packet.md"
        p.write_text("# Follow-up\n\nDo the next thing.")
        return str(p)

    def _env(self, monkeypatch, sid):
        if sid is None:
            monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

    def _ledger_events(self, relay):
        return [json.loads(l) for l in relay.LEDGER.read_text().splitlines()] if relay.LEDGER.exists() else []

    def test_send_adopts_from_retired_lead(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp")
        self._mk(relay, owner_lead="old-lead")
        self._env(monkeypatch, "new-lead")
        with mock.patch.object(relay.iterm, "send", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        s = relay.read_session("e1")
        assert s["owner_lead"] == "new-lead"
        assert s["owner_project"] == "webapp"
        events = self._ledger_events(relay)
        assert any(e["event"] == "adopted" and e.get("from_lead") == "old-lead" for e in events)

    def test_send_adopts_unowned(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp")
        self._mk(relay, owner_lead=None)
        self._env(monkeypatch, "new-lead")
        with mock.patch.object(relay.iterm, "send", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        s = relay.read_session("e1")
        assert s["owner_lead"] == "new-lead"
        events = self._ledger_events(relay)
        assert any(e["event"] == "adopted" and e.get("from_lead") is None for e in events)

    def test_send_warns_but_does_not_steal_from_live_lead(self, relay, tmp_path, monkeypatch, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "other-lead", project="beta")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp")
        self._mk(relay, owner_lead="other-lead")
        self._env(monkeypatch, "new-lead")
        with mock.patch.object(relay.iterm, "send", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True), \
             mock.patch.object(relay, "_lead_alive", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        s = relay.read_session("e1")
        assert s["owner_lead"] == "other-lead"                      # NOT stolen
        assert s["current_packet"] == 2                             # packet still delivered
        err = capsys.readouterr().err
        assert "owned by live lead" in err and "relay adopt e1 --force" in err

    def test_resume_adopts_from_retired_lead(self, relay, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp")
        self._mk_resumable(relay, owner_lead="old-lead")
        self._env(monkeypatch, "new-lead")
        self._run_relaunch(relay, relay.cmd_resume)
        assert relay.read_session("e1")["owner_lead"] == "new-lead"

    def test_restart_adopts_from_retired_lead(self, relay, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp")
        self._mk_resumable(relay, owner_lead="old-lead")
        self._env(monkeypatch, "new-lead")
        self._run_relaunch(relay, relay.cmd_restart)
        assert relay.read_session("e1")["owner_lead"] == "new-lead"

    def test_adopt_command_force_takes_from_live_lead(self, relay, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "other-lead", project="beta")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp")
        self._mk(relay, owner_lead="other-lead")
        self._env(monkeypatch, "new-lead")
        with mock.patch.object(relay, "_lead_alive", return_value=True):
            relay.cmd_adopt(SimpleNamespace(session_id="e1", force=True))
        s = relay.read_session("e1")
        assert s["owner_lead"] == "new-lead"
        events = self._ledger_events(relay)
        adopted = [e for e in events if e["event"] == "adopted"]
        assert adopted and adopted[-1]["forced"] is True

    def test_adopt_refuses_non_lead_caller(self, relay, monkeypatch):
        self._mk(relay, owner_lead="old-lead")
        self._env(monkeypatch, "no-marker-caller")  # env sid set but never armed as a lead
        with pytest.raises(SystemExit):
            relay.cmd_adopt(SimpleNamespace(session_id="e1", force=False))
        assert relay.read_session("e1")["owner_lead"] == "old-lead"

    def test_no_env_no_adoption(self, relay, tmp_path, monkeypatch):
        self._mk(relay, owner_lead="old-lead")
        self._env(monkeypatch, None)  # bare-terminal invocation: no CLAUDE_CODE_SESSION_ID at all
        with mock.patch.object(relay.iterm, "send", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        assert relay.read_session("e1")["owner_lead"] == "old-lead"

    def test_list_footnotes_orphaned_executors(self, relay, capsys, monkeypatch):
        monkeypatch.setattr(relay, "pid_alive", lambda pid: True)
        self._mk(relay, owner_lead="ghost-lead", status="busy")
        relay.cmd_list(SimpleNamespace(json=False, lead=None, all=False, closed=False))
        out = capsys.readouterr().out
        assert "⚠" in out and "e1" in out
        assert "relay adopt" in out

    def test_same_project_handoff_auto_transfers(self, relay, tmp_path, monkeypatch):
        # A real regression: old + new lead share the SAME project (and thus the same
        # derived tab_label), but the old lead has no live pid and an unresolvable iterm_session —
        # that tab-label collision must NOT block the transfer; it must auto-adopt, not warn.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "old-lead", project="webapp",
                                       tab_label="[Lead] webapp", iterm_session="w0t0p0:OLDLEAD")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp",
                                       tab_label="[Lead] webapp", iterm_session="w0t0p0:NEWLEAD")
        self._mk(relay, owner_lead="old-lead")
        self._env(monkeypatch, "new-lead")
        with mock.patch.object(relay.iterm, "send", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True), \
             mock.patch.object(relay.iterm, "tty_by_id", return_value=None):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(relay, tmp_path)))
        s = relay.read_session("e1")
        assert s["owner_lead"] == "new-lead"


class TestLeadStart:
    """`relay lead-start <sid>` (invoked by /relay:mode): idempotent arm/re-arm. A re-arm on an
    already-armed session must preserve handoff/history state (`predecessor`, `started`) that only
    the FIRST arm sets — a bug let the successor's post-handoff /relay:mode verify silently wipe
    both fields."""

    def _args(self, session_id, model=None, project=None):
        return SimpleNamespace(session_id=session_id, model=model, project=project, no_rename=True)

    def test_fresh_arm_has_no_predecessor(self, relay, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        relay.cmd_lead_start(self._args("lead-1"))
        marker = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert marker["predecessor"] is None
        assert marker["started"] is not None

    def test_rearm_preserves_predecessor_and_started(self, relay, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        predecessor = {"session_id": "old-lead", "tab_label": "[Lead] webapp", "iterm_session": "w1t1p0:OLD"}
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path),
                                       predecessor=predecessor, started="2020-01-01T00:00:00")
        relay.cmd_lead_start(self._args("lead-1", project="webapp"))
        marker = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert marker["predecessor"] == predecessor
        assert marker["started"] == "2020-01-01T00:00:00"

    def test_rearm_refreshes_last_active(self, relay, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path),
                                       started="2020-01-01T00:00:00")
        monkeypatch.setattr(relay.lead_guard, "now", lambda: "2030-01-01T00:00:00")
        relay.cmd_lead_start(self._args("lead-1", project="webapp"))
        marker = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert marker["last_active"] == "2030-01-01T00:00:00"
        assert marker["started"] == "2020-01-01T00:00:00"   # unaffected by the last_active refresh

    def test_records_backend(self, relay, tmp_path, monkeypatch):
        # D2 (Defect A's root cause): lead-start never used to stamp a backend at all, so a fresh
        # marker's `backend` read null and `term_backend`'s `or iterm` fallback in cmd_nudge_lead
        # fell straight through to whichever backend the CALLER happened to be running under.
        monkeypatch.chdir(tmp_path)
        relay.cmd_lead_start(self._args("lead-1"))
        marker = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert marker["backend"] == relay.iterm.NAME
        assert marker["backend"] in ("iterm", "terminal")

    def test_rearm_restamps_backend(self, relay, tmp_path, monkeypatch):
        # Unlike predecessor/started, backend is NOT preserved across re-arms — it always reflects
        # whatever backend THIS arm actually ran under (e.g. relay re-armed from a different shell).
        monkeypatch.chdir(tmp_path)
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp",
                                      cwd=str(tmp_path), backend="stale-backend")
        relay.cmd_lead_start(self._args("lead-1", project="webapp"))
        marker = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert marker["backend"] == relay.iterm.NAME


class TestHandoff:
    """`relay handoff <handoff.md>`: pre-arm a successor lead's marker for a pinned session id,
    spawn its tab seeded with the handoff file, then step the caller down as the final act. Mirrors
    cmd_resume_lead's iterm_id-capture pattern (packet 002) — reused via read_iterm_id_at."""
    def _env(self, monkeypatch, sid):
        if sid is None:
            monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

    def _md(self, tmp_path, text="what's in flight, what's done, next steps"):
        p = tmp_path / "handoff.md"
        p.write_text(text)
        return p

    def _leads(self, relay):
        return {m["session_id"]: m for m in relay.lead_guard.list_leads(relay.STATE_ROOT)}

    def _ledger_events(self, relay):
        return [json.loads(l) for l in relay.LEDGER.read_text().splitlines()] if relay.LEDGER.exists() else []

    def _run(self, relay, handoff_md, project=None, model=None, iterm_id="w1t1p0:NEW", spawn_side_effect=None):
        captured = {}
        spawn_effect = spawn_side_effect or (lambda **kw: captured.update(kw))
        with mock.patch.object(relay.iterm, "spawn", side_effect=spawn_effect), \
             mock.patch.object(relay, "read_iterm_id_at", return_value=iterm_id):
            relay.cmd_handoff(SimpleNamespace(handoff_md=str(handoff_md), project=project, model=model))
        return captured

    def test_handoff_prearms_successor(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        md = self._md(tmp_path)
        cap = self._run(relay, md)
        leads = self._leads(relay)
        successors = [m for sid, m in leads.items() if sid != "lead-1"]
        assert len(successors) == 1
        succ = successors[0]
        assert succ["project"] == "webapp"
        assert succ["tab_label"] == "[Lead] webapp"
        assert succ["color"] is not None
        assert succ["plugin_version"] is not None
        assert "stop_hook_timeout" in succ
        assert cap["session_uuid"] == succ["session_id"]
        # DELIBERATE spec change #2 (truncation-fix packet): the seed no longer points at the
        # user's own md — it points at relay's own copy under the successor's lead dir (aftercare
        # appended there instead of inlined in the prompt; see test_handoff_seed_prompt_is_short_pointer).
        copy_path = relay.lead_guard.lead_dir(relay.STATE_ROOT, succ["session_id"]) / "handoff.md"
        assert str(copy_path) in cap["prompt"]
        assert str(md) not in cap["prompt"]
        # DELIBERATE spec change (aftercare packet): the successor now verifies its own arming via
        # /relay:mode after settling instead of being told never to run it — see
        # test_handoff_copy_has_aftercare_section for the full replacement spec.
        assert "do NOT run /relay:mode" not in cap["prompt"]

    def test_handoff_steps_caller_down(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        md = self._md(tmp_path)
        self._run(relay, md)
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1") == {}
        events = self._ledger_events(relay)
        hoff = [e for e in events if e["event"] == "lead_handoff"]
        assert len(hoff) == 1
        assert hoff[0]["from_lead"] == "lead-1"
        successors = [sid for sid in self._leads(relay) if sid != "lead-1"]
        assert hoff[0]["to_lead"] == successors[0]

    def test_handoff_spawn_failure_preserves_caller(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        md = self._md(tmp_path)

        def _boom(**kw):
            raise RuntimeError("spawn failed")

        with pytest.raises(SystemExit):
            self._run(relay, md, spawn_side_effect=_boom)
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1") != {}   # caller intact
        successors = [sid for sid in self._leads(relay) if sid != "lead-1"]
        assert successors == []                                                # no ghost

    def test_handoff_refuses_non_lead(self, relay, tmp_path, monkeypatch):
        self._env(monkeypatch, "not-a-lead")   # env sid set but never armed
        md = self._md(tmp_path)
        with mock.patch.object(relay.iterm, "spawn") as spawn_mock:
            with pytest.raises(SystemExit):
                relay.cmd_handoff(SimpleNamespace(handoff_md=str(md), project=None, model=None))
            spawn_mock.assert_not_called()

    def test_handoff_refuses_missing_md(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        with pytest.raises(SystemExit):
            relay.cmd_handoff(SimpleNamespace(handoff_md=str(tmp_path / "nope.md"), project=None, model=None))
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1") != {}

    def test_handoff_defaults_project_model_from_caller_marker(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", model="opus",
                                       cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        md = self._md(tmp_path)
        self._run(relay, md)
        successors = [m for sid, m in self._leads(relay).items() if sid != "lead-1"]
        assert successors[0]["project"] == "webapp"
        assert successors[0]["model"] == "opus"

    def test_handoff_records_predecessor(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path),
                                       tab_label="[Lead] webapp", iterm_session="w1t1p0:OLD")
        self._env(monkeypatch, "lead-1")
        md = self._md(tmp_path)
        self._run(relay, md)
        successors = [m for sid, m in self._leads(relay).items() if sid != "lead-1"]
        pred = successors[0]["predecessor"]
        assert pred["session_id"] == "lead-1"
        assert pred["tab_label"] == "[Lead] webapp"
        assert pred["iterm_session"] == "w1t1p0:OLD"

    def test_handoff_seed_prompt_is_short_pointer(self, relay, tmp_path, monkeypatch):
        # REGRESSION PIN (live incident, tonight): the seed prompt inlined the full aftercare text
        # WITH the pinned UUID twice, and the typed launch command (cd + tab-color printf +
        # pidfile + iterm_id + `exec claude --session-id <uuid> '<seed>'`) truncated mid-string —
        # visible cutoff at "relay stop f268fe91-fdbc-4fb8-a04" — leaving an unbalanced quote, zsh
        # fell into its `quote>` continuation prompt, and the successor never launched. The seed
        # must now be a short pointer at a file, not an inlined instruction block.
        #
        # STATE_ROOT is repointed to a short tmp dir (not the `relay` fixture's deep pytest
        # tmp_path, which nests the full test id and would inflate the copy path length well past
        # any real ~/.relay-tasks/lead/<uuid>/handoff.md — that's a pytest artifact, not something
        # the 300-char budget is meant to absorb).
        import tempfile
        short_root = Path(tempfile.mkdtemp(prefix="rl-")) / ".relay-tasks"
        monkeypatch.setattr(relay, "STATE_ROOT", short_root)
        monkeypatch.setattr(relay, "LEDGER", short_root / "sessions.jsonl")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        md = self._md(tmp_path)
        cap = self._run(relay, md)
        prompt = cap["prompt"]
        assert len(prompt) < 300, f"seed prompt is {len(prompt)} chars — must stay a short pointer: {prompt!r}"
        copy_path = relay.lead_guard.lead_dir(relay.STATE_ROOT, cap["session_uuid"]) / "handoff.md"
        assert str(copy_path) in prompt
        # The uuid appears exactly ONCE, as the copy path's directory name (the same structural
        # convention as marker.json/pid/iterm_id under lead_dir(sid) — not something worth avoiding).
        # What actually caused the truncation, and what must NOT reappear, is the aftercare text
        # being inlined a second time alongside it (the old prompt used the pin in two separate
        # sentences — "matches the pinned '<uuid>'" AND "relay stop <uuid>" — a much longer string).
        assert prompt.count(cap["session_uuid"]) == 1
        assert "relay stop" not in prompt   # aftercare content lives in the copy file now, not here

    def test_handoff_copy_contains_user_content(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        md = self._md(tmp_path, text="what's in flight: the parser rewrite. next: wire the CLI.")
        cap = self._run(relay, md)
        copy_path = relay.lead_guard.lead_dir(relay.STATE_ROOT, cap["session_uuid"]) / "handoff.md"
        copy_text = copy_path.read_text()
        assert copy_text.startswith("what's in flight: the parser rewrite. next: wire the CLI.")

    def test_handoff_copy_has_aftercare_section(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        md = self._md(tmp_path)
        cap = self._run(relay, md)
        copy_path = relay.lead_guard.lead_dir(relay.STATE_ROOT, cap["session_uuid"]) / "handoff.md"
        copy_text = copy_path.read_text()
        assert "SUCCESSOR AFTERCARE" in copy_text
        assert "/relay:mode" in copy_text
        assert cap["session_uuid"] in copy_text
        assert "relay stop" in copy_text
        assert "close-predecessor" in copy_text
        assert "do NOT run /relay:mode" not in copy_text

    def test_handoff_copy_write_failure_preserves_caller(self, relay, tmp_path, monkeypatch):
        # Same invariant as a failed spawn: writing the relay-owned copy is on the critical path
        # BEFORE the successor's tab exists, so a failure there must leave the caller as lead and
        # drop no ghost marker either.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        md = self._md(tmp_path)
        # Fails specifically the try/except around the copy write (build_handoff_copy is the last
        # thing called before handoff_copy_path.write_text) — NOT the earlier pre-arm write_marker
        # call, which must still succeed so this stays an isolated "copy write fails" case.
        with mock.patch.object(relay, "build_handoff_copy", side_effect=OSError("disk full")), \
             mock.patch.object(relay.iterm, "spawn") as spawn_mock:
            with pytest.raises(SystemExit):
                relay.cmd_handoff(SimpleNamespace(handoff_md=str(md), project=None, model=None))
            spawn_mock.assert_not_called()
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1") != {}   # caller intact
        successors = [sid for sid in self._leads(relay) if sid != "lead-1"]
        assert successors == []                                                # no ghost


class TestClosePredecessor:
    """`relay close-predecessor`: the successor-only aftercare step that closes the outgoing lead's
    now-unarmed zombie tab, recorded in the successor's own marker at handoff time."""

    def _env(self, monkeypatch, sid):
        if sid is None:
            monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

    def _ledger_events(self, relay):
        return [json.loads(l) for l in relay.LEDGER.read_text().splitlines()] if relay.LEDGER.exists() else []

    def test_close_predecessor_closes_and_clears(self, relay, tmp_path, monkeypatch):
        predecessor = {"session_id": "old-lead", "tab_label": "[Lead] webapp", "iterm_session": "w1t1p0:OLD"}
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp", cwd=str(tmp_path),
                                       predecessor=predecessor)
        self._env(monkeypatch, "new-lead")
        with mock.patch.object(relay.iterm, "close", return_value=True) as close_mock:
            relay.cmd_close_predecessor(SimpleNamespace())
        close_mock.assert_called_once_with("[Lead] webapp", "w1t1p0:OLD", None)
        marker = relay.lead_guard.read_marker(relay.STATE_ROOT, "new-lead")
        assert "predecessor" not in marker or not marker["predecessor"]
        events = self._ledger_events(relay)
        closed_events = [e for e in events if e["event"] == "predecessor_closed"]
        assert len(closed_events) == 1
        assert closed_events[0]["predecessor"] == "old-lead"
        assert closed_events[0]["tab_closed"] is True

    def test_close_predecessor_refuses_live_lead(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "old-lead", project="webapp", cwd=str(tmp_path),
                                       tab_label="[Lead] webapp")
        predecessor = {"session_id": "old-lead", "tab_label": "[Lead] webapp", "iterm_session": None}
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp", cwd=str(tmp_path),
                                       predecessor=predecessor)
        self._env(monkeypatch, "new-lead")
        with mock.patch.object(relay.iterm, "close") as close_mock:
            with pytest.raises(SystemExit):
                relay.cmd_close_predecessor(SimpleNamespace())
            close_mock.assert_not_called()
        marker = relay.lead_guard.read_marker(relay.STATE_ROOT, "new-lead")
        assert marker["predecessor"] == predecessor

    def test_close_predecessor_no_predecessor_noop(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "new-lead", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "new-lead")
        with mock.patch.object(relay.iterm, "close") as close_mock:
            relay.cmd_close_predecessor(SimpleNamespace())  # no exception
            close_mock.assert_not_called()

    def test_close_predecessor_refuses_non_lead(self, relay, monkeypatch):
        self._env(monkeypatch, "not-a-lead")
        with pytest.raises(SystemExit):
            relay.cmd_close_predecessor(SimpleNamespace())


class TestStatus:
    """`relay status` — strictly read-only, statusline-safe one-liner. No write_session/
    append_ledger/_check_one/marker-touch anywhere in this path (unlike `relay list`/`check`)."""

    def _exec(self, relay, sid, owner_lead=None, status="busy", claude_session="cs-x", packet=1, report=False):
        relay.write_session(sid, {
            "session_id": sid, "owner_lead": owner_lead, "claude_session": claude_session,
            "current_packet": packet, "status": status, "topic": "t", "worktree": "/w",
            "scope": "", "model": "opus", "busy_since": relay.now(), "updated": relay.now(),
        })
        if report:
            d = relay.packets_dir(sid)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{packet:03d}-report.md").write_text("done")

    def _args(self, session_id=None, statusline=False):
        return SimpleNamespace(session_id=session_id, statusline=statusline)

    def test_lead_view_counts_and_names(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        self._exec(relay, "e1", owner_lead="lead-1", status="busy", report=False)
        self._exec(relay, "e2", owner_lead="lead-1", status="busy", report=False)
        self._exec(relay, "e3", owner_lead="lead-1", status="busy", report=True)
        relay.cmd_status(self._args(session_id="lead-1"))
        out = capsys.readouterr().out
        # DELIBERATE spec change (packet 004, live-usage feedback): busy executors are now named,
        # not just counted — a bare "2 busy" doesn't tell you WHO.
        assert "busy: e1,e2" in out
        assert "✅" in out and "e3" in out
        assert "WAKE" not in out

    def test_lead_view_names_busy_executors(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        self._exec(relay, "e1", owner_lead="lead-1", status="busy")
        self._exec(relay, "e2", owner_lead="lead-1", status="busy")
        relay.cmd_status(self._args(session_id="lead-1"))
        out = capsys.readouterr().out
        assert "e1" in out and "e2" in out
        assert "2 busy" not in out   # the old count-only format must be gone

    def test_lead_view_busy_overflow(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        for sid in ("e1", "e2", "e3", "e4"):
            self._exec(relay, sid, owner_lead="lead-1", status="busy")
        relay.cmd_status(self._args(session_id="lead-1"))
        out = capsys.readouterr().out
        assert "busy: e1,e2,e3 +1" in out
        assert "e4" not in out

    def test_reported_upgrade_is_read_only(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        self._exec(relay, "e1", owner_lead="lead-1", status="busy", report=True)
        before = (relay.session_dir("e1") / "session.json").read_bytes()
        relay.cmd_status(self._args(session_id="lead-1"))
        out = capsys.readouterr().out
        assert "e1" in out and "✅" in out
        assert "busy" not in out   # report-file existence upgrades it out of the busy count
        after = (relay.session_dir("e1") / "session.json").read_bytes()
        assert before == after

    def test_executor_view(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        self._exec(relay, "e1", owner_lead="lead-1", claude_session="cs-1", status="busy")
        relay.cmd_status(self._args(session_id="cs-1"))
        out = capsys.readouterr().out
        # DELIBERATE spec change (packet 004 + lead's review, live-usage feedback): the
        # " -> overall: lead tab" tail was noise — dropped entirely; and the owner label reads
        # "for <project>" ("lead: <project>" was ambiguous inside an executor tab).
        assert "pkt" in out and "for webapp" in out
        assert "overall" not in out and "lead:" not in out

    def test_executor_view_by_relay_name(self, relay, capsys):
        # REGRESSION (observed live): `relay status e1` printed nothing. The executor
        # branch only matched by claude_session (the --statusline payload's id space), so the
        # RELAY session id/name a human types classified as "neither" → silent exit. Both id
        # spaces must work.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        self._exec(relay, "e1", owner_lead="lead-1", claude_session="cs-1", status="busy")
        relay.cmd_status(self._args(session_id="e1"))   # relay name, NOT the claude_session
        out = capsys.readouterr().out
        assert "pkt" in out and "for webapp" in out

    def test_unknown_session_prints_nothing(self, relay, capsys):
        relay.cmd_status(self._args(session_id="nobody-here"))
        assert capsys.readouterr().out == ""

    def test_statusline_stdin_json(self, relay, capsys, monkeypatch):
        import io
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        self._exec(relay, "e1", owner_lead="lead-1", status="busy")
        monkeypatch.setattr(relay.sys, "stdin", io.StringIO(json.dumps({"session_id": "lead-1"})))
        relay.cmd_status(self._args(statusline=True))
        out = capsys.readouterr().out
        assert "busy" in out

    def test_statusline_garbage_stdin_quiet(self, relay, capsys, monkeypatch):
        import io
        monkeypatch.setattr(relay.sys, "stdin", io.StringIO("not json"))
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        relay.cmd_status(self._args(statusline=True))
        assert capsys.readouterr().out == ""

    def test_wake_stuck_surfaces(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        self._exec(relay, "e1", owner_lead="lead-1", status="busy")
        lock_path = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1") / "poll.lock"
        lock_path.write_text(json.dumps({"pid": 999999, "pid_started": None, "ts": 1.0}))
        relay.cmd_status(self._args(session_id="lead-1"))
        out = capsys.readouterr().out
        assert "WAKE stuck" in out

    def test_armed_lead_is_visible_even_with_no_executors(self, relay, capsys):
        """DELIBERATE behavior change (was: printed nothing). An armed lead with no executors and
        nothing wrong used to render an EMPTY status line — indistinguishable from not being a lead
        at all. That invisibility is the root of the silent-unarming class of bug
        (docs/lead-arming-durability.md): 'am I armed?' had no glanceable answer. A blank line must
        now mean 'not a lead', never 'armed but quiet'."""
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        relay.cmd_status(self._args(session_id="lead-1"))
        assert "webapp" in capsys.readouterr().out

    def test_paused_lead_renders_as_paused_not_armed(self, relay, capsys):
        """A tombstoned lead is NOT armed (gate and wake off), so it must say so rather than render
        the armed token — and must not show executor/wake segments implying a live watcher."""
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        relay.lead_guard.tombstone_lead(relay.STATE_ROOT, "lead-1")
        relay.cmd_status(self._args(session_id="lead-1"))
        out = capsys.readouterr().out
        assert "paused" in out and "webapp" in out
        assert "🚦" not in out  # must not look armed

    # ---- transcript-size / handoff-awareness segment (--statusline mode only) ------------------

    def _sparse_file(self, path, mb):
        """A file of the given size WITHOUT writing that many real bytes (truncate, sparse) — same
        trick TestHandoffNudge uses so these tests don't actually write megabytes to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(int(mb * 1024 * 1024))
        return path

    def _statusline(self, relay, monkeypatch, sid="lead-1", transcript_path=None):
        import io
        payload = {"session_id": sid}
        if transcript_path is not None:
            payload["transcript_path"] = str(transcript_path)
        monkeypatch.setattr(relay.sys, "stdin", io.StringIO(json.dumps(payload)))
        relay.cmd_status(self._args(statusline=True))

    def test_status_weight_segment_below_60pct_silent(self, relay, capsys, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        transcript = self._sparse_file(tmp_path / "t.jsonl", 2)   # 2MB / 5MB default threshold = 40%
        self._statusline(relay, monkeypatch, transcript_path=transcript)
        out = capsys.readouterr().out
        assert "MB" not in out

    def test_status_weight_segment_approaching(self, relay, capsys, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        transcript = self._sparse_file(tmp_path / "t.jsonl", 3.5)   # 70% of the 5MB default
        self._statusline(relay, monkeypatch, transcript_path=transcript)
        out = capsys.readouterr().out
        assert "3.5MB" in out
        assert "handoff" not in out

    def test_status_weight_segment_at_threshold_points_to_handoff(self, relay, capsys, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        transcript = self._sparse_file(tmp_path / "t.jsonl", 6)   # over the 5MB default threshold
        self._statusline(relay, monkeypatch, transcript_path=transcript)
        out = capsys.readouterr().out
        assert "→ /relay:handoff" in out

    def test_status_weight_segment_after_nudge_flag_persists(self, relay, capsys, tmp_path, monkeypatch):
        # Flag set but transcript now small: the flag is the durable fact, size math is not
        # re-consulted to decide WHETHER to point (only mb > 0 gates it, per the packet's rule).
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        relay.lead_guard.mark_handoff_nudged(relay.STATE_ROOT, "lead-1")
        transcript = self._sparse_file(tmp_path / "t.jsonl", 2)
        self._statusline(relay, monkeypatch, transcript_path=transcript)
        out = capsys.readouterr().out
        assert "→ /relay:handoff" in out

    def test_status_weight_absent_without_statusline(self, relay, capsys, tmp_path):
        # Positional-id invocation: no JSON payload was ever parsed, so there's no transcript_path
        # to measure — the segment must not appear, even for a lead that (unbeknownst to this
        # invocation) is objectively heavy.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        relay.lead_guard.mark_handoff_nudged(relay.STATE_ROOT, "lead-1")
        relay.cmd_status(self._args(session_id="lead-1"))
        out = capsys.readouterr().out
        assert "MB" not in out and "handoff" not in out

    def test_status_weight_respects_disabled_config(self, relay, capsys, tmp_path, monkeypatch):
        (relay.STATE_ROOT / "lead").mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "lead" / "config.json").write_text(json.dumps({"handoff_nudge": False}))
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        transcript = self._sparse_file(tmp_path / "t.jsonl", 6)   # would otherwise point to handoff
        self._statusline(relay, monkeypatch, transcript_path=transcript)
        out = capsys.readouterr().out
        assert "MB" not in out and "handoff" not in out

    def test_status_weight_segment_no_writes(self, relay, capsys, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        self._exec(relay, "e1", owner_lead="lead-1", status="busy")
        transcript = self._sparse_file(tmp_path / "t.jsonl", 6)
        session_before = (relay.session_dir("e1") / "session.json").read_bytes()
        lead_dir = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1")
        files_before = sorted(p.relative_to(lead_dir) for p in lead_dir.rglob("*") if p.is_file())
        self._statusline(relay, monkeypatch, transcript_path=transcript)
        out = capsys.readouterr().out
        assert "→ /relay:handoff" in out   # the segment actually rendered
        session_after = (relay.session_dir("e1") / "session.json").read_bytes()
        files_after = sorted(p.relative_to(lead_dir) for p in lead_dir.rglob("*") if p.is_file())
        assert session_before == session_after
        assert files_before == files_after


class TestResolveSid:
    """resolve_sid: the ONE name-resolution surface every sid-accepting command/flag routes
    through (wired centrally in main()). Precedence: exact executor id > exact lead id > unique
    lead project name > unique sid prefix (len>=6) > unchanged passthrough."""

    def _run_main(self, relay, monkeypatch, argv):
        monkeypatch.setattr(relay.sys, "argv", ["relay"] + argv)
        relay.main()

    def test_executor_exact_id_wins_over_lead_project_collision(self, relay):
        # Executor named "docs-site" AND a lead whose project is ALSO "docs-site" — precedence (a)
        # must pin to the executor, never fall through to the project-name branch.
        relay.write_session("docs-site", {"session_id": "docs-site", "current_packet": 1, "status": "busy",
            "topic": "t", "worktree": "/w", "scope": "", "model": "opus", "owner_lead": None})
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-x", project="docs-site")
        assert relay.resolve_sid("docs-site") == "docs-site"

    def test_lead_project_name_resolves_end_to_end(self, relay, capsys, monkeypatch):
        # Proves the resolver is actually wired into main() -> cmd_status, not just a unit that
        # works in isolation: the CLI is invoked with the project NAME, never the raw lead sid.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        relay.write_session("e1", {"session_id": "e1", "current_packet": 1, "status": "busy",
            "topic": "t", "worktree": "/w", "scope": "", "model": "opus", "owner_lead": "lead-1",
            "busy_since": relay.now(), "updated": relay.now()})
        self._run_main(relay, monkeypatch, ["status", "webapp"])
        out = capsys.readouterr().out
        # DELIBERATE spec change (packet 004): busy executors are named, not counted.
        assert "busy: e1" in out

    def test_ambiguous_project_name_exits_with_candidates(self, relay, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-aaaaaaaa", project="dup")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-bbbbbbbb", project="dup")
        monkeypatch.setattr(relay.sys, "argv", ["relay", "status", "dup"])
        with pytest.raises(SystemExit) as exc:
            relay.main()
        msg = str(exc.value)
        assert "lead-aaaaaaaa" in msg and "lead-bbbbbbbb" in msg
        assert "unique prefix" in msg

    def test_unique_prefix_resolves(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "abcdef1234567890", project="proj1")
        assert relay.resolve_sid("abcdef") == "abcdef1234567890"

    def test_ambiguous_prefix_exits_with_candidates(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "abcdef111111", project="p1")
        relay.lead_guard.write_marker(relay.STATE_ROOT, "abcdef222222", project="p2")
        with pytest.raises(SystemExit) as exc:
            relay.resolve_sid("abcdef")
        msg = str(exc.value)
        assert "abcdef111111" in msg and "abcdef222222" in msg
        assert "unique prefix" in msg

    def test_unknown_token_passes_through_unchanged(self, relay, monkeypatch):
        # No executor, no lead, no prefix match — the resolver returns it verbatim and cmd_focus's
        # OWN pre-existing "no such session" message fires, unmodified.
        monkeypatch.setattr(relay.sys, "argv", ["relay", "focus", "totally-unknown-xyz"])
        with pytest.raises(SystemExit) as exc:
            relay.main()
        assert "no such session: totally-unknown-xyz" in str(exc.value)

    def test_resolve_sid_is_read_only(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", stop_hook_timeout=1800)
        lead_dir = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1")
        files_before = sorted(p.relative_to(lead_dir) for p in lead_dir.rglob("*") if p.is_file())
        relay.resolve_sid("webapp")
        relay.resolve_sid("nonexistent-thing")
        relay.resolve_sid("lead-1")
        files_after = sorted(p.relative_to(lead_dir) for p in lead_dir.rglob("*") if p.is_file())
        assert files_before == files_after

    def test_spawn_lead_flag_resolves_project_name(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp")
        pkt = tmp_path / "packet.md"
        pkt.write_text("do the thing")
        with mock.patch.object(relay.iterm, "spawn"), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value=None):
            self._run_main(relay, monkeypatch,
                ["spawn", str(tmp_path), "topic", str(pkt), "--name", "e1", "--lead", "webapp"])
        s = relay.read_session("e1")
        assert s["owner_lead"] == "lead-1"
        assert s["owner_project"] == "webapp"


class TestUniqueLeadProject:
    """unique_lead_project: auto-suffixing so no two LIVE leads share a project name at arm time."""

    NOW = time.mktime(time.strptime("2026-01-01T12:00:00", "%Y-%m-%dT%H:%M:%S"))

    @staticmethod
    def _stamp(offset_seconds):
        return time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(TestUniqueLeadProject.NOW - offset_seconds))

    def _lead(self, sid, project, offset_seconds=60, last_active=...):
        return {"session_id": sid, "project": project,
                "last_active": self._stamp(offset_seconds) if last_active is ... else last_active}

    def test_fresh_name_returned_unchanged(self, relay):
        leads = [self._lead("other-1", "some-other-project")]
        name, clash = relay.unique_lead_project("claude-relay", "self-1", [], leads, now_ts=self.NOW)
        assert (name, clash) == ("claude-relay", None)

    def test_exact_collision_with_live_lead_suffixes_to_2(self, relay):
        leads = [self._lead("other-1", "claude-relay")]
        name, clash = relay.unique_lead_project("claude-relay", "self-1", [], leads, now_ts=self.NOW)
        assert name == "claude-relay-2"
        assert clash == "other-1"

    def test_dash_2_also_taken_advances_to_smallest_free(self, relay):
        leads = [self._lead("other-1", "claude-relay"), self._lead("other-2", "claude-relay-2")]
        name, clash = relay.unique_lead_project("claude-relay", "self-1", [], leads, now_ts=self.NOW)
        assert name == "claude-relay-3"
        assert clash == "other-1"

    def test_self_session_id_is_not_reserved(self, relay):
        # idempotent re-arm: this session's own existing marker must not suffix itself.
        leads = [self._lead("self-1", "claude-relay")]
        name, clash = relay.unique_lead_project("claude-relay", "self-1", [], leads, now_ts=self.NOW)
        assert (name, clash) == ("claude-relay", None)

    def test_excluded_predecessor_is_not_reserved(self, relay):
        # handoff: successor inherits the predecessor's name rather than getting suffixed.
        leads = [self._lead("predecessor-1", "claude-relay")]
        name, clash = relay.unique_lead_project(
            "claude-relay", "self-1", ["predecessor-1"], leads, now_ts=self.NOW)
        assert (name, clash) == ("claude-relay", None)

    def test_stale_last_active_beyond_window_is_a_ghost(self, relay):
        leads = [self._lead("other-1", "claude-relay",
                             offset_seconds=relay.LEAD_LIVE_WINDOW_SECONDS + 1)]
        name, clash = relay.unique_lead_project("claude-relay", "self-1", [], leads, now_ts=self.NOW)
        assert (name, clash) == ("claude-relay", None)

    def test_unparseable_or_missing_last_active_is_a_ghost(self, relay):
        leads = [self._lead("other-1", "claude-relay", last_active=None),
                 self._lead("other-2", "claude-relay", last_active="not-a-timestamp")]
        name, clash = relay.unique_lead_project("claude-relay", "self-1", [], leads, now_ts=self.NOW)
        assert (name, clash) == ("claude-relay", None)


class TestLaunchBackgroundLabelAssert:
    """_launch_background_label_assert: the detached follow-up _ensure_tab_label kicks off so the
    [Exec] label survives Claude Code's one-time ~6-8s OSC titling (see the relay-exec-label
    packet's Findings) -- iTerm-only, best-effort, never blocks the caller."""

    def test_iterm_with_handle_launches_detached_ensure_label(self, relay):
        bk = mock.Mock(NAME="iterm")
        with mock.patch.object(relay.subprocess, "Popen") as popen:
            relay._launch_background_label_assert(bk, "w1t2p0:UUID", "[Exec] e1")
        popen.assert_called_once()
        argv, kwargs = popen.call_args
        cmd = argv[0]
        assert cmd[0] == relay.sys.executable
        assert cmd[1] == relay.RELAY_BIN
        assert cmd[2:] == ["_ensure-label", "w1t2p0:UUID", "[Exec] e1"]
        assert kwargs.get("start_new_session") is True

    def test_no_handle_does_not_launch(self, relay):
        bk = mock.Mock(NAME="iterm")
        with mock.patch.object(relay.subprocess, "Popen") as popen:
            relay._launch_background_label_assert(bk, None, "[Exec] e1")
        popen.assert_not_called()

    def test_terminal_app_backend_does_not_launch(self, relay):
        bk = mock.Mock(NAME="terminal")
        with mock.patch.object(relay.subprocess, "Popen") as popen:
            relay._launch_background_label_assert(bk, "twid:5", "[Exec] e1")
        popen.assert_not_called()

    def test_launch_failure_is_swallowed(self, relay):
        bk = mock.Mock(NAME="iterm")
        with mock.patch.object(relay.subprocess, "Popen", side_effect=OSError("boom")):
            relay._launch_background_label_assert(bk, "w1t2p0:UUID", "[Exec] e1")  # no raise


class TestEnsureTabLabelBackgroundHandoff:
    """_ensure_tab_label must hand off to _launch_background_label_assert on every return path
    (label already live, label recovered mid-retry, label still missing after all attempts) --
    the synchronous window alone (~4.5s) never reaches past Claude's ~6-8s clobber point."""

    def test_launches_when_already_alive(self, relay):
        bk = mock.Mock(NAME="iterm")
        bk.is_alive.return_value = True
        with mock.patch.object(relay, "_launch_background_label_assert") as launch:
            ok = relay._ensure_tab_label(bk, "h1", "[Exec] e1")
        assert ok is True
        launch.assert_called_once_with(bk, "h1", "[Exec] e1")
        bk.rename_by_id.assert_not_called()

    def test_launches_after_exhausting_retries_still_missing(self, relay):
        bk = mock.Mock(NAME="iterm")
        bk.is_alive.return_value = False
        with mock.patch.object(relay, "_launch_background_label_assert") as launch, \
             mock.patch.object(relay.time, "sleep"):
            ok = relay._ensure_tab_label(bk, "h1", "[Exec] e1", attempts=2, delay=0)
        assert ok is False
        launch.assert_called_once_with(bk, "h1", "[Exec] e1")

    def test_no_handle_never_launches(self, relay):
        bk = mock.Mock(NAME="iterm")
        with mock.patch.object(relay, "_launch_background_label_assert") as launch:
            ok = relay._ensure_tab_label(bk, None, "[Exec] e1")
        assert ok is False
        launch.assert_not_called()
        bk.is_alive.assert_not_called()


class TestBackgroundLabelLoop:
    """_background_label_loop: the polling logic that runs inside the detached `relay
    _ensure-label` subprocess. Uses injectable clock_fn/sleep_fn so the bounded window is
    exercised without a real ~30s wait."""

    def _fake_clock(self, ticks):
        """ticks: a list of times consumed one per clock_fn() call; the last value repeats once
        exhausted, so a loop that overruns still terminates instead of raising."""
        state = {"i": 0}

        def clock():
            i = min(state["i"], len(ticks) - 1)
            state["i"] += 1
            return ticks[i]
        return clock

    def test_noop_when_already_labeled_two_checks_in_a_row(self, relay):
        with mock.patch.object(relay.iterm_backend, "live_session_names", return_value={"[Exec] e1"}), \
             mock.patch.object(relay.iterm_backend, "title_is_live", return_value=True), \
             mock.patch.object(relay.iterm_backend, "rename_by_id") as rename:
            ok = relay._background_label_loop(
                "h1", "[Exec] e1", window=30, interval=3,
                clock_fn=self._fake_clock([0, 1, 2, 3, 4, 5]), sleep_fn=lambda s: None)
        assert ok is True
        rename.assert_not_called()

    def test_reasserts_until_clobber_fixed_then_holds(self, relay):
        # First two polls see the title clobbered away from [Exec]; from the third poll on it
        # reads [Exec] again (as if the background rename just landed) and must hold for 2 in a row.
        reads = [False, False, True, True, True]
        with mock.patch.object(relay.iterm_backend, "live_session_names", return_value=set()), \
             mock.patch.object(relay.iterm_backend, "title_is_live", side_effect=reads), \
             mock.patch.object(relay.iterm_backend, "rename_by_id") as rename:
            ok = relay._background_label_loop(
                "h1", "[Exec] e1", window=30, interval=3,
                clock_fn=self._fake_clock([0, 1, 2, 3, 4, 5, 6]), sleep_fn=lambda s: None)
        assert ok is True
        assert rename.call_count == 2  # one per clobbered read
        rename.assert_called_with("h1", "[Exec] e1")

    def test_gives_up_after_window_if_never_holds(self, relay):
        with mock.patch.object(relay.iterm_backend, "live_session_names", return_value=set()), \
             mock.patch.object(relay.iterm_backend, "title_is_live", return_value=False), \
             mock.patch.object(relay.iterm_backend, "rename_by_id") as rename:
            ok = relay._background_label_loop(
                "h1", "[Exec] e1", window=10, interval=3,
                clock_fn=self._fake_clock([0, 3, 6, 9, 12]), sleep_fn=lambda s: None)
        assert ok is False
        assert rename.call_count >= 1


class TestCmdEnsureLabel:
    """cmd_ensure_label: the hidden subcommand entry point run inside the detached subprocess --
    must never raise regardless of what _background_label_loop does."""

    def test_delegates_to_background_loop(self, relay):
        with mock.patch.object(relay, "_background_label_loop", return_value=True) as loop:
            relay.cmd_ensure_label(SimpleNamespace(handle="h1", label="[Exec] e1"))
        loop.assert_called_once_with("h1", "[Exec] e1")

    def test_exception_in_loop_is_swallowed(self, relay):
        with mock.patch.object(relay, "_background_label_loop", side_effect=RuntimeError("boom")):
            relay.cmd_ensure_label(SimpleNamespace(handle="h1", label="[Exec] e1"))  # no raise


class TestTombstoneNameReservation:
    """A PAUSED (tombstoned) lead keeps holding its project name; a plain stale GHOST does not.

    These two cases look identical by `last_active` alone — both are old — so they are tested as a
    matched pair. The difference is intent: a tombstone announced it is coming back (resume revives
    it under its own name), a ghost never did. Without the tombstone half, quitting and resuming
    could hand your name to another lead and bring you back as `<project>-2`
    (docs/lead-arming-durability.md §9.2)."""

    NOW = TestUniqueLeadProject.NOW
    STALE = 999999  # far beyond LEAD_LIVE_WINDOW_SECONDS

    def _lead(self, sid, project, offset_seconds, ended=False):
        m = {"session_id": sid, "project": project,
             "last_active": TestUniqueLeadProject._stamp(offset_seconds)}
        if ended:
            m["ended"] = True
        return m

    def test_tombstoned_lead_reserves_its_name_even_when_stale(self, relay):
        leads = [self._lead("paused-1", "claude-relay", self.STALE, ended=True)]
        name, clash = relay.unique_lead_project("claude-relay", "self-1", [], leads, now_ts=self.NOW)
        assert name == "claude-relay-2"
        assert clash == "paused-1"

    def test_plain_stale_ghost_still_releases_its_name(self, relay):
        """The matched negative: same staleness, no tombstone → name reclaimed, no suffix creep."""
        leads = [self._lead("ghost-1", "claude-relay", self.STALE, ended=False)]
        name, clash = relay.unique_lead_project("claude-relay", "self-1", [], leads, now_ts=self.NOW)
        assert (name, clash) == ("claude-relay", None)

    def test_resuming_lead_gets_its_own_name_back(self, relay):
        """Self is always excluded, so the paused lead reviving under its own id keeps the base
        name — the whole point of reserving it."""
        leads = [self._lead("paused-1", "claude-relay", self.STALE, ended=True)]
        name, clash = relay.unique_lead_project("claude-relay", "paused-1", [], leads,
                                                now_ts=self.NOW)
        assert (name, clash) == ("claude-relay", None)
