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

    def test_footer_forbids_opening_real_tabs_in_demos(self, relay):
        """An executor verifying its work must not drive the human's real terminal. The bullet
        names the EXACT seam a live packet-002 incident found: term_backend(s) re-resolves the
        backend from session.json, so patching relay.iterm alone silently misses close/send/rename."""
        p = relay.build_packet("do the thing", "/tmp/x/001-report.md",
                               "/path/to/relay diff sess-x", "file:///tmp/x/001-diff.html")
        assert "NEVER OPEN REAL TABS TO VERIFY" in p
        assert "term_backend" in p and "iterm_backend" in p
        assert "Sandbox HOME alone is NOT enough" in p

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

    def test_footer_contains_treat_cold_line(self, relay):
        """#12 (§6e-e4): the GATES text must tell the executor to treat the packet as its ONLY
        context and distrust memory of prior packets/conversations."""
        p = relay.build_packet("do the thing", "/tmp/x/001-report.md", "/path/to/relay diff sess-x", "file:///tmp/x/001-diff.html")
        assert "TREAT THIS PACKET COLD" in p
        assert "trust no memory of prior packets" in p
        assert "say so explicitly in\n  your report" in p or "say so explicitly in your report" in p

    def test_footer_contains_stop_and_report_paragraph(self, relay):
        """#14 (§7-h2, broadened per §9): the GATES text must cover ALL blocking questions, not
        just world-state ones, and must route them through the report (never an interactive
        question in the tab)."""
        p = relay.build_packet("do the thing", "/tmp/x/001-report.md", "/path/to/relay diff sess-x", "file:///tmp/x/001-diff.html")
        assert "STOP AND REPORT, NEVER ASK IN THE TAB" in p
        assert "currently\n  deployed/committed/applied state" in p or "currently deployed/committed/applied state" in p
        assert "any judgement call this packet can't resolve" in p
        assert "never raise\n  an interactive question for it" in p or "never raise an interactive question for it" in p

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


class TestPreconditionsNag:
    """#13/§7-h1: `relay send`/`relay spawn` warn (never block) when an outgoing packet has no
    `## Preconditions` heading — pure grep, same warning-stamp philosophy as ver?/stale-hooks/d4's
    dropped-discipline-marker check. The section itself isn't the point; authoring it forces the
    world-state walk. Covers both send targets since h1 says "a packet" and both cmd_spawn and
    cmd_send write one out — see packet_missing_preconditions's own call sites."""

    def test_missing_section_flagged(self, relay):
        assert relay.packet_missing_preconditions("do the thing\n\n## Deliverable\nfix it") is True

    def test_h2_heading_present(self, relay):
        assert relay.packet_missing_preconditions("intro\n\n## Preconditions\n- clean worktree\n") is False

    def test_h3_heading_present(self, relay):
        assert relay.packet_missing_preconditions("intro\n\n### Preconditions\n- clean worktree\n") is False

    def test_case_insensitive(self, relay):
        assert relay.packet_missing_preconditions("intro\n\n## preconditions\n- x\n") is False

    def test_trailing_text_on_heading_line_tolerated(self, relay):
        # "don't demand byte-exactness for a nag" — annotations after the word still count.
        assert relay.packet_missing_preconditions("intro\n\n## Preconditions (world-state)\n- x\n") is False

    def test_mention_in_prose_does_not_count(self, relay):
        # Only a HEADING satisfies the nag — talking about preconditions in body text doesn't.
        assert relay.packet_missing_preconditions("we should discuss preconditions here\n") is True

    def test_spawn_warns_when_section_absent(self, relay, tmp_path, capsys):
        packet = tmp_path / "p.md"
        packet.write_text("do the thing\n\n## Deliverable\nfix it")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: None), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123):
            relay.cmd_spawn(SimpleNamespace(packet=str(packet), topic="foo", name="no-precond",
                                             worktree=str(worktree), model=None, model_override=None,
                                             skip_perms=None, pane=None, lead=None, scope=None))
        out = capsys.readouterr().out
        assert "Preconditions" in out and "spawning anyway" in out

    def test_spawn_silent_when_section_present(self, relay, tmp_path, capsys):
        packet = tmp_path / "p.md"
        packet.write_text("do the thing\n\n## Preconditions\n- clean worktree\n\n## Deliverable\nfix it")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: None), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123):
            relay.cmd_spawn(SimpleNamespace(packet=str(packet), topic="foo", name="has-precond",
                                             worktree=str(worktree), model=None, model_override=None,
                                             skip_perms=None, pane=None, lead=None, scope=None))
        out = capsys.readouterr().out
        assert "Preconditions" not in out

    def _mk_send_target(self, relay, sid="e1"):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
            "tab_label": "relay-" + sid, "model": None, "pid": os.getpid(),
            "iterm_session": "w0t0p0:OLD", "claude_session": "cs-x", "status": "busy",
            "current_packet": 1, "busy_since": relay.now(), "created": relay.now(),
            "updated": relay.now()})
        (relay.packets_dir(sid) / "001-packet.md").write_text("first packet")
        (relay.packets_dir(sid) / "001-report.md").write_text("done")

    def test_send_warns_when_section_absent(self, relay, tmp_path, capsys):
        self._mk_send_target(relay, "e1")
        packet = tmp_path / "next.md"
        packet.write_text("# Follow-up\n\nDo the next thing.")
        with mock.patch.object(relay.iterm, "send", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=str(packet)))
        out = capsys.readouterr().out
        assert "Preconditions" in out and "sending anyway" in out

    def test_send_silent_when_section_present(self, relay, tmp_path, capsys):
        self._mk_send_target(relay, "e2")
        packet = tmp_path / "next.md"
        packet.write_text("# Follow-up\n\n## Preconditions\n- clean worktree\n\nDo the next thing.")
        with mock.patch.object(relay.iterm, "send", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e2", packet=str(packet)))
        out = capsys.readouterr().out
        assert "Preconditions" not in out


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


class TestSpawnLaunchHonesty:
    """§12 #20 (LIVE INCIDENT, 2026-07-21): a spawn whose launch never happened — no PID captured
    AND the tab label never took — used to write a plain `status: busy` marker, so `relay list`
    showed a live executor working on a packet nothing had ever been delivered to. Both signals
    failing now retries the launch once and, if that fails too, records LAUNCH_FAILED and points at
    `relay restart`. Either signal alone is a degraded-but-live tab and must be UNAFFECTED."""

    def _spawn(self, relay, tmp_path, pids, labels, expect_exit=False):
        """Drive cmd_spawn with per-ATTEMPT launch signals: `pids`/`labels` are the values read_pid
        and _ensure_tab_label return on attempt 1, attempt 2, … Returns (spawn_call_count, session).
        `expect_exit` asserts the non-zero exit a launch-failed spawn owes a scripted caller."""
        pkt = tmp_path / "p.md"
        pkt.write_text("do the thing")
        calls = []
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: calls.append(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", side_effect=list(pids)), \
             mock.patch.object(relay, "read_iterm_id", return_value=None), \
             mock.patch.object(relay, "_ensure_tab_label", side_effect=list(labels)), \
             mock.patch.object(relay, "pid_start_time", return_value=None):
            run = lambda: relay.cmd_spawn(SimpleNamespace(  # noqa: E731
                worktree=str(tmp_path), topic="t", packet=str(pkt), model=None, name="e1",
                scope=None, skip_perms=None, pane=None, lead=None, model_override=None))
            if expect_exit:
                with pytest.raises(SystemExit) as ei:
                    run()
                assert ei.value.code == 1
            else:
                run()
        return len(calls), relay.read_session("e1")

    def _ledger_events(self, relay):
        return [json.loads(l) for l in relay.LEDGER.read_text().splitlines()] if relay.LEDGER.exists() else []

    def test_both_signals_fail_retries_once_then_marks_launch_failed(self, relay, tmp_path, capsys):
        n, s = self._spawn(relay, tmp_path, pids=[None, None], labels=[False, False], expect_exit=True)
        assert n == 2                                    # retried the launch exactly once
        assert s["status"] == relay.LAUNCH_FAILED        # NOT busy — nothing is working
        assert s["pid"] is None
        err = capsys.readouterr().err
        assert "did NOT launch" in err and f"relay restart e1" in err
        assert any(e["event"] == "launch_failed" and e["session_id"] == "e1"
                   for e in self._ledger_events(relay))

    def test_retry_that_succeeds_is_a_normal_busy_spawn(self, relay, tmp_path, capsys):
        n, s = self._spawn(relay, tmp_path, pids=[None, 4242], labels=[False, True])
        assert n == 2                                    # first attempt failed, retry launched
        assert s["status"] == "busy" and s["pid"] == 4242
        out = capsys.readouterr().out
        assert "spawned session 'e1'" in out
        assert not any(e["event"] == "launch_failed" for e in self._ledger_events(relay))

    def test_pid_only_failure_still_busy_and_not_retried(self, relay, tmp_path):
        # The label took → the tab is real, just missing its pidfile. Pre-existing degraded-but-live
        # behavior (a warning), NOT a failed launch — the over-correction this must not become.
        n, s = self._spawn(relay, tmp_path, pids=[None], labels=[True])
        assert n == 1 and s["status"] == "busy" and s["pid"] is None

    def test_label_only_failure_still_busy_and_not_retried(self, relay, tmp_path):
        n, s = self._spawn(relay, tmp_path, pids=[999], labels=[False])
        assert n == 1 and s["status"] == "busy" and s["pid"] == 999

    def test_healthy_spawn_unaffected(self, relay, tmp_path, capsys):
        n, s = self._spawn(relay, tmp_path, pids=[123], labels=[True])
        assert n == 1 and s["status"] == "busy" and s["pid"] == 123
        assert "spawned session 'e1'" in capsys.readouterr().out

    def test_launch_failed_is_sticky_through_check(self, relay, tmp_path):
        # _check_one must not relabel it `dead` — that reads as "it ran and then died" and loses the
        # one fact that matters (the pinned conversation id was never registered).
        self._spawn(relay, tmp_path, pids=[None, None], labels=[False, False], expect_exit=True)
        assert relay._check_one("e1")["status"] == relay.LAUNCH_FAILED

    def test_status_renders_untruncated_in_list(self, relay, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        self._spawn(relay, tmp_path, pids=[None, None], labels=[False, False], expect_exit=True)
        capsys.readouterr()
        relay.cmd_list(SimpleNamespace(lead=None, all=True, json=False, closed=False))
        out = capsys.readouterr().out
        assert relay.LAUNCH_FAILED in out                  # not clipped to "launch-fai"
        # …and it isn't listed as a wake-orphan: nothing is running to lose a wake, and the orphan
        # line's "just relay send/resume" advice is exactly what this session can't do.
        assert "owned by retired leads" not in out

    def test_send_refuses_launch_failed_session(self, relay, tmp_path):
        # Neither delivery path can work: no tab to type into, no conversation to resume.
        self._spawn(relay, tmp_path, pids=[None, None], labels=[False, False], expect_exit=True)
        nxt = tmp_path / "next.md"
        nxt.write_text("more work")
        with pytest.raises(SystemExit) as ei:
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=str(nxt)))
        assert relay.LAUNCH_FAILED in str(ei.value) and "relay restart e1" in str(ei.value)


class TestResumeLaunchHonesty:
    """§12 #21 (same incident): `claude --resume <uuid>` against an id Claude Code never registered
    exits instantly, yet resume printed "resumed … pid N" for a pid that was already gone — and did
    so identically forever, since that id can never become valid. Resume now refuses up front on a
    known-failed launch, and verifies the relaunched process survives a grace window before
    claiming success."""

    def _mk(self, relay, sid="e1", status="dead", claude_session="uuid-x"):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
            "tab_label": "[Exec] e1", "model": None, "pid": 999, "iterm_session": "w0t0p0:OLD",
            "claude_session": claude_session, "status": status, "current_packet": 1,
            "busy_since": relay.now(), "created": relay.now(), "updated": relay.now()})
        (relay.packets_dir(sid) / "001-packet.md").write_text("do the work")

    def _resume(self, relay, sid="e1", new_pid=123, new_pid_alive=True, force=False):
        calls = []
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: calls.append(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=new_pid), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True), \
             mock.patch.object(relay, "pid_start_time", return_value=None), \
             mock.patch.object(relay, "LAUNCH_GRACE_SECONDS", 0), \
             mock.patch.object(relay, "conversation_transcript_exists", return_value=None), \
             mock.patch.object(relay, "pid_alive", side_effect=lambda p: new_pid_alive if p == new_pid else False):
            relay.cmd_resume(SimpleNamespace(session_id=sid, force=force))
        return calls

    def test_refuses_resume_of_a_launch_that_never_happened(self, relay):
        self._mk(relay, status=relay.LAUNCH_FAILED)
        with pytest.raises(SystemExit) as ei:
            self._resume(relay)
        msg = str(ei.value)
        assert "never" in msg and "relay restart e1" in msg
        assert relay.read_session("e1")["status"] == relay.LAUNCH_FAILED  # untouched

    def test_launch_failed_refusal_does_not_launch_anything(self, relay):
        self._mk(relay, status=relay.LAUNCH_FAILED)
        calls = []
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: calls.append(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "pid_alive", return_value=False):
            with pytest.raises(SystemExit):
                relay.cmd_resume(SimpleNamespace(session_id="e1", force=False))
        assert not calls

    def test_force_overrides_the_launch_failed_refusal(self, relay):
        self._mk(relay, status=relay.LAUNCH_FAILED, claude_session="uuid-keep")
        calls = self._resume(relay, force=True)
        assert calls and calls[0]["resume_id"] == "uuid-keep"
        assert relay.read_session("e1")["status"] == "busy"

    def test_dead_pid_after_relaunch_is_reported_as_failure_not_success(self, relay, capsys):
        self._mk(relay)
        with pytest.raises(SystemExit) as ei:
            self._resume(relay, new_pid=4242, new_pid_alive=False)
        msg = str(ei.value)
        assert "FAILED" in msg and "4242 is already gone" in msg and "relay restart e1" in msg
        assert "resumed 'e1'" not in capsys.readouterr().out      # never claimed success
        assert relay.read_session("e1")["status"] == relay.LAUNCH_FAILED

    def test_missing_pid_after_relaunch_is_a_failed_launch(self, relay):
        self._mk(relay)
        with pytest.raises(SystemExit) as ei:
            self._resume(relay, new_pid=None)
        assert "no PID was ever written" in str(ei.value)
        assert relay.read_session("e1")["status"] == relay.LAUNCH_FAILED

    def test_healthy_resume_unaffected(self, relay, capsys):
        self._mk(relay, claude_session="uuid-keep")
        calls = self._resume(relay)
        assert calls[0]["resume_id"] == "uuid-keep"
        assert "resumed 'e1'" in capsys.readouterr().out
        s = relay.read_session("e1")
        assert s["status"] == "busy" and s["pid"] == 123

    def test_second_resume_after_a_failed_one_is_refused_not_looped(self, relay):
        # The loop-forever half of #21: the first resume's failure marks the session, so the next
        # one stops instead of relaunching into the identical wall.
        self._mk(relay)
        with pytest.raises(SystemExit):
            self._resume(relay, new_pid=4242, new_pid_alive=False)
        with pytest.raises(SystemExit) as ei:
            self._resume(relay)
        assert "relay restart e1" in str(ei.value)

    def test_missing_transcript_warns_but_does_not_block(self, relay, capsys):
        self._mk(relay)
        with mock.patch.object(relay, "conversation_transcript_exists", return_value=False):
            calls = self._resume2(relay)
        assert calls                                              # still resumed
        assert "no Claude Code transcript exists" in capsys.readouterr().err

    def test_present_transcript_is_silent(self, relay, capsys):
        self._mk(relay)
        with mock.patch.object(relay, "conversation_transcript_exists", return_value=True):
            self._resume2(relay)
        assert "no Claude Code transcript" not in capsys.readouterr().err

    def _resume2(self, relay, sid="e1"):
        """_resume without its own conversation_transcript_exists patch, so a caller can set one."""
        calls = []
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: calls.append(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True), \
             mock.patch.object(relay, "pid_start_time", return_value=None), \
             mock.patch.object(relay, "LAUNCH_GRACE_SECONDS", 0), \
             mock.patch.object(relay, "pid_alive", side_effect=lambda p: p == 123):
            relay.cmd_resume(SimpleNamespace(session_id=sid, force=False))
        return calls


