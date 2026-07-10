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
        p1 = relay.build_packet("Fix the auth bug in login.py", "/tmp/a/001-report.md", "/path/to/relay diff sess-1")
        p2 = relay.build_packet("Implement the new charts grid layout, multi-file", "/tmp/b/002-report.md", "/path/to/relay diff sess-2")

        footer1 = p1.split("---\n(relay")[1]
        footer2 = p2.split("---\n(relay")[1]
        # Strip the two legitimately-varying lines (report path and diff_cmd) before comparing.
        footer1_norm = footer1.replace("/tmp/a/001-report.md", "REPORT_PATH").replace("/path/to/relay diff sess-1", "DIFF_CMD")
        footer2_norm = footer2.replace("/tmp/b/002-report.md", "REPORT_PATH").replace("/path/to/relay diff sess-2", "DIFF_CMD")
        assert footer1_norm == footer2_norm

    def test_footer_contains_required_sections(self, relay):
        p = relay.build_packet("do the thing", "/tmp/x/001-report.md", "/path/to/relay diff sess-x")
        assert "STAGE, NEVER COMMIT" in p
        assert "ONE LOGICAL DELIVERABLE" in p
        assert "/tmp/x/001-report.md" in p
        assert "UNVERIFIED" in p
        assert "VERY FIRST LINE" in p
        assert "relay diff" in p
        assert "sess-x" in p


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
        assert relay.packet_summary("# Fix the three Both-view bugs\n\nDetails...") == "Fix the three Both-view bugs"

    def test_collapses_whitespace_and_skips_blanks(self, relay):
        assert relay.packet_summary("\n\n   ##   Add   the  SMA  toggle  \nmore") == "Add the SMA toggle"

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
        assert cap["label"] == "[E] e1"
        assert s["tab_label"] == "[E] e1"
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
            "busy_since": relay.now(), "owner_lead": owner_lead, "owner_project": owner_project})

    def _args(self, json=False, lead=None, all=False):
        return SimpleNamespace(json=json, lead=lead, all=all)

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
        long_topic = "fix: Both-view bugs (draggable sep, panel default, 1h SMA) extra"  # 60 chars
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
                                      tab_label="[L] alpha")
        with mock.patch.object(relay.iterm, "focus", return_value=True) as focus:
            relay.cmd_focus(SimpleNamespace(session_id="lead-1"))
        focus.assert_called_once_with("[L] alpha")

    def test_lead_with_no_label_errors(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1")  # armed before rename → no label
        with mock.patch.object(relay.iterm, "focus", return_value=False):
            with pytest.raises(SystemExit):
                relay.cmd_focus(SimpleNamespace(session_id="lead-1"))

    def test_unknown_session_errors(self, relay):
        with pytest.raises(SystemExit):
            relay.cmd_focus(SimpleNamespace(session_id="nope"))


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


class TestResumeLead:
    """cmd_resume routing a crashed LEAD (marker present, no session.json) → restores its OWN Claude
    conversation via `claude --resume <sid>`. iterm.spawn is mocked so no real iTerm/claude."""
    def _run(self, relay, sid, force=False):
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)):
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
                                      tab_label="[L] webapp", color=[1, 2, 3])
        cap = self._run(relay, "lead-1")
        assert cap["label"] == "[L] webapp"
        assert cap["tab_color"] == [1, 2, 3]
        m = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert m["tab_label"] == "[L] webapp"
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