class TestLaunchSurvived:
    """_launch_survived: the shell writes the pidfile BEFORE exec'ing claude, so a just-read pid
    proves only that the shell ran. The probe must return the moment the pid is gone (the failure a
    human is waiting on) and only wait out the full window for a genuinely healthy launch."""

    def test_none_pid_is_a_failed_launch(self, relay):
        assert relay._launch_survived(None, grace=0) is False

    def test_dead_pid_returns_immediately_without_sleeping(self, relay):
        slept = []
        with mock.patch.object(relay, "pid_alive", return_value=False):
            ok = relay._launch_survived(4242, grace=30, sleep_fn=slept.append, clock_fn=lambda: 0.0)
        assert ok is False and slept == []          # no waiting out the window on a dead pid

    def test_pid_that_dies_mid_window_fails(self, relay):
        alive = iter([True, True, False])
        ticks = iter([0.0, 0.5, 1.0, 1.5, 2.0])
        with mock.patch.object(relay, "pid_alive", side_effect=lambda p: next(alive)):
            ok = relay._launch_survived(1, grace=3, sleep_fn=lambda _: None,
                                        clock_fn=lambda: next(ticks))
        assert ok is False

    def test_pid_alive_through_the_window_survives(self, relay):
        ticks = iter([0.0, 0.5, 1.0, 5.0])
        with mock.patch.object(relay, "pid_alive", return_value=True):
            ok = relay._launch_survived(1, grace=3, sleep_fn=lambda _: None,
                                        clock_fn=lambda: next(ticks))
        assert ok is True


class TestConversationTranscriptExists:
    """The pre-flight 'was this conversation ever created' probe: Claude Code writes
    <config-dir>/projects/<cwd-slug>/<uuid>.jsonl. Unknowable cases must return None, not False —
    a false 'never created' would refuse a perfectly good resume."""

    def _root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        root = tmp_path / "cfg" / "projects"
        root.mkdir(parents=True)
        return root

    def test_finds_transcript_in_any_project_dir(self, relay, tmp_path, monkeypatch):
        root = self._root(tmp_path, monkeypatch)
        (root / "-some-other-path").mkdir()
        (root / "-w").mkdir()
        (root / "-w" / "uuid-x.jsonl").write_text("{}")
        assert relay.conversation_transcript_exists("uuid-x") is True

    def test_absent_transcript_is_false(self, relay, tmp_path, monkeypatch):
        root = self._root(tmp_path, monkeypatch)
        (root / "-w").mkdir()
        assert relay.conversation_transcript_exists("uuid-x") is False

    def test_no_projects_root_is_unknown(self, relay, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
        assert relay.conversation_transcript_exists("uuid-x") is None

    def test_empty_projects_root_is_unknown(self, relay, tmp_path, monkeypatch):
        self._root(tmp_path, monkeypatch)
        assert relay.conversation_transcript_exists("uuid-x") is None


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
        # Leads now get a stable relay-controlled tab label at /relay:mode → focusable like
        # executors. The handle goes with it (backlog §2): this marker has none, so it's None here
        # — the label-only fallback — while a marker WITH one threads it through (see
        # TestLeadTabIdentityAddressing.test_focus_lead_passes_handle_to_the_recorded_backend).
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="alpha",
                                      tab_label="[Lead] alpha")
        with mock.patch.object(relay.iterm, "focus", return_value=True) as focus:
            relay.cmd_focus(SimpleNamespace(session_id="lead-1"))
        focus.assert_called_once_with("[Lead] alpha", None)

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
    def _mk(self, relay, sid="e1", pid=12345, handle=None):
        relay.session_dir(sid).mkdir(parents=True)
        relay.write_session(sid, {"session_id": sid, "status": "busy",
            "tab_label": "relay-e1", "pid": pid, "current_packet": 1, "topic": "t",
            "worktree": "/w", "busy_since": relay.now(), "iterm_session": handle})

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

    def test_lingering_tab_is_retitled_closed(self, relay):
        """#4 generalization: a tab that outlives its session says so. Only the branch that leaves
        one standing retitles — a tab that actually closed has nothing to rename."""
        self._mk(relay, handle="w0t0p0:E1")
        with mock.patch.object(relay.iterm, "close", return_value=False), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True), \
             mock.patch.object(relay.iterm, "rename_by_id", return_value=True) as rn, \
             mock.patch.object(relay, "pid_alive", side_effect=[True, False]), \
             mock.patch.object(relay.os, "kill"), \
             mock.patch.object(relay.time, "sleep"):
            relay.cmd_close(self._args())
        rn.assert_called_once_with("w0t0p0:E1", "[closed] e1")   # by HANDLE, never by label
        assert relay.read_session("e1")["tab_label"] == "[closed] e1"   # persisted after the retitle

    def test_a_closed_tab_is_not_retitled(self, relay):
        self._mk(relay, handle="w0t0p0:E1")
        with mock.patch.object(relay.iterm, "close", return_value=True), \
             mock.patch.object(relay.iterm, "rename_by_id") as rn, \
             mock.patch.object(relay, "pid_alive", side_effect=[True, False]), \
             mock.patch.object(relay.os, "kill"), \
             mock.patch.object(relay.time, "sleep"):
            relay.cmd_close(self._args())
        rn.assert_not_called()
        assert relay.read_session("e1")["tab_label"] == "relay-e1"      # untouched

    def test_retitle_failure_never_fails_the_close(self, relay):
        self._mk(relay, handle="w0t0p0:E1")
        with mock.patch.object(relay.iterm, "close", return_value=False), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True), \
             mock.patch.object(relay.iterm, "rename_by_id", side_effect=OSError("boom")), \
             mock.patch.object(relay, "pid_alive", side_effect=[True, False]), \
             mock.patch.object(relay.os, "kill"), \
             mock.patch.object(relay.time, "sleep"):
            relay.cmd_close(self._args())                               # must not raise
        assert relay.read_session("e1")["status"] == "closed"
        assert relay.read_session("e1")["tab_label"] == "relay-e1"

    def test_keep_tab_retitles_the_still_live_tab(self, relay):
        self._mk(relay, handle="w0t0p0:E1")
        with mock.patch.object(relay.iterm, "rename_by_id", return_value=True) as rn, \
             mock.patch.object(relay, "pid_alive", return_value=True):
            relay.cmd_close(self._args(keep_tab=True))
        rn.assert_called_once_with("w0t0p0:E1", "[closed] e1")
        assert relay.read_session("e1")["tab_label"] == "[closed] e1"

    def test_a_handle_less_session_is_never_retitled_by_label(self, relay):
        """No recorded handle → no rename at all. Falling back to a label match is exactly the #2
        defect (a label can name two live tabs), so a legacy record degrades to a silent no-op."""
        self._mk(relay, handle=None)   # legacy record, spawned before handle capture
        with mock.patch.object(relay.iterm, "close", return_value=False), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True), \
             mock.patch.object(relay.iterm, "rename_by_id") as rn, \
             mock.patch.object(relay, "pid_alive", side_effect=[True, False]), \
             mock.patch.object(relay.os, "kill"), \
             mock.patch.object(relay.time, "sleep"):
            relay.cmd_close(self._args())          # must not raise
        rn.assert_not_called()
        assert relay.read_session("e1")["status"] == "closed"
        assert relay.read_session("e1")["tab_label"] == "relay-e1"

    def test_keep_tab_leaves_tab_and_process(self, relay):
        self._mk(relay)
        with mock.patch.object(relay.iterm, "close") as close, \
             mock.patch.object(relay, "pid_alive", return_value=True), \
             mock.patch.object(relay.os, "kill") as kill:
            relay.cmd_close(self._args(keep_tab=True))
        assert relay.read_session("e1")["status"] == "closed"
        close.assert_not_called()
        kill.assert_not_called()


REPORT_001 = """Export button ships behind the existing exporter; 4 tests, suite green, staged.

Status: clean
Risk flags: none
UNVERIFIED: the Safari download path — only tested in Chrome.
Changed: toolbar + exporter wiring.

## What changed
- src/toolbar.tsx:88 — new button
"""


class TestReportTldr:
    """_report_tldr lifts exactly the lead-facing fields out of a report: the first-line outcome
    plus the three mandated TL;DR lines. Everything else in a report is detail the seed links to."""

    def test_extracts_outcome_and_tldr_fields(self, relay):
        t = relay._report_tldr(REPORT_001)
        assert t["outcome"] == "Export button ships behind the existing exporter; 4 tests, suite green, staged."
        assert t["status"] == "clean"
        assert t["risk"] == "none"
        assert t["unverified"] == "the Safari download path — only tested in Chrome."

    def test_missing_fields_are_none_not_an_error(self, relay):
        # a malformed report (no TL;DR block at all) must degrade to a thinner entry, never raise
        t = relay._report_tldr("it worked I think\n\nsome prose\n")
        assert t["outcome"] == "it worked I think"
        assert (t["status"], t["risk"], t["unverified"]) == (None, None, None)

    def test_bulleted_tldr_lines_are_read(self, relay):
        # real reports sometimes bullet the TL;DR block; the leading marker must not hide the field
        t = relay._report_tldr("done\n\n- Status: partial\n- UNVERIFIED: none\n")
        assert t["status"] == "partial" and t["unverified"] == "none"

    def test_first_match_wins_over_later_prose(self, relay):
        # the TL;DR block is authoritative; a later section repeating "Status:" must not overwrite it
        t = relay._report_tldr("done\nStatus: clean\n\n## Detail\nStatus: not really\n")
        assert t["status"] == "clean"


class TestSuccessorSeed:
    """`relay retire` (backlog §6e-e3): the seed is derived from what is already on disk — the
    session's packets and their reports — so retiring costs nothing from the session being retired
    (and works on a dead/stalled one, which is exactly when it's needed most)."""

    def _mk(self, relay, sid="e1", packets=(("001", REPORT_001), ("002", None)), status="reported"):
        d = relay.packets_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        for n, report in packets:
            (d / f"{n}-packet.md").write_text(f"Task {n} — do the {n} thing.\n\nmore detail\n")
            if report is not None:
                (d / f"{n}-report.md").write_text(report)
        relay.write_session(sid, {"session_id": sid, "status": status, "tab_label": "[Exec] e1",
            "pid": None, "current_packet": int(packets[-1][0]) if packets else 1, "topic": "csv export",
            "scope": "export", "worktree": "/w", "model": "sonnet", "superseded_by": None,
            "busy_since": relay.now()})

    def _args(self, **over):
        base = dict(session_id="e1", force=False, keep_tab=True)
        base.update(over)
        return SimpleNamespace(**base)

    def _retire(self, relay, **over):
        with mock.patch.object(relay.iterm, "close", return_value=True), \
             mock.patch.object(relay, "pid_alive", return_value=False), \
             mock.patch.object(relay.time, "sleep"):
            relay.cmd_retire(self._args(**over))
        return (relay.session_dir("e1") / relay.SEED_FILENAME).read_text()

    # ── the seed's content contract ──────────────────────────────────────────
    def test_entries_are_ordered_and_flag_the_unreported_one(self, relay):
        self._mk(relay)
        entries = relay.seed_entries("e1")
        assert [e["n"] for e in entries] == ["001", "002"]
        assert entries[0]["tldr"]["status"] == "clean"
        assert entries[1]["tldr"] is None                     # 002 was never reported
        assert entries[1]["report_path"] is None

    def test_seed_has_every_contract_section(self, relay):
        self._mk(relay)
        seed = self._retire(relay)
        for section in ("# Successor seed — e1", "## Territory", "## Packet index", "## Inherit with care"):
            assert section in seed

    def test_seed_carries_the_reported_packets_summaries(self, relay):
        self._mk(relay)
        seed = self._retire(relay)
        assert "### 001 — Task 001 — do the 001 thing." in seed
        assert "Export button ships behind the existing exporter" in seed   # outcome
        assert "- Status: clean" in seed
        assert "the Safari download path" in seed                            # the gotcha survives

    def test_seed_flags_the_unreported_packet_as_no_report(self, relay):
        self._mk(relay)
        seed = self._retire(relay)
        assert "### 002" in seed and "NO REPORT" in seed
        assert "Packets worked: 2 (1 reported, 1 unreported)" in seed

    def test_seed_records_the_inherited_territory(self, relay):
        self._mk(relay)
        seed = self._retire(relay)
        assert "- Worktree: /w" in seed and "- Topic: csv export" in seed and "- Model: sonnet" in seed

    def test_seed_is_not_a_transcript_dump(self, relay):
        # the guarantee that makes the seed cheap to read: report DETAIL stays behind a link
        self._mk(relay)
        seed = self._retire(relay)
        assert "src/toolbar.tsx:88" not in seed and "## What changed" not in seed
        assert str(relay.packets_dir("e1") / "001-report.md") in seed      # linked, not inlined

    def test_seed_survives_a_session_with_no_packets(self, relay):
        self._mk(relay, packets=())
        seed = self._retire(relay)
        assert "Packets worked: 0" in seed and "there is no" in seed

    # ── retire as a close variant ────────────────────────────────────────────
    def test_retire_closes_as_superseded_by_seed(self, relay):
        self._mk(relay)
        self._retire(relay)
        s = relay.read_session("e1")
        assert s["status"] == "superseded"
        assert s["superseded_by"] == relay.SEED_RETIRED_BY_SEED

    def test_retire_closes_the_tab_like_close_does(self, relay):
        self._mk(relay)
        with mock.patch.object(relay.iterm, "close", return_value=True) as close, \
             mock.patch.object(relay, "pid_alive", return_value=False), \
             mock.patch.object(relay.time, "sleep"):
            relay.cmd_retire(self._args(keep_tab=False))
        close.assert_called_once()

    def test_retire_is_ledgered_with_its_seed(self, relay):
        self._mk(relay)
        self._retire(relay)
        events = [json.loads(l) for l in (relay.STATE_ROOT / "sessions.jsonl").read_text().splitlines()]
        retired = [e for e in events if e["event"] == "retired"]
        assert len(retired) == 1 and retired[0]["packets"] == 2 and retired[0]["forced"] is False
        assert retired[0]["seed"].endswith(relay.SEED_FILENAME)

    # ── refusals ─────────────────────────────────────────────────────────────
    def test_refuses_unknown_session(self, relay):
        with pytest.raises(SystemExit) as ei:
            relay.cmd_retire(self._args(session_id="ghost"))
        assert "no such session: ghost" in str(ei.value)

    def test_refuses_busy_session_with_an_unreported_packet(self, relay):
        # retiring mid-flight kills that packet unreported — the seed can't summarise what was
        # never written, so this is the last moment the lead can choose to wait instead.
        self._mk(relay, packets=(("001", REPORT_001), ("002", None)), status="busy")
        with pytest.raises(SystemExit) as ei:
            relay.cmd_retire(self._args())
        assert "still busy on packet 002" in str(ei.value) and "--force" in str(ei.value)
        assert not (relay.session_dir("e1") / relay.SEED_FILENAME).exists()   # refused = no seed
        assert relay.read_session("e1")["status"] == "busy"                   # and not closed

    def test_force_retires_a_busy_session_and_seeds_it_as_no_report(self, relay):
        self._mk(relay, packets=(("001", REPORT_001), ("002", None)), status="busy")
        seed = self._retire(relay, force=True)
        assert "NO REPORT" in seed
        assert relay.read_session("e1")["status"] == "superseded"

    def test_busy_but_already_reported_needs_no_force(self, relay):
        # busy alone isn't the problem — an unreported CURRENT packet is. A session that reported
        # and is idling (still stamped busy until the next check) retires without ceremony.
        self._mk(relay, packets=(("001", REPORT_001),), status="busy")
        seed = self._retire(relay)
        assert "NO REPORT" not in seed

    def test_refuses_double_retire(self, relay):
        self._mk(relay)
        self._retire(relay)
        with pytest.raises(SystemExit) as ei:
            relay.cmd_retire(self._args())
        assert "already retired" in str(ei.value) and relay.SEED_FILENAME in str(ei.value)


class TestSpawnSeed:
    """`relay spawn --seed`: how a fresh session consumes a retired one's seed. The seed is
    inherited CONTEXT appended to the packet body — never a replacement for the task, and never
    ahead of the GATES footer."""

    def _spawn(self, relay, tmp_path, seed, sid="new-1"):
        pkt = tmp_path / "packet.md"; pkt.write_text("Finish the export work.")
        with mock.patch.object(relay.iterm, "spawn"), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123):
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path), topic="t", packet=str(pkt),
                model=None, name=sid, scope=None, skip_perms=None, pane=None, seed=seed))
        return (relay.packets_dir(sid) / "001-packet.md").read_text()

    def _retired(self, relay, sid="old-1"):
        """A retired session on disk, returning the path of its seed."""
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        (relay.packets_dir(sid) / "001-packet.md").write_text("Task 001 — do the 001 thing.")
        (relay.packets_dir(sid) / "001-report.md").write_text(REPORT_001)
        relay.write_session(sid, {"session_id": sid, "status": "reported", "tab_label": "l",
            "pid": None, "current_packet": 1, "topic": "csv export", "scope": "export",
            "worktree": "/w", "model": "sonnet", "superseded_by": None, "busy_since": relay.now()})
        with mock.patch.object(relay.iterm, "close", return_value=True), \
             mock.patch.object(relay, "pid_alive", return_value=False), \
             mock.patch.object(relay.time, "sleep"):
            relay.cmd_retire(SimpleNamespace(session_id=sid, force=False, keep_tab=True))
        return relay.session_dir(sid) / relay.SEED_FILENAME

    def test_seed_by_path_lands_in_the_packet(self, relay, tmp_path):
        seed_path = self._retired(relay)
        packet = self._spawn(relay, tmp_path, str(seed_path))
        assert "# Successor seed — old-1" in packet
        assert "Export button ships behind the existing exporter" in packet

    def test_seed_by_retired_session_id_resolves(self, relay, tmp_path):
        # the ergonomic form: `--seed old-1`, exactly what the retire message tells you to run
        self._retired(relay)
        assert "# Successor seed — old-1" in self._spawn(relay, tmp_path, "old-1")

    def test_task_comes_first_and_gates_come_last(self, relay, tmp_path):
        # ordering is load-bearing: the seed must never sit between the executor and its GATES
        self._retired(relay)
        packet = self._spawn(relay, tmp_path, "old-1")
        assert packet.index("Finish the export work.") < packet.index("INHERITED CONTEXT") \
            < packet.index("# Successor seed") < packet.index("GATES") < packet.index("REPORT FORMAT")

    def test_seed_is_framed_as_context_not_instructions(self, relay, tmp_path):
        self._retired(relay)
        packet = self._spawn(relay, tmp_path, "old-1")
        assert "not part of your task" in packet and "not a transcript and not" in packet

    def test_unseeded_spawn_is_byte_identical_to_before(self, relay, tmp_path):
        # --seed is strictly additive: omitting it must change nothing about a normal packet
        with_none = self._spawn(relay, tmp_path, None, sid="a")
        assert "INHERITED CONTEXT" not in with_none
        assert with_none == relay.build_packet(
            "Finish the export work.", str(relay.packets_dir("a") / "001-report.md"),
            f"{relay.RELAY_BIN} diff a", relay.path_to_file_url(relay.packets_dir("a") / "001-diff.html"))

    def test_refuses_a_seed_that_does_not_exist(self, relay, tmp_path):
        with pytest.raises(SystemExit) as ei:
            self._spawn(relay, tmp_path, "no-such-session")
        assert "neither a readable file nor a retired session" in str(ei.value)

    def test_a_bad_seed_refuses_before_any_session_is_created(self, relay, tmp_path):
        # fail before the tab/session exist, so a typo'd --seed leaves no half-spawned wreckage
        with pytest.raises(SystemExit):
            self._spawn(relay, tmp_path, "no-such-session", sid="ghost")
        assert relay.read_session("ghost") is None


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
        # pid_alive is per-pid, not a constant: `alive` answers for the session's OLD pid (999, the
        # already-dead-or-not process the restart/resume guard asks about), while the NEW pid the
        # relaunch reads back (123) is alive — a healthy relaunch. A blanket False would also mean
        # "the process we just launched is already gone", i.e. the §12 #21 failure, which is a
        # different scenario and has its own tests (TestResumeLaunchHonesty).
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True), \
             mock.patch.object(relay, "LAUNCH_GRACE_SECONDS", 0), \
             mock.patch.object(relay, "pid_alive", side_effect=lambda p: True if p == 123 else alive):
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

    # ---- arm-time field stamping (§1) ---------------------------------------------------------
    # write_marker rewrites the WHOLE marker, so any field cmd_resume_lead omits is DROPPED from a
    # restored lead. These pin each field's intended re-stamp/preserve/reset semantics.

    def test_lead_resume_records_backend(self, relay):
        # §1: a restored lead used to carry no `backend` at all, so every nudge to it fell back to
        # _probe_backend_for_tab (or, on 0/2+ matches, the caller's ambient guess — Defect A).
        # cmd_resume_lead opened the new tab itself, so it can record this rather than guess.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w")
        self._run(relay, "lead-1")
        marker = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert marker["backend"] == relay.iterm.NAME
        assert marker["backend"] in ("iterm", "terminal")

    def test_lead_resume_restamps_stale_backend(self, relay):
        # Re-stamped, not preserved: the restore spawns the tab under THIS invocation's backend,
        # which may differ from whatever the crashed instance ran under (§1's open question).
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w",
                                      backend="stale-backend")
        self._run(relay, "lead-1")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")["backend"] == relay.iterm.NAME

    def test_lead_resume_preserves_started(self, relay):
        # `started` means "when this lead began" and a restore is the SAME lead — it used to be
        # silently reset to the restore time.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w",
                                      started="2020-01-01T00:00:00")
        self._run(relay, "lead-1")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")["started"] == "2020-01-01T00:00:00"

    def test_lead_resume_preserves_predecessor(self, relay):
        # Dropping this stranded the handoff-zombie tab permanently: the successor's marker is the
        # ONLY record of it, and `relay close-predecessor` reads it from there.
        pred = {"session_id": "old-lead", "tab_label": "[Lead] p", "iterm_session": "w1t1p0:OLD"}
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w",
                                      predecessor=pred)
        self._run(relay, "lead-1")
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")["predecessor"] == pred

    def test_lead_resume_resets_autonomous_posture(self, relay):
        # §6f: the posture is per-session and opt-in-each-time — never preserved across an arm. A
        # lead that had `relay auto on` comes back in the safe wait-for-human posture.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="p", cwd="/w",
                                      autonomous=True, autonomous_source="command")
        self._run(relay, "lead-1")
        marker = relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1")
        assert marker["autonomous"] is False
        assert marker["autonomous_source"] == "config"

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
             mock.patch.object(relay, "LAUNCH_GRACE_SECONDS", 0), \
             mock.patch.object(relay, "pid_alive", side_effect=lambda p: p == 123):
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


class TestWhenIdleQueue:
    """`relay send --when-idle` (task #18): queue a packet for a busy executor instead of refusing,
    and deliver it on the session's own idle transition — replacing the shell `until relay check`
    loops leads were hand-rolling. iterm.send/is_alive are mocked; no real iTerm is touched."""

    def _mk(self, relay, sid="e1", status="busy", pid=None, report=False):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
            "tab_label": "relay-e1", "model": None, "pid": pid if pid is not None else os.getpid(),
            "iterm_session": "w0t0p0:OLD", "claude_session": "cs-x", "status": status,
            "current_packet": 1, "busy_since": relay.now(), "created": relay.now(),
            "updated": relay.now()})
        (relay.packets_dir(sid) / "001-packet.md").write_text("first packet")
        if report:
            (relay.packets_dir(sid) / "001-report.md").write_text("done")

    def _packet(self, relay, tmp_path, text="# Follow-up\n\nDo the next thing.", name="next.md"):
        p = tmp_path / name
        p.write_text(text)
        return str(p)

    def _queue(self, relay, tmp_path, sid="e1", **kw):
        """A --when-idle send into a busy session (the queueing path)."""
        with mock.patch.object(relay.iterm, "send", return_value=True) as send, \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id=sid, when_idle=True,
                                           packet=self._packet(relay, tmp_path, **kw)))
        return send

    def _deliver(self, relay, sid="e1", trigger="test"):
        with mock.patch.object(relay.iterm, "send", return_value=True) as send, \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            item = relay.deliver_queued(sid, trigger=trigger)
        return item, send

    def _events(self, relay, name):
        f = relay.STATE_ROOT / "sessions.jsonl"
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text().splitlines() if json.loads(l)["event"] == name]

    # ── queueing instead of refusing ─────────────────────────────────────────
    def test_busy_session_queues_instead_of_refusing(self, relay, tmp_path):
        self._mk(relay, status="busy", report=False)          # genuinely mid-turn
        send = self._queue(relay, tmp_path)
        send.assert_not_called()                              # NOT injected mid-turn
        q = relay.read_queue("e1")
        assert len(q) == 1 and q[0]["id"] == 1
        assert relay.read_session("e1")["current_packet"] == 1  # no packet delivered
        assert not (relay.packets_dir("e1") / "002-packet.md").exists()

    def test_queueing_is_ledgered(self, relay, tmp_path):
        self._mk(relay)
        self._queue(relay, tmp_path)
        assert [e["queue_id"] for e in self._events(relay, "packet_queued")] == [1]

    def test_stalled_session_also_queues(self, relay, tmp_path):
        self._mk(relay, status="stalled", report=False)
        self._queue(relay, tmp_path)
        assert len(relay.read_queue("e1")) == 1

    def test_when_idle_on_an_already_idle_session_sends_now(self, relay, tmp_path):
        # --when-idle is "queue IF you must", not "always queue" — an idle session takes the packet
        # immediately, and nothing is left queued.
        self._mk(relay, status="busy", report=True)           # report on disk → refreshes to reported
        send = self._queue(relay, tmp_path)
        send.assert_called_once()
        assert relay.read_queue("e1") == []
        assert relay.read_session("e1")["current_packet"] == 2

    def test_queued_body_is_captured_at_queue_time(self, relay, tmp_path):
        # the source .md may be edited or deleted between queueing and delivery
        self._mk(relay)
        src = self._packet(relay, tmp_path, text="# Original\n\nthe queued work")
        with mock.patch.object(relay.iterm, "send", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=src, when_idle=True))
        Path(src).unlink()                                    # source gone before delivery
        (relay.packets_dir("e1") / "001-report.md").write_text("done")
        item, send = self._deliver(relay)
        assert item is not None
        assert "the queued work" in (relay.packets_dir("e1") / "002-packet.md").read_text()

    # ── delivery on the idle transition ──────────────────────────────────────
    def test_no_delivery_while_still_busy(self, relay, tmp_path):
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        item, send = self._deliver(relay)
        assert item is None                                   # this is the whole point
        send.assert_not_called()
        assert len(relay.read_queue("e1")) == 1                # still queued

    def test_delivers_once_the_session_reports(self, relay, tmp_path):
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        (relay.packets_dir("e1") / "001-report.md").write_text("done")   # the real transition
        item, send = self._deliver(relay, trigger="stop-hook")
        assert item is not None
        send.assert_called_once()
        assert relay.read_queue("e1") == []
        assert relay.read_session("e1")["current_packet"] == 2

    def test_delivery_goes_through_the_normal_send_path(self, relay, tmp_path):
        # a queued packet is not a second kind of packet: same numbering, same GATES/REPORT FORMAT
        # footer, and the footer is applied at DELIVERY time so it names packet 002's own paths.
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        (relay.packets_dir("e1") / "001-report.md").write_text("done")
        self._deliver(relay)
        packet = (relay.packets_dir("e1") / "002-packet.md").read_text()
        assert "Do the next thing." in packet
        assert "GATES" in packet and "REPORT FORMAT" in packet
        assert "002-report.md" in packet and "002-diff.html" in packet
        assert [e["packet"] for e in self._events(relay, "packet_sent")] == [2]

    def test_delivery_is_ledgered_separately_from_queueing(self, relay, tmp_path):
        # "delivery must be provable, not assumed" — a queued packet that never lands must not look
        # like one that did.
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        (relay.packets_dir("e1") / "001-report.md").write_text("done")
        self._deliver(relay, trigger="stop-hook")
        delivered = self._events(relay, "queue_delivered")
        assert len(delivered) == 1
        assert delivered[0]["queue_id"] == 1 and delivered[0]["packet"] == 2
        assert delivered[0]["trigger"] == "stop-hook" and delivered[0]["remaining"] == 0
        assert len(self._events(relay, "packet_queued")) == 1     # and the two are distinct events

    def test_multiple_queued_deliver_oldest_first_ONE_per_idle(self, relay, tmp_path):
        # delivering both at once would inject the second mid-turn — the exact unsafety --when-idle
        # exists to prevent — so it's strictly one per idle transition, FIFO.
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path, text="# First\n\nfirst queued", name="a.md")
        self._queue(relay, tmp_path, text="# Second\n\nsecond queued", name="b.md")
        (relay.packets_dir("e1") / "001-report.md").write_text("done")
        item, _ = self._deliver(relay)
        assert item["id"] == 1
        assert "first queued" in (relay.packets_dir("e1") / "002-packet.md").read_text()
        remaining = relay.read_queue("e1")
        assert [i["id"] for i in remaining] == [2]                # #2 waits for the NEXT idle
        assert not (relay.packets_dir("e1") / "003-packet.md").exists()
        # ...and it does not deliver while the session is busy again with 002
        assert self._deliver(relay)[0] is None
        (relay.packets_dir("e1") / "002-report.md").write_text("done")
        item2, _ = self._deliver(relay)
        assert item2["id"] == 2
        assert "second queued" in (relay.packets_dir("e1") / "003-packet.md").read_text()

    def test_empty_queue_delivers_nothing(self, relay):
        self._mk(relay, status="busy", report=True)
        assert self._deliver(relay)[0] is None

    # ── failure handling ─────────────────────────────────────────────────────
    def test_failed_delivery_puts_the_packet_back(self, relay, tmp_path):
        # a refusal at delivery time (here: superseded) must not swallow the packet
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        s = relay.read_session("e1")
        s["status"] = "superseded"; s["superseded_by"] = "e2"
        relay.write_session("e1", s)
        item, send = self._deliver(relay)
        assert item is None
        send.assert_not_called()
        q = relay.read_queue("e1")
        assert len(q) == 1 and "superseded" in q[0]["last_error"]
        assert len(self._events(relay, "queue_delivery_failed")) == 1

    def test_repeated_identical_failures_are_ledgered_once(self, relay, tmp_path):
        # otherwise every `relay check` while the session stays superseded writes a ledger line
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        s = relay.read_session("e1")
        s["status"] = "superseded"; s["superseded_by"] = "e2"
        relay.write_session("e1", s)
        for _ in range(3):
            self._deliver(relay)
        assert len(self._events(relay, "queue_delivery_failed")) == 1
        assert len(relay.read_queue("e1")) == 1                   # still preserved

    def test_a_held_lock_blocks_a_concurrent_delivery(self, relay, tmp_path):
        # both triggers (Stop hook + a lead's `relay check`) can fire on the same idle transition
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        (relay.packets_dir("e1") / "001-report.md").write_text("done")
        (relay.session_dir("e1") / "queue.lock").write_text("999999")   # someone else mid-delivery
        item, send = self._deliver(relay)
        assert item is None
        send.assert_not_called()
        assert len(relay.read_queue("e1")) == 1                   # untouched, not lost

    def test_a_stale_lock_is_broken(self, relay, tmp_path):
        # a deliverer killed mid-flight must not wedge the queue forever
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        (relay.packets_dir("e1") / "001-report.md").write_text("done")
        lock = relay.session_dir("e1") / "queue.lock"
        lock.write_text("999999")
        os.utime(lock, (time.time() - 9999, time.time() - 9999))
        item, _ = self._deliver(relay)
        assert item is not None                                   # broke the stale lock, delivered
        assert not lock.exists()                                  # and released it

    def test_lock_is_released_after_a_normal_delivery(self, relay, tmp_path):
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        (relay.packets_dir("e1") / "001-report.md").write_text("done")
        self._deliver(relay)
        assert not (relay.session_dir("e1") / "queue.lock").exists()

    # ── --when-idle must not soften the OTHER refusals ───────────────────────
    def test_superseded_still_refuses_with_when_idle(self, relay, tmp_path):
        self._mk(relay, status="superseded")
        s = relay.read_session("e1"); s["superseded_by"] = "e2"; relay.write_session("e1", s)
        with pytest.raises(SystemExit) as ei:
            self._queue(relay, tmp_path)
        assert "superseded" in str(ei.value)
        assert relay.read_queue("e1") == []

    def test_launch_failed_still_refuses_with_when_idle(self, relay, tmp_path):
        self._mk(relay, status=relay.LAUNCH_FAILED)
        with pytest.raises(SystemExit) as ei:
            self._queue(relay, tmp_path)
        assert relay.LAUNCH_FAILED in str(ei.value) and "relay restart" in str(ei.value)
        assert relay.read_queue("e1") == []

    def test_unknown_session_still_refuses_with_when_idle(self, relay, tmp_path):
        with pytest.raises(SystemExit) as ei:
            self._queue(relay, tmp_path, sid="ghost")
        assert "no such session: ghost" in str(ei.value)

    def test_plain_send_without_the_flag_is_unchanged(self, relay, tmp_path):
        # the whole feature is opt-in: no --when-idle → the same hard refusal as before
        self._mk(relay, status="busy", report=False)
        with mock.patch.object(relay.iterm, "send", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            with pytest.raises(SystemExit) as ei:
                relay.cmd_send(SimpleNamespace(session_id="e1",
                                               packet=self._packet(relay, tmp_path)))
        assert "refusing to send" in str(ei.value)
        assert relay.read_queue("e1") == []

    # ── visibility + cancel ──────────────────────────────────────────────────
    def test_check_reports_the_queue_depth(self, relay, tmp_path, capsys):
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        self._queue(relay, tmp_path)
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_check(SimpleNamespace(session_id="e1", all=False, json=False))
        assert "2 queued" in capsys.readouterr().out

    def test_check_json_carries_the_queue_depth(self, relay, tmp_path, capsys):
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        capsys.readouterr()          # drop the queueing step's output; assert on cmd_check alone
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_check(SimpleNamespace(session_id="e1", all=False, json=True))
        assert json.loads(capsys.readouterr().out)[0]["queued"] == 1

    def test_check_json_stays_pure_json_while_delivering(self, relay, tmp_path, capsys):
        # A delivery triggered BY `check --json` is chatty (the send path prints, and so does the
        # Preconditions nag). That output must not land in a machine-read payload — but the
        # delivery must still happen. Assert both halves in one run.
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        (relay.packets_dir("e1") / "001-report.md").write_text("done")
        capsys.readouterr()
        with mock.patch.object(relay.iterm, "send", return_value=True) as send, \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_check(SimpleNamespace(session_id="e1", all=False, json=True))
        out = capsys.readouterr()
        assert json.loads(out.out)[0]["session_id"] == "e1"     # stdout parses — nothing leaked in
        send.assert_called_once()                               # and the delivery still happened
        assert relay.read_queue("e1") == []

    def test_check_delivers_as_the_net_trigger(self, relay, tmp_path):
        # the fallback for sessions whose Stop hook isn't armed (executor_escalation off)
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        (relay.packets_dir("e1") / "001-report.md").write_text("done")
        with mock.patch.object(relay.iterm, "send", return_value=True) as send, \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_check(SimpleNamespace(session_id="e1", all=False, json=False))
        send.assert_called_once()
        assert self._events(relay, "queue_delivered")[0]["trigger"] == "check"

    def test_queue_command_lists_pending_packets(self, relay, tmp_path, capsys):
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path, text="# Toast\n\nadd the progress toast")
        relay.cmd_queue(SimpleNamespace(session_id="e1", cancel=None))
        out = capsys.readouterr().out
        assert "1 queued packet(s)" in out and "Toast" in out and "--cancel" in out

    def test_queue_command_on_an_empty_queue(self, relay, capsys):
        self._mk(relay)
        relay.cmd_queue(SimpleNamespace(session_id="e1", cancel=None))
        assert "nothing queued" in capsys.readouterr().out

    def test_cancel_one_by_id(self, relay, tmp_path):
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path, name="a.md")
        self._queue(relay, tmp_path, name="b.md")
        relay.cmd_queue(SimpleNamespace(session_id="e1", cancel="1"))
        assert [i["id"] for i in relay.read_queue("e1")] == [2]
        assert [e["queue_id"] for e in self._events(relay, "queue_cancelled")] == [1]

    def test_cancel_all(self, relay, tmp_path):
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path, name="a.md")
        self._queue(relay, tmp_path, name="b.md")
        relay.cmd_queue(SimpleNamespace(session_id="e1", cancel="all"))
        assert relay.read_queue("e1") == []
        assert not relay.queue_path("e1").exists()

    def test_cancel_an_id_that_is_not_queued(self, relay, tmp_path):
        self._mk(relay, status="busy", report=False)
        self._queue(relay, tmp_path)
        with pytest.raises(SystemExit) as ei:
            relay.cmd_queue(SimpleNamespace(session_id="e1", cancel="7"))
        assert "no queued packet #7" in str(ei.value)
        assert len(relay.read_queue("e1")) == 1        # refused, nothing dropped

    def test_queue_command_unknown_session(self, relay):
        with pytest.raises(SystemExit) as ei:
            relay.cmd_queue(SimpleNamespace(session_id="ghost", cancel=None))
        assert "no such session: ghost" in str(ei.value)

    def test_corrupt_queue_file_reads_as_empty(self, relay):
        # a hand-edited queue.json must never take down check/list
        self._mk(relay)
        relay.queue_path("e1").write_text("{not json")
        assert relay.read_queue("e1") == []


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


class TestVerify:
    """`relay verify <sid> [--packet N] [--rerun]` (backlog §6b / #7) — the CLI seam only: real git
    repos in tmp_path, ledger events, and exit codes. The verdict LOGIC (and the §9 temper it
    encodes) is unit-tested in tests/test_report_verify.py against the pure functions.

    Exit codes are load-bearing rather than cosmetic: #16 phase 2 (autonomous auto-commit) is
    supposed to build on this, and it can only ever be allowed to key off a zero exit."""

    def _git(self, repo, *args):
        import subprocess
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    def _repo(self, tmp_path, staged=("src.py",)):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "src.py").write_text("one\n")
        (repo / "other.py").write_text("two\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-m", "init")
        for name in staged:
            (repo / name).write_text("changed\n")
            self._git(repo, "add", name)
        return repo

    REPORT = ("Changed the source file; suite green, staged.\n\n"
              "Status: clean\nRisk flags: none\nUNVERIFIED: none\nChanged: src.py\n\n"
              "## What changed\n- src.py:1 — changed it.\n\n"
              "My changes are staged, not committed, ready for the lead to review.\n")

    def _mk(self, relay, sid, repo, report_text):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        relay.write_session(sid, {"session_id": sid, "worktree": str(repo), "topic": "t",
            "scope": "t", "tab_label": f"relay-{sid}", "status": "reported",
            "current_packet": 1, "busy_since": relay.now()})
        if report_text is not None:
            (relay.packets_dir(sid) / "001-report.md").write_text(report_text)

    def _run(self, relay, sid, rerun=False, packet=None):
        with pytest.raises(SystemExit) as e:
            relay.cmd_verify(SimpleNamespace(session_id=sid, packet=packet, rerun=rerun))
        return e.value.code

    def test_truthful_report_exits_zero_and_ledgers_counts_match(self, relay, tmp_path, capsys):
        self._mk(relay, "e1", self._repo(tmp_path), self.REPORT)
        assert self._run(relay, "e1") == 0
        out = capsys.readouterr().out
        assert "COUNTS-MATCH" in out
        assert 'must NEVER be read as "the report is true"' in out
        events = [json.loads(l) for l in relay.LEDGER.read_text().splitlines()]
        rec = [e for e in events if e["event"] == "report_verify"][-1]
        assert rec["session_id"] == "e1" and rec["packet"] == 1
        assert rec["verdict"] == "COUNTS-MATCH" and rec["mismatches"] == 0

    def test_claimed_file_never_staged_exits_one_and_names_it(self, relay, tmp_path, capsys):
        report = self.REPORT.replace("- src.py:1 — changed it.",
                                     "- src.py:1 — changed it.\n- other.py:1 — changed it too.")
        self._mk(relay, "e1", self._repo(tmp_path), report)
        assert self._run(relay, "e1") == 1
        out = capsys.readouterr().out
        assert "MISMATCH" in out and "other.py" in out
        rec = [json.loads(l) for l in relay.LEDGER.read_text().splitlines()][-1]
        assert rec["verdict"] == "MISMATCH" and rec["mismatches"] == 1

    def test_missing_unverified_line_exits_two_as_malformed(self, relay, tmp_path, capsys):
        self._mk(relay, "e1", self._repo(tmp_path), self.REPORT.replace("UNVERIFIED: none\n", ""))
        assert self._run(relay, "e1") == 2
        assert "MALFORMED" in capsys.readouterr().out
        assert [json.loads(l) for l in relay.LEDGER.read_text().splitlines()][-1]["verdict"] == "MALFORMED"

    def test_risk_flags_are_echoed_in_the_output(self, relay, tmp_path, capsys):
        report = self.REPORT.replace("Risk flags: none",
                                     "Risk flags: weakened a parity assertion")
        self._mk(relay, "e1", self._repo(tmp_path), report)
        self._run(relay, "e1")
        assert "weakened a parity assertion" in capsys.readouterr().out

    def test_no_report_yet_is_a_clean_refusal(self, relay, tmp_path):
        self._mk(relay, "e1", self._repo(tmp_path), None)
        with pytest.raises(SystemExit) as e:
            relay.cmd_verify(SimpleNamespace(session_id="e1", packet=None, rerun=False))
        assert "no report yet" in str(e.value.code)

    def test_unknown_session_is_a_clean_refusal(self, relay):
        with pytest.raises(SystemExit) as e:
            relay.cmd_verify(SimpleNamespace(session_id="nope", packet=None, rerun=False))
        assert "no such session" in str(e.value.code)

    def test_packet_flag_selects_the_report(self, relay, tmp_path, capsys):
        self._mk(relay, "e1", self._repo(tmp_path), self.REPORT)
        (relay.packets_dir("e1") / "002-report.md").write_text(
            self.REPORT.replace("UNVERIFIED: none\n", ""))
        assert self._run(relay, "e1", packet=2) == 2  # packet 2 is the malformed one
        assert "packet 002" in capsys.readouterr().out

    def test_default_run_does_not_execute_anything(self, relay, tmp_path, capsys):
        """Without --rerun nothing is executed, and the output says so — 'did not run' must never
        be mistakable for 'ran and matched' (§9.6a)."""
        report = self.REPORT.replace("## What changed",
                                     "Ran `python3 -m pytest tests -q` — 5 passed.\n\n## What changed")
        self._mk(relay, "e1", self._repo(tmp_path), report)
        with mock.patch.object(relay, "_rerun_declared") as rr:
            assert self._run(relay, "e1") == 0
        rr.assert_not_called()
        assert "NOT RE-RUN" in capsys.readouterr().out

    def test_rerun_only_executes_allowlisted_commands(self, relay, tmp_path):
        """The security boundary: a report is untrusted text. Anything not pytest-shaped, and
        anything carrying shell metacharacters, must never reach subprocess."""
        report = self.REPORT.replace(
            "## What changed",
            "Ran `python3 -m pytest tests -q`, then `rm -rf /` and `npm test`.\n\n## What changed")
        self._mk(relay, "e1", self._repo(tmp_path), report)
        seen = []

        def fake(worktree, commands, declared):
            seen.extend(commands)
            return [{"cmd": c, "passed": 5, "declared": declared} for c in commands]

        with mock.patch.object(relay, "_rerun_declared", side_effect=fake):
            self._run(relay, "e1", rerun=True)
        assert seen == ["python3 -m pytest tests -q"]

    def test_rerun_executes_argv_without_a_shell(self, relay, tmp_path):
        """argv-only, never `shell=True` — the other half of the boundary above."""
        report = self.REPORT.replace("## What changed",
                                     "Ran `python3 -m pytest tests -q` — 5 passed.\n\n## What changed")
        self._mk(relay, "e1", self._repo(tmp_path), report)
        calls = []
        real_run = relay.subprocess.run

        def spy(argv, **kw):
            calls.append((argv, kw))
            if argv and argv[0] == "git":
                return real_run(argv, **kw)
            return SimpleNamespace(returncode=0, stdout="5 passed in 0.1s", stderr="")

        with mock.patch.object(relay.subprocess, "run", side_effect=spy):
            assert self._run(relay, "e1", rerun=True) == 0
        pytest_calls = [(a, k) for a, k in calls if a and a[0] != "git"]
        assert pytest_calls, "the declared pytest command was never run"
        for argv, kw in pytest_calls:
            assert isinstance(argv, list)          # argv, not a string
            assert not kw.get("shell")             # and never through a shell

    def test_rerun_that_produces_no_count_is_inconclusive_not_zero(self, relay, tmp_path, capsys):
        """The live failure this verdict exists for: a re-run that yields no `N passed` must not
        exit 0, or an autonomous caller would read a suite that never ran as agreement."""
        report = self.REPORT.replace("## What changed",
                                     "Ran `python3 -m pytest tests -q` — 5 passed.\n\n## What changed")
        self._mk(relay, "e1", self._repo(tmp_path), report)
        with mock.patch.object(relay, "_rerun_declared",
                               return_value=[{"cmd": "python3 -m pytest tests -q",
                                              "passed": None, "declared": 5}]):
            assert self._run(relay, "e1", rerun=True) == 3
        assert "INCONCLUSIVE" in capsys.readouterr().out

    def test_missing_worktree_is_a_clean_refusal(self, relay, tmp_path):
        relay.packets_dir("e1").mkdir(parents=True, exist_ok=True)
        relay.write_session("e1", {"session_id": "e1", "worktree": None, "status": "reported",
                                   "current_packet": 1})
        (relay.packets_dir("e1") / "001-report.md").write_text(self.REPORT)
        with pytest.raises(SystemExit) as e:
            relay.cmd_verify(SimpleNamespace(session_id="e1", packet=None, rerun=False))
        assert "no recorded worktree" in str(e.value.code)

    def test_gone_worktree_degrades_instead_of_crashing(self, relay, tmp_path):
        """_git_lines swallows git failures on purpose: a deleted worktree must yield fewer facts,
        never a traceback in the lead's face."""
        repo = self._repo(tmp_path)
        self._mk(relay, "e1", repo, self.REPORT)
        import shutil as _sh
        _sh.rmtree(repo)
        assert self._run(relay, "e1") == 1  # claims a file nothing can confirm → MISMATCH, no crash


class TestVerifyForAutocommit:
    """`relay verify <sid> --for-autocommit` (#16 phase 2) — the CLI seam of the auto-commit gate.
    The clearance LOGIC is unit-tested in tests/test_report_verify.py; this pins the wiring: the
    attestation flags, the exit code, and the ledger record.

    Nothing here (or in the command) ever commits: the commit stays the lead's own action."""

    def _run(self, relay, sid, **flags):
        args = SimpleNamespace(session_id=sid, packet=None, rerun=False, for_autocommit=True,
                               in_plan=flags.get("in_plan", False),
                               diff_reviewed=flags.get("diff_reviewed", False))
        with pytest.raises(SystemExit) as e:
            relay.cmd_verify(args)
        return e.value.code

    def _events(self, relay, name):
        return [json.loads(l) for l in relay.LEDGER.read_text().splitlines()
                if json.loads(l)["event"] == name]

    def _setup(self, relay, tmp_path, report=None, staged=("src.py",)):
        tv = TestVerify()
        repo = tv._repo(tmp_path, staged=staged)
        tv._mk(relay, "e1", repo, report if report is not None else tv.REPORT)
        return repo

    def test_fully_cleared_exits_zero_and_prints_cleared(self, relay, tmp_path, capsys):
        self._setup(relay, tmp_path)
        assert self._run(relay, "e1", in_plan=True, diff_reviewed=True) == 0
        assert "AUTO-COMMIT: CLEARED" in capsys.readouterr().out

    def test_cleared_run_writes_an_auto_commit_event_with_verdict_and_diff_stat(
            self, relay, tmp_path, capsys):
        self._setup(relay, tmp_path)
        self._run(relay, "e1", in_plan=True, diff_reviewed=True)
        rec = self._events(relay, "auto_commit")[-1]
        assert rec["session_id"] == "e1" and rec["packet"] == 1
        assert rec["verdict"] == "COUNTS-MATCH" and rec["cleared"] is True
        assert rec["files"] == 1 and rec["insertions"] >= 1
        assert rec["in_plan"] is True and rec["diff_reviewed"] is True

    def test_missing_attestations_do_not_clear_and_are_ledgered_as_blocked(
            self, relay, tmp_path, capsys):
        self._setup(relay, tmp_path)
        assert self._run(relay, "e1") != 0
        assert "NOT-CLEARED-BECAUSE-not-attested-in-plan" in capsys.readouterr().out
        rec = self._events(relay, "auto_commit_blocked")[-1]
        assert rec["cleared"] is False and rec["reason"] == "not-attested-in-plan"
        assert not self._events(relay, "auto_commit")

    def test_counts_match_that_fails_a_condition_still_exits_nonzero(self, relay, tmp_path, capsys):
        """The trap this guards: the verdict is fine, so the verdict's own exit code is 0. In
        --for-autocommit mode the exit code answers 'may the lead commit?', not 'is the verdict
        clean?' — conflating them would auto-clear every unattested run."""
        self._setup(relay, tmp_path)
        assert self._run(relay, "e1", in_plan=True) != 0
        assert "NOT-CLEARED-BECAUSE-not-attested-diff-reviewed" in capsys.readouterr().out

    def test_signoff_gated_path_blocks_even_when_fully_attested(self, relay, tmp_path, capsys):
        report = TestVerify.REPORT.replace("- src.py:1 — changed it.",
                                           "- hooks/hook.py:1 — changed it.")
        repo = tmp_path / "repo"
        repo.mkdir()
        tv = TestVerify()
        tv._git(repo, "init", "-q")
        (repo / "hooks").mkdir()
        (repo / "hooks" / "hook.py").write_text("one\n")
        tv._git(repo, "add", "-A")
        tv._git(repo, "commit", "-m", "init")
        (repo / "hooks" / "hook.py").write_text("changed\n")
        tv._git(repo, "add", "hooks/hook.py")
        tv._mk(relay, "e1", repo, report)
        assert self._run(relay, "e1", in_plan=True, diff_reviewed=True) != 0
        out = capsys.readouterr().out
        assert "NOT-CLEARED-BECAUSE-signoff-gated-path-touched" in out
        assert "hooks/hook.py" in out

    def test_malformed_report_blocks_the_gate(self, relay, tmp_path, capsys):
        self._setup(relay, tmp_path, report=TestVerify.REPORT.replace("UNVERIFIED: none\n", ""))
        assert self._run(relay, "e1", in_plan=True, diff_reviewed=True) == 2
        assert "NOT-CLEARED-BECAUSE-verdict-is-MALFORMED" in capsys.readouterr().out

    def test_without_the_flag_no_clearance_block_and_no_auto_commit_event(
            self, relay, tmp_path, capsys):
        """A plain `relay verify` must stay exactly what it was — no clearance, nothing ledgered
        about committing."""
        self._setup(relay, tmp_path)
        with pytest.raises(SystemExit):
            relay.cmd_verify(SimpleNamespace(session_id="e1", packet=None, rerun=False,
                                             for_autocommit=False, in_plan=False,
                                             diff_reviewed=False))
        assert "AUTO-COMMIT" not in capsys.readouterr().out
        assert not self._events(relay, "auto_commit")
        assert not self._events(relay, "auto_commit_blocked")

    def test_the_caveat_still_prints_above_a_cleared_line(self, relay, tmp_path, capsys):
        """The clearance block must never be readable on its own as 'the work is fine'."""
        self._setup(relay, tmp_path)
        self._run(relay, "e1", in_plan=True, diff_reviewed=True)
        out = capsys.readouterr().out
        assert 'must NEVER be read as "the report is true"' in out
        assert "clears the AUTOMATION only" in out


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
        # Per-pid pid_alive for the same reason as TestRestartResume._run — see that comment.
        captured = {}
        with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: captured.update(kw)), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123), \
             mock.patch.object(relay, "read_iterm_id", return_value="w0t0p0:NEW"), \
             mock.patch.object(relay, "_ensure_tab_label", return_value=True), \
             mock.patch.object(relay, "LAUNCH_GRACE_SECONDS", 0), \
             mock.patch.object(relay, "pid_alive", side_effect=lambda p: True if p == 123 else alive):
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

    def test_handoff_retitles_the_predecessor_tab(self, relay, tmp_path, monkeypatch):
        """#4/§4: after a handoff the tab bar briefly holds TWO tabs titled `[Lead] webapp`. The
        husk is retitled so exactly one tab ever claims to be the live lead."""
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp",
                                      cwd=str(tmp_path), iterm_session="w0t0p0:OLD")
        self._env(monkeypatch, "lead-1")
        with mock.patch.object(relay.iterm, "rename_by_id", return_value=True) as rn:
            self._run(relay, self._md(tmp_path))
        # by HANDLE, never by label — the label is precisely what's ambiguous right now (#2)
        rn.assert_called_once_with("w0t0p0:OLD", "[ex-Lead] webapp")
        succ = [m for sid, m in self._leads(relay).items() if sid != "lead-1"][0]
        assert succ["tab_label"] == "[Lead] webapp"                    # successor untouched
        assert succ["predecessor"]["tab_label"] == "[ex-Lead] webapp"  # husk record kept in sync,
        # so close-predecessor's label FALLBACK can still find it
        assert any(e["event"] == "predecessor_retitled" for e in self._ledger_events(relay))

    def test_handoff_without_a_recorded_handle_is_a_silent_noop(self, relay, tmp_path, monkeypatch):
        # a legacy marker (pre-iterm_session) must degrade to no rename, not an error
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        with mock.patch.object(relay.iterm, "rename_by_id") as rn:
            self._run(relay, self._md(tmp_path))
        rn.assert_not_called()
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1") == {}   # handoff still done
        assert any(e["event"] == "lead_handoff" for e in self._ledger_events(relay))

    def test_a_failing_retitle_never_fails_the_handoff(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp",
                                      cwd=str(tmp_path), iterm_session="w0t0p0:OLD",
                                      tab_label="[Lead] webapp")
        self._env(monkeypatch, "lead-1")
        with mock.patch.object(relay.iterm, "rename_by_id", side_effect=OSError("boom")):
            self._run(relay, self._md(tmp_path))                       # must not raise
        assert relay.lead_guard.read_marker(relay.STATE_ROOT, "lead-1") == {}   # stepped down anyway
        succ = [m for sid, m in self._leads(relay).items() if sid != "lead-1"][0]
        assert succ["predecessor"]["tab_label"] == "[Lead] webapp"      # unchanged: rename didn't take

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

    def test_build_handoff_copy_reappend_is_idempotent(self, relay):
        # #19 repro: re-handoff of an ALREADY-PROCESSED doc (the source passed to `relay handoff`
        # is itself a prior successor's relay-owned copy, aftercare already appended) must replace
        # the stale aftercare section, never stack a second one on top of it.
        once = relay.build_handoff_copy("what's in flight", "aaaa")
        assert once.count("SUCCESSOR AFTERCARE") == 1
        twice = relay.build_handoff_copy(once, "bbbb")
        assert twice.count("SUCCESSOR AFTERCARE") == 1
        assert "bbbb" in twice
        assert "aaaa" not in twice   # the stale pin from the first pass must not survive
        assert twice.startswith("what's in flight")

    def test_dropped_discipline_markers_flags_missing(self, relay):
        # d4: a marker present in the predecessor doc but absent from the new doc is flagged.
        pred = "queue item: box setup [ops-not-lead-work] — delegate to an executor"
        new = "queue item: box setup — get it done"
        assert relay.dropped_discipline_markers(pred, new) == ["[ops-not-lead-work]"]

    def test_dropped_discipline_markers_silent_when_carried_forward(self, relay):
        pred = "queue item: box setup [ops-not-lead-work] — delegate to an executor"
        new = "queue item: box setup [ops-not-lead-work] — delegate to an executor, per usual"
        assert relay.dropped_discipline_markers(pred, new) == []

    def test_dropped_discipline_markers_silent_with_no_predecessor_doc(self, relay):
        # First-ever lead: nothing inherited, nothing to compare against — must not false-positive.
        assert relay.dropped_discipline_markers("", "queue item: box setup — get it done") == []

    def test_handoff_warns_when_inherited_marker_dropped(self, relay, tmp_path, monkeypatch, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        # Simulate lead-1 itself having been a successor: its own inherited copy carried a marker.
        inherited_dir = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1")
        inherited_dir.mkdir(parents=True, exist_ok=True)
        (inherited_dir / "handoff.md").write_text("prior queue item [ops-not-lead-work] ssh in and build it")
        md = self._md(tmp_path, text="new queue item: ssh in and build it")   # marker dropped
        self._run(relay, md)
        out = capsys.readouterr().out
        assert "[ops-not-lead-work]" in out
        assert "dropped" in out.lower()

    def test_handoff_silent_when_inherited_marker_carried_forward(self, relay, tmp_path, monkeypatch, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path))
        self._env(monkeypatch, "lead-1")
        inherited_dir = relay.lead_guard.lead_dir(relay.STATE_ROOT, "lead-1")
        inherited_dir.mkdir(parents=True, exist_ok=True)
        (inherited_dir / "handoff.md").write_text("prior queue item [ops-not-lead-work] ssh in and build it")
        md = self._md(tmp_path, text="new queue item [ops-not-lead-work] — delegate, ssh in and build it")
        self._run(relay, md)
        out = capsys.readouterr().out
        assert "dropped" not in out.lower()

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

    # ---- arm-time field stamping (§1) ---------------------------------------------------------
    # cmd_handoff writes the successor's marker TWICE (pre-arm, then a refresh once the tab's
    # iterm_session is known). Both must stamp the full field set — write_marker rewrites the whole
    # marker, so a field passed only to the first write is dropped by the second.

    def _successor(self, relay, tmp_path, monkeypatch, caller_marker_kwargs=None):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp",
                                      cwd=str(tmp_path), **(caller_marker_kwargs or {}))
        self._env(monkeypatch, "lead-1")
        self._run(relay, self._md(tmp_path))
        return [m for sid, m in self._leads(relay).items() if sid != "lead-1"][0]

    def test_successor_records_backend(self, relay, tmp_path, monkeypatch):
        # §1: the successor's marker used to have no `backend`, so every nudge to a handed-off lead
        # went through _probe_backend_for_tab instead of being addressed. cmd_handoff spawns the
        # successor's tab itself, so it knows the answer.
        succ = self._successor(relay, tmp_path, monkeypatch)
        assert succ["backend"] == relay.iterm.NAME
        assert succ["backend"] in ("iterm", "terminal")

    def test_successor_backend_survives_the_post_spawn_refresh(self, relay, tmp_path, monkeypatch):
        # The second write (adding iterm_session) must not drop what the pre-arm stamped — proven
        # by asserting both fields are present on the FINAL marker at once.
        succ = self._successor(relay, tmp_path, monkeypatch)
        assert succ["backend"] == relay.iterm.NAME
        assert succ["iterm_session"] == "w1t1p0:NEW"

    def test_successor_started_is_stable_across_both_writes(self, relay, tmp_path, monkeypatch):
        # One `started` for one lead: the pre-arm and the refresh must not each default to their own
        # now(), or the field would mean "when the tab finished opening".
        stamps = iter(["2030-01-01T00:00:00", "2030-06-06T06:06:06"])
        with mock.patch.object(relay, "now", lambda: next(stamps)):
            succ = self._successor(relay, tmp_path, monkeypatch)
        assert succ["started"] == "2030-01-01T00:00:00"

    def test_successor_does_not_inherit_autonomous_posture(self, relay, tmp_path, monkeypatch):
        # §6f: the posture is per-session and opt-in-each-time. A successor silently inheriting its
        # predecessor's auto posture is exactly the unannounced inversion §6f warns against — the
        # successor's aftercare /relay:mode is what applies (and announces) the config default.
        succ = self._successor(relay, tmp_path, monkeypatch,
                               {"autonomous": True, "autonomous_source": "command"})
        assert succ["autonomous"] is False
        assert succ["autonomous_source"] == "config"


class TestLeadListTermColumn:
    """`relay list`'s TERM cell: which terminal app hosts each lead's tab. A "?" is the degraded
    state §1 is about — nudges to that lead are probe-or-guess rather than addressed — and it used
    to be findable only by reading marker JSON."""

    def _render(self, relay, capsys, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        capsys.readouterr()
        relay.cmd_list(SimpleNamespace(lead=None, all=True, json=False, closed=False))
        return capsys.readouterr().out

    def test_renders_recorded_backend(self, relay, capsys, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd="/w",
                                      backend="iterm")
        out = self._render(relay, capsys, monkeypatch)
        assert "TERM" in out                      # the column exists
        assert "iterm" in out

    def test_unrecorded_backend_renders_question_mark(self, relay, capsys, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="legacy", cwd="/w")
        out = self._render(relay, capsys, monkeypatch)
        header, row = [l for l in out.splitlines() if "TERM" in l][0], \
                      [l for l in out.splitlines() if l.startswith("legacy")][0]
        assert row.split()[header.split().index("TERM")] == "?"

    def test_term_cell_helper(self, relay):
        assert relay._lead_term_cell({"backend": "terminal"}) == "terminal"
        assert relay._lead_term_cell({"backend": None}) == "?"
        assert relay._lead_term_cell({}) == "?"


class TestLeadLiveness:
    """§3: `relay list`'s LIVE cell — live / unreachable / ghost — from a tab-alive probe plus
    the same fresh-stamp window unique_lead_project uses (LEAD_LIVE_WINDOW_SECONDS)."""

    NOW = time.mktime(time.strptime("2026-01-01T12:00:00", "%Y-%m-%dT%H:%M:%S"))

    @classmethod
    def _stamp(cls, offset_seconds):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cls.NOW - offset_seconds))

    def _marker(self, last_active_offset=60, tab_label="[Lead] p", iterm_session="w0t0p0:X"):
        return {"session_id": "lead-1", "project": "p", "tab_label": tab_label,
                "iterm_session": iterm_session, "last_active": self._stamp(last_active_offset)}

    def test_alive_tab_is_live_regardless_of_stamp_age(self, relay):
        # an idle lead waiting on the human can legitimately go quiet past the window without
        # being dead — the tab being alive is what matters here, not staleness.
        m = self._marker(last_active_offset=relay.LEAD_LIVE_WINDOW_SECONDS + 999)
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            assert relay._lead_liveness(m, now_ts=self.NOW) == "live"

    def test_dead_tab_fresh_stamp_is_unreachable(self, relay):
        # the alpha_service failure §3 calls out as "the valuable one".
        m = self._marker(last_active_offset=60)
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            assert relay._lead_liveness(m, now_ts=self.NOW) == "unreachable"

    def test_dead_tab_stale_stamp_is_ghost(self, relay):
        # the beta_view case: dead for days, never stepped down.
        m = self._marker(last_active_offset=relay.LEAD_LIVE_WINDOW_SECONDS + 1)
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            assert relay._lead_liveness(m, now_ts=self.NOW) == "ghost"

    def test_missing_last_active_with_dead_tab_is_ghost(self, relay):
        m = self._marker()
        m["last_active"] = None
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            assert relay._lead_liveness(m, now_ts=self.NOW) == "ghost"

    def test_is_alive_exception_degrades_to_not_alive(self, relay):
        # fail-open: a probe crash must surface a degraded state, never take down the render.
        m = self._marker(last_active_offset=relay.LEAD_LIVE_WINDOW_SECONDS + 1)
        with mock.patch.object(relay.iterm, "is_alive", side_effect=RuntimeError("boom")):
            assert relay._lead_liveness(m, now_ts=self.NOW) == "ghost"

    def test_legacy_marker_with_no_tab_label_never_crashes(self, relay):
        # pre-stamping legacy marker: tab_label/iterm_session never recorded at all.
        m = {"session_id": "lead-1", "project": "p", "last_active": self._stamp(60)}
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            assert relay._lead_liveness(m, now_ts=self.NOW) == "unreachable"

    def test_liveness_cell_shapes(self, relay):
        # _lead_liveness_cell has no now_ts override (mirrors _lead_auto_cell/_lead_term_cell,
        # which always resolve against real wall-clock time) — so stamps here must be relative to
        # relay.now(), not the fixed self.NOW used by the now_ts-injectable tests above.
        fresh = relay.now()
        stale = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.localtime(time.time() - relay.LEAD_LIVE_WINDOW_SECONDS - 1))
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            assert relay._lead_liveness_cell(self._marker())[0] == "live"
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            assert relay._lead_liveness_cell(
                {"session_id": "lead-1", "tab_label": "x", "iterm_session": "y",
                 "last_active": fresh})[0] == "unreachable"
            assert relay._lead_liveness_cell(
                {"session_id": "lead-1", "tab_label": "x", "iterm_session": "y",
                 "last_active": stale})[0] == "ghost"

    def _render(self, relay, capsys, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        capsys.readouterr()
        relay.cmd_list(SimpleNamespace(lead=None, all=True, json=False, closed=False))
        return capsys.readouterr().out

    def test_live_lead_renders_in_table(self, relay, capsys, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="alpha",
                                      tab_label="[Lead] alpha", iterm_session="w0t0p0:X")
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            out = self._render(relay, capsys, monkeypatch)
        assert "LIVE" in out
        row = [l for l in out.splitlines() if l.startswith("alpha")][0]
        assert "live" in row.split()

    def test_ghost_lead_renders_and_footnote(self, relay, capsys, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="beta_view")
        mp = relay.lead_guard.marker_path(relay.STATE_ROOT, "lead-1")
        m = json.loads(mp.read_text())
        m["last_active"] = "2000-01-01T00:00:00"
        mp.write_text(json.dumps(m))
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            out = self._render(relay, capsys, monkeypatch)
        row = [l for l in out.splitlines() if l.startswith("beta_view")][0]
        assert "ghost" in row.split()
        assert "LIVE=ghost" in out
        assert "beta_view" in [l for l in out.splitlines() if "LIVE=ghost" in l][0]

    def test_unreachable_lead_renders_and_footnote(self, relay, capsys, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="alpha_service")
        with mock.patch.object(relay.iterm, "is_alive", return_value=False):
            out = self._render(relay, capsys, monkeypatch)
        row = [l for l in out.splitlines() if l.startswith("alpha_service")][0]
        assert "unreachable" in row.split()
        assert "LIVE=unreachable" in out

    def test_legacy_marker_renders_without_crashing(self, relay, capsys, monkeypatch):
        # no tab_label/iterm_session at all (write_marker's own defaults) — must render, not raise.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="legacy")
        out = self._render(relay, capsys, monkeypatch)
        row = [l for l in out.splitlines() if l.startswith("legacy")][0]
        assert row.split()[-1]  # rendered a full row, no crash


class TestLeadTranscriptMB:
    """§9: `relay list`'s MB cell — a lead's OWN transcript size, read straight from disk (list
    has no --statusline payload to reuse, unlike the status-line's weight segment)."""

    def test_reads_real_transcript(self, relay, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        proj_dir = tmp_path / "cfg" / "projects" / "-some-project"
        proj_dir.mkdir(parents=True)
        (proj_dir / "lead-1.jsonl").write_bytes(b"x" * (2 * 1024 * 1024))
        assert relay._lead_mb_cell({"session_id": "lead-1"}) == "2.0"

    def test_dash_when_unlocatable(self, relay, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        (tmp_path / "cfg" / "projects").mkdir(parents=True)
        assert relay._lead_mb_cell({"session_id": "lead-1"}) == "-"

    def test_dash_when_no_projects_root(self, relay, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
        assert relay._lead_mb_cell({"session_id": "lead-1"}) == "-"

    def test_dash_for_missing_session_id(self, relay):
        assert relay._lead_mb_cell({}) == "-"

    def test_mb_column_renders_in_list(self, relay, capsys, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        proj_dir = tmp_path / "cfg" / "projects" / "-p"
        proj_dir.mkdir(parents=True)
        (proj_dir / "lead-1.jsonl").write_bytes(b"x" * (3 * 1024 * 1024))
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="heavy")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_list(SimpleNamespace(lead=None, all=True, json=False, closed=False))
        out = capsys.readouterr().out
        assert "MB" in out
        row = [l for l in out.splitlines() if l.startswith("heavy")][0]
        assert "3.0" in row.split()


class TestHeavinessHelpers:
    """§6e: the shared MB/threshold helpers behind both the EXECUTORS MB column (e1) and
    cmd_send's heaviness gate (e2) — one number, read the same way everywhere."""

    def _set_threshold(self, relay, mb):
        (relay.STATE_ROOT / "lead").mkdir(parents=True, exist_ok=True)
        relay.lead_guard.config_path(relay.STATE_ROOT).write_text(json.dumps({"handoff_nudge_mb": mb}))

    def test_threshold_default_matches_handoff_nudge_default(self, relay):
        # §6e design note: "reuse the existing handoff-nudge thresholds rather than inventing new
        # ones" — same key, same default, not a second number to keep in sync.
        assert relay._heaviness_threshold_mb() == relay.lead_guard.LEAD_DEFAULTS["handoff_nudge_mb"]

    def test_threshold_reads_config_override(self, relay):
        self._set_threshold(relay, 2)
        assert relay._heaviness_threshold_mb() == 2.0

    def test_is_heavy_at_or_above_threshold(self, relay):
        assert relay._is_heavy(5.0, 5.0) is True
        assert relay._is_heavy(5.1, 5.0) is True
        assert relay._is_heavy(4.9, 5.0) is False

    def test_is_heavy_none_is_never_heavy(self, relay):
        # an unlocatable transcript must never gate/warn — nothing to warn about, and guessing
        # would be worse than staying quiet.
        assert relay._is_heavy(None, 0.0) is False

    def test_transcript_mb_for_missing_session_is_none(self, relay):
        assert relay._transcript_mb_for(None) is None
        assert relay._transcript_mb_for("") is None

    def test_transcript_mb_for_real_file(self, relay, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        d = tmp_path / "cfg" / "projects" / "-p"
        d.mkdir(parents=True)
        (d / "cs-x.jsonl").write_bytes(b"x" * (1024 * 1024))
        assert relay._transcript_mb_for("cs-x") == pytest.approx(1.0, abs=0.01)

    def test_transcript_mb_for_unlocatable_is_none(self, relay, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        (tmp_path / "cfg" / "projects").mkdir(parents=True)
        assert relay._transcript_mb_for("cs-x") is None

    def test_mb_cell_text(self, relay):
        assert relay._mb_cell_text(3.14159) == "3.1"
        assert relay._mb_cell_text(None) == "-"


class TestExecutorMBColumn:
    """§6e e1: `relay list`'s EXECUTORS table gains the same MB visibility leads got in #3, plus
    a ver?/stale-hooks-style "heavy" footnote naming executors past the threshold."""

    def _exec(self, relay, sid, claude_session, status="reported", packet=3):
        relay.write_session(sid, {"session_id": sid, "current_packet": packet, "status": status,
            "topic": "t", "worktree": "/w", "scope": "", "model": "sonnet",
            "claude_session": claude_session, "busy_since": relay.now(), "updated": relay.now(),
            "owner_lead": None, "owner_project": None})
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        (relay.packets_dir(sid) / f"{packet:03d}-report.md").write_text("done")

    def _transcript(self, tmp_path, monkeypatch, name, mb):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        d = tmp_path / "cfg" / "projects" / "-p"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.jsonl").write_bytes(b"x" * int(mb * 1024 * 1024))

    def test_light_and_heavy_rows_both_show_mb(self, relay, capsys, tmp_path, monkeypatch):
        self._transcript(tmp_path, monkeypatch, "cs-light", 0.5)
        d = tmp_path / "cfg" / "projects" / "-p"
        (d / "cs-heavy.jsonl").write_bytes(b"x" * int(6 * 1024 * 1024))
        self._exec(relay, "light-1", "cs-light")
        self._exec(relay, "heavy-1", "cs-heavy")
        relay.cmd_list(SimpleNamespace(lead=None, all=True, json=False, closed=False))
        out = capsys.readouterr().out
        assert "MB" in out
        light_row = [l for l in out.splitlines() if l.startswith("light-1")][0]
        heavy_row = [l for l in out.splitlines() if l.startswith("heavy-1")][0]
        assert "0.5" in light_row.split()
        assert "6.0" in heavy_row.split()

    def test_heavy_footnote_names_the_session_pkts_and_mb(self, relay, capsys, tmp_path, monkeypatch):
        self._transcript(tmp_path, monkeypatch, "cs-heavy", 6)
        self._exec(relay, "heavy-1", "cs-heavy", packet=9)
        relay.cmd_list(SimpleNamespace(lead=None, all=True, json=False, closed=False))
        out = capsys.readouterr().out
        assert "⚠ heavy:" in out
        footnote = [l for l in out.splitlines() if "⚠ heavy:" in l][0]
        assert "heavy-1" in footnote and "9 pkts" in footnote and "6.0MB" in footnote

    def test_no_footnote_when_nothing_heavy(self, relay, capsys, tmp_path, monkeypatch):
        self._transcript(tmp_path, monkeypatch, "cs-light", 0.2)
        self._exec(relay, "light-1", "cs-light")
        relay.cmd_list(SimpleNamespace(lead=None, all=True, json=False, closed=False))
        out = capsys.readouterr().out
        assert "⚠ heavy:" not in out

    def test_unlocatable_transcript_is_dash_not_heavy(self, relay, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        (tmp_path / "cfg" / "projects").mkdir(parents=True)
        self._exec(relay, "nope-1", "cs-missing")
        relay.cmd_list(SimpleNamespace(lead=None, all=True, json=False, closed=False))
        out = capsys.readouterr().out
        row = [l for l in out.splitlines() if l.startswith("nope-1")][0]
        assert "-" in row.split()
        assert "⚠ heavy:" not in out


class TestSendHeavinessGate:
    """§6e e2: `relay send` into a heavy session refuses unless --heavy-override "<reason>" is
    given (mirrors executor_model_ceiling exactly), and the override reason lands in the ledger.
    §9 binding constraint: wording must NUDGE (state the fact + the alternative), never scold."""

    def _mk(self, relay, sid="e1", claude_session="cs-x", status="reported"):
        relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
        relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
            "tab_label": "relay-e1", "model": None, "pid": os.getpid(), "iterm_session": "w0t0p0:OLD",
            "claude_session": claude_session, "status": status, "current_packet": 1,
            "busy_since": relay.now(), "created": relay.now(), "updated": relay.now()})
        (relay.packets_dir(sid) / "001-packet.md").write_text("first packet")
        (relay.packets_dir(sid) / "001-report.md").write_text("done")

    def _packet(self, tmp_path):
        p = tmp_path / "next-packet.md"
        p.write_text("# Follow-up\n\nDo the next thing.")
        return str(p)

    def _transcript(self, tmp_path, monkeypatch, name, mb):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        d = tmp_path / "cfg" / "projects" / "-p"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.jsonl").write_bytes(b"x" * int(mb * 1024 * 1024))

    def _ledger_events(self, relay):
        if not relay.LEDGER.exists():
            return []
        return [json.loads(l) for l in relay.LEDGER.read_text().splitlines()]

    def test_heavy_session_refuses_without_override(self, relay, tmp_path, monkeypatch):
        self._transcript(tmp_path, monkeypatch, "cs-x", 6)
        self._mk(relay)
        with mock.patch.object(relay.iterm, "send") as send:
            with pytest.raises(SystemExit) as ei:
                relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(tmp_path),
                                               heavy_override=None))
        msg = str(ei.value)
        assert "heavy" in msg and "6.0MB" in msg and "--heavy-override" in msg
        send.assert_not_called()  # refused before any delivery attempt

    def test_nudge_wording_states_fact_and_alternative_not_a_scold(self, relay, tmp_path, monkeypatch):
        # §9: "heaviness is not degradation... the override will be used often and legitimately."
        self._transcript(tmp_path, monkeypatch, "cs-x", 6)
        self._mk(relay)
        with mock.patch.object(relay.iterm, "send"):
            with pytest.raises(SystemExit) as ei:
                relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(tmp_path),
                                               heavy_override=None))
        msg = str(ei.value).lower()
        assert "not a verdict on its work" in msg or "not degrad" in msg or "disciplined executor" in msg
        assert "relay retire" in msg  # the cheap-escape alternative is named, not just a refusal

    def test_heavy_session_proceeds_with_override_and_ledgers_reason(self, relay, tmp_path, monkeypatch):
        self._transcript(tmp_path, monkeypatch, "cs-x", 6)
        self._mk(relay)
        with mock.patch.object(relay.iterm, "send", return_value=True) as send:
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(tmp_path),
                                           heavy_override="need to finish this thread"))
        send.assert_called_once()
        events = self._ledger_events(relay)
        override_events = [e for e in events if e["event"] == "heavy_override"]
        assert len(override_events) == 1
        assert override_events[0]["session_id"] == "e1"
        assert override_events[0]["reason"] == "need to finish this thread"
        assert override_events[0]["mb"] == 6.0

    def test_light_session_sends_silently_no_gate(self, relay, tmp_path, monkeypatch):
        self._transcript(tmp_path, monkeypatch, "cs-x", 0.5)
        self._mk(relay)
        with mock.patch.object(relay.iterm, "send", return_value=True) as send:
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(tmp_path),
                                           heavy_override=None))
        send.assert_called_once()
        events = self._ledger_events(relay)
        assert not [e for e in events if e["event"] == "heavy_override"]

    def test_unlocatable_transcript_never_gates(self, relay, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        (tmp_path / "cfg" / "projects").mkdir(parents=True)
        self._mk(relay, claude_session="cs-nowhere")
        with mock.patch.object(relay.iterm, "send", return_value=True) as send:
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(tmp_path),
                                           heavy_override=None))
        send.assert_called_once()

    def test_missing_heavy_override_attr_treated_as_none(self, relay, tmp_path, monkeypatch):
        # callers that predate this flag (or construct args without it) must not crash — getattr
        # default, same defensive style as args.all/args.closed elsewhere in cmd_list.
        self._transcript(tmp_path, monkeypatch, "cs-x", 0.5)
        self._mk(relay)
        with mock.patch.object(relay.iterm, "send", return_value=True) as send:
            relay.cmd_send(SimpleNamespace(session_id="e1", packet=self._packet(tmp_path)))
        send.assert_called_once()


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


class TestLabelHoldsForTab:
    """_label_holds_for_tab: "is MY tab called this?" (identity) vs the old "is ANY tab called
    this?" (label) — backlog §2. The bounded matcher itself (iterm.title_is_live) is left REAL
    throughout; only the title READ is mocked."""

    def test_matching_title_on_my_tab_holds(self, relay):
        with mock.patch.object(relay.iterm_backend, "title_by_id", return_value="[Exec] e1") as by_id, \
             mock.patch.object(relay.iterm_backend, "live_session_names") as any_tab:
            assert relay._label_holds_for_tab("h1", "[Exec] e1") is True
        by_id.assert_called_once_with("h1")
        any_tab.assert_not_called()      # identity answered it; no any-tab scan

    def test_matches_claude_status_suffix_on_my_tab(self, relay):
        # Claude Code appends its own status text; the real bounded matcher must still say yes.
        title = "[Exec] e1" + relay.iterm_backend.TAB_TITLE_SEP + "working"
        with mock.patch.object(relay.iterm_backend, "title_by_id", return_value=title):
            assert relay._label_holds_for_tab("h1", "[Exec] e1") is True

    def test_different_title_on_my_tab_does_not_hold(self, relay):
        with mock.patch.object(relay.iterm_backend, "title_by_id", return_value="claude"):
            assert relay._label_holds_for_tab("h1", "[Exec] e1") is False

    def test_sibling_tab_with_my_label_does_not_count(self, relay):
        # THE §2 CASE: a sibling carries my label byte-for-byte, my own tab does not. The old
        # any-tab scan said True here and the tab was never re-titled.
        with mock.patch.object(relay.iterm_backend, "title_by_id", return_value="claude"), \
             mock.patch.object(relay.iterm_backend, "live_session_names",
                               return_value={"[Lead] webapp", "claude"}):
            assert relay._label_holds_for_tab("mine", "[Lead] webapp") is False

    def test_unreadable_tab_does_not_count_as_labeled(self, relay):
        # None = "couldn't read it" (closed tab, or a failed probe). An unreadable tab is not a
        # correctly-titled tab: say False so the caller re-asserts (a rename against a gone session
        # is a harmless no-op) rather than declaring victory over a tab nobody can see.
        with mock.patch.object(relay.iterm_backend, "title_by_id", return_value=None):
            assert relay._label_holds_for_tab("h1", "[Exec] e1") is False

    def test_without_handle_falls_back_to_any_tab_scan(self, relay):
        # Legacy/pre-capture sessions have no identity to ask about — the old behavior is the
        # fallback, not the default.
        with mock.patch.object(relay.iterm_backend, "title_by_id") as by_id, \
             mock.patch.object(relay.iterm_backend, "live_session_names",
                               return_value={"[Exec] e1"}) as any_tab:
            assert relay._label_holds_for_tab(None, "[Exec] e1") is True
        by_id.assert_not_called()
        any_tab.assert_called_once()

    def test_without_handle_fallback_can_still_say_no(self, relay):
        with mock.patch.object(relay.iterm_backend, "live_session_names", return_value={"claude"}):
            assert relay._label_holds_for_tab(None, "[Exec] e1") is False


class TestLeadTabTarget:
    """_lead_tab_target: resolve a LEAD marker to (backend, label, handle). Both identities come
    from the marker — the backend that hosts the tab, and the handle that identifies it within that
    backend — so no lead-tab call site has to remember to pass the handle (backlog §2)."""

    def test_uses_recorded_backend_and_handle(self, relay):
        marker = {"tab_label": "[Lead] webapp", "iterm_session": "w0t0p0:X", "backend": "terminal"}
        bk, label, handle = relay._lead_tab_target(marker)
        assert bk.NAME == "terminal"          # the marker's backend, not the caller's ambient one
        assert (label, handle) == ("[Lead] webapp", "w0t0p0:X")

    def test_probes_when_backend_unrecorded(self, relay):
        # Pre-stamping markers: probe which backend actually has the tab rather than guessing.
        marker = {"tab_label": "[Lead] webapp", "iterm_session": "w0t0p0:X"}
        probed = mock.Mock(NAME="probed")
        with mock.patch.object(relay, "_probe_backend_for_tab", return_value=probed) as probe:
            bk, _label, _handle = relay._lead_tab_target(marker)
        assert bk is probed
        probe.assert_called_once_with("[Lead] webapp", "w0t0p0:X")

    def test_ambient_fallback_when_probe_is_ambiguous(self, relay):
        marker = {"tab_label": "[Lead] webapp"}
        with mock.patch.object(relay, "_probe_backend_for_tab", return_value=None):
            bk, _label, handle = relay._lead_tab_target(marker)
        assert bk is relay.iterm            # last resort, unchanged from before
        assert handle is None               # handle-less marker stays handle-less


class TestLeadTabIdentityAddressing:
    """The two lead-tab call sites that used to address by LABEL ALONE while the handle sat unused
    on the marker (backlog §2): `relay focus <lead>` and cmd_resume_lead's already-alive guard. A
    handoff pair shares a tab title byte-for-byte, so label-only could act on the WRONG lead."""

    def test_focus_lead_passes_handle_to_the_recorded_backend(self, relay, capsys):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd="/w",
                                      tab_label="[Lead] webapp", iterm_session="w0t0p0:MINE",
                                      backend="iterm")
        with mock.patch.object(relay.iterm, "focus", return_value=True) as focus:
            relay.cmd_focus(SimpleNamespace(session_id="lead-1"))
        focus.assert_called_once_with("[Lead] webapp", "w0t0p0:MINE")
        assert "focused lead 'webapp'" in capsys.readouterr().out

    def test_focus_lead_without_handle_still_works(self, relay):
        # Legacy marker (no captured handle): label-only, exactly as before — the fallback must not
        # regress into "can't focus pre-capture leads".
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd="/w",
                                      tab_label="[Lead] webapp", backend="iterm")
        with mock.patch.object(relay.iterm, "focus", return_value=True) as focus:
            relay.cmd_focus(SimpleNamespace(session_id="lead-1"))
        focus.assert_called_once_with("[Lead] webapp", None)

    def test_resume_lead_alive_guard_is_identity_aware(self, relay):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd="/w",
                                      tab_label="[Lead] webapp", iterm_session="w0t0p0:MINE",
                                      backend="iterm")
        with mock.patch.object(relay.iterm, "spawn"), \
             mock.patch.object(relay, "read_iterm_id_at", return_value=None), \
             mock.patch.object(relay, "pid_alive", return_value=False), \
             mock.patch.object(relay.iterm, "is_alive", return_value=False) as is_alive:
            relay.cmd_resume_lead("lead-1")
        is_alive.assert_called_once_with("[Lead] webapp", "w0t0p0:MINE")

    def test_resume_lead_refuses_when_its_own_tab_is_alive(self, relay):
        # The guard still guards — identity-aware doesn't mean permissive.
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd="/w",
                                      tab_label="[Lead] webapp", iterm_session="w0t0p0:MINE",
                                      backend="iterm")
        with mock.patch.object(relay.iterm, "spawn") as spawn, \
             mock.patch.object(relay, "pid_alive", return_value=False), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            with pytest.raises(SystemExit):
                relay.cmd_resume_lead("lead-1")
        spawn.assert_not_called()


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

    # These drive the loop through the IDENTITY seam it now reads (iterm.title_by_id — "what is MY
    # tab called", §2) instead of the old any-tab scan. title_is_live is deliberately left REAL so
    # the bounded match is exercised for real rather than stubbed to a bare True/False.

    def test_noop_when_already_labeled_two_checks_in_a_row(self, relay):
        with mock.patch.object(relay.iterm_backend, "title_by_id", return_value="[Exec] e1"), \
             mock.patch.object(relay.iterm_backend, "rename_by_id") as rename:
            ok = relay._background_label_loop(
                "h1", "[Exec] e1", window=30, interval=3,
                clock_fn=self._fake_clock([0, 1, 2, 3, 4, 5]), sleep_fn=lambda s: None)
        assert ok is True
        rename.assert_not_called()

    def test_reasserts_until_clobber_fixed_then_holds(self, relay):
        # First two polls see MY tab's title clobbered away from [Exec]; from the third on it reads
        # [Exec] again (as if the background rename just landed) and must hold for 2 in a row.
        reads = ["claude", "claude", "[Exec] e1", "[Exec] e1", "[Exec] e1"]
        with mock.patch.object(relay.iterm_backend, "title_by_id", side_effect=reads), \
             mock.patch.object(relay.iterm_backend, "rename_by_id") as rename:
            ok = relay._background_label_loop(
                "h1", "[Exec] e1", window=30, interval=3,
                clock_fn=self._fake_clock([0, 1, 2, 3, 4, 5, 6]), sleep_fn=lambda s: None)
        assert ok is True
        assert rename.call_count == 2  # one per clobbered read
        rename.assert_called_with("h1", "[Exec] e1")

    def test_gives_up_after_window_if_never_holds(self, relay):
        with mock.patch.object(relay.iterm_backend, "title_by_id", return_value="claude"), \
             mock.patch.object(relay.iterm_backend, "rename_by_id") as rename:
            ok = relay._background_label_loop(
                "h1", "[Exec] e1", window=10, interval=3,
                clock_fn=self._fake_clock([0, 3, 6, 9, 12]), sleep_fn=lambda s: None)
        assert ok is False
        assert rename.call_count >= 1

    def test_ignores_an_identically_titled_sibling_tab(self, relay):
        """§2, the whole point: a handoff pair shares `[Lead] <project>` byte-for-byte. MY tab is
        still called "claude"; the SIBLING's is correctly titled. The old any-tab scan saw the
        sibling and declared victory, leaving my tab mislabeled forever (its wake silently dead)."""
        titles = {"mine": "claude", "sibling": "[Lead] webapp"}

        def rename(handle, new_name):
            titles[handle] = new_name
            return True

        with mock.patch.object(relay.iterm_backend, "title_by_id", side_effect=titles.get), \
             mock.patch.object(relay.iterm_backend, "live_session_names",
                               side_effect=lambda: set(titles.values())) as any_tab, \
             mock.patch.object(relay.iterm_backend, "rename_by_id", side_effect=rename):
            ok = relay._background_label_loop(
                "mine", "[Lead] webapp", window=30, interval=3,
                clock_fn=self._fake_clock([0, 1, 2, 3, 4, 5, 6]), sleep_fn=lambda s: None)
        assert ok is True
        assert titles["mine"] == "[Lead] webapp"     # MY tab got titled, not just some tab
        assert titles["sibling"] == "[Lead] webapp"  # the sibling was never touched
        any_tab.assert_not_called()                  # never fell back to the any-tab scan


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
