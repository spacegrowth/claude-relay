"""
Layer 1 (pure Python, no real AppleScript, CI-able) unit tests for the terminal backends:
iterm.py's shared command builder + tab-color escapes, terminal_app.py's window-id addressing
(osascript mocked, scripts inspected), and backend.py's selection order.

Run: pytest tests/test_backends.py -v
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "lib"))
import backend      # noqa: E402
import iterm        # noqa: E402
import terminal_app  # noqa: E402
import lead_guard   # noqa: E402


def _ok(stdout):
    return subprocess.CompletedProcess(["osascript"], 0, stdout, "")


class TestBuildClaudeCmd:
    def test_fresh_session_pins_uuid_and_model(self):
        cmd = iterm.build_claude_cmd("do it", model="sonnet", session_uuid="u-1")
        assert "--session-id u-1" in cmd and "--model sonnet" in cmd and "'do it'" in cmd

    def test_resume_has_no_model(self):
        # A resumed conversation already has a model — passing one again would be rejected/ignored.
        cmd = iterm.build_claude_cmd("continue", model="sonnet", resume_id="cs-1")
        assert "--resume cs-1" in cmd and "--model" not in cmd

    def test_skip_perms_flag(self):
        assert "--dangerously-skip-permissions" in iterm.build_claude_cmd("x", skip_perms=True)
        assert "--dangerously-skip-permissions" not in iterm.build_claude_cmd("x")


class TestTabColor:
    def test_escape_bytes_carry_rgb(self):
        esc = iterm.tab_color_escape((255, 105, 97))
        assert "\033]6;1;bg;red;brightness;255\a" in esc
        assert "\033]6;1;bg;green;brightness;105\a" in esc
        assert "\033]6;1;bg;blue;brightness;97\a" in esc

    def test_printf_form_has_no_raw_control_bytes(self):
        p = iterm.tab_color_printf((1, 2, 3))
        assert "\\033]6;1;bg;red;brightness;1\\a" in p
        assert "\033" not in p and "\a" not in p  # printf-safe: literal backslashes only

    def test_spawn_embeds_printf_when_colored(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid",
                        tab_color=(9, 8, 7), rename_delay=0)
        script = osa_run.call_args[0][0]
        # The cmd is embedded in an AppleScript string literal, so osa() doubles the backslashes:
        # script carries \\033, AppleScript unescapes to \033, printf turns that into ESC.
        assert "printf '\\\\033]6;1;bg;red;brightness;9" in script

    def test_spawn_omits_printf_without_color(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0)
        assert "printf" not in osa_run.call_args[0][0]


class TestSpawnLeadWindow:
    def test_with_lead_handle_walks_sessions_and_targets_matched_window(self):
        # Python-API placement explicitly disabled (returns None) so this test exercises the pure
        # AppleScript fallback path deterministically — regardless of whether THIS machine happens
        # to have a real, working iTerm2 Python API connection available (see TestPyApiHybrid for
        # the Python-API-succeeds case).
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run, \
             mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab", return_value=None):
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        lead_handle="w1t2p0:LEAD-UUID")
        script = osa_run.call_args[0][0]
        assert 'if (id of s) is "LEAD-UUID" then' in script
        assert "set leadWindow to w" in script
        assert "set foundLeadWindow to true" in script
        assert "if foundLeadWindow then" in script
        assert "tell leadWindow to create tab with default profile" in script
        # fallback branches for when the lead's session isn't found must still be present
        assert "tell current window to create tab with default profile" in script

    def test_without_lead_handle_matches_todays_shape(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0)
        script = osa_run.call_args[0][0]
        assert "foundLeadWindow" not in script
        assert "leadWindow" not in script
        assert "  if (count of windows) is 0 then\n" in script
        assert "    tell current window to create tab with default profile\n" in script


class TestSpawnPaneLayout:
    def test_pane_with_lead_handle_splits_matched_session(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        lead_handle="w1t2p0:LEAD-UUID", layout="pane")
        script = osa_run.call_args[0][0]
        assert 'if (id of s) is "LEAD-UUID" then' in script
        assert "set leadSession to s" in script
        assert "set foundLeadWindow to true" in script
        assert "tell leadSession to set targetSession to (split vertically with default profile)" in script
        # tab-creation verb must NOT appear — pane layout never creates a tab when the lead is found
        assert "tell leadWindow to create tab with default profile" not in script
        # fallback branches for when the lead's session isn't found must still be present
        assert "tell current window to create tab with default profile" in script

    def test_pane_without_lead_handle_degrades_to_tab_shape(self):
        # no lead_handle at all → no session to split against, same as today's tab-only shape
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        layout="pane")
        script = osa_run.call_args[0][0]
        assert "split vertically" not in script
        assert "  if (count of windows) is 0 then\n" in script
        assert "    tell current window to create tab with default profile\n" in script

    def test_default_layout_is_byte_identical_to_tab(self):
        # layout="tab" (the default) must produce the exact same script as omitting layout entirely
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run, \
             mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab", return_value=None):
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        lead_handle="w1t2p0:LEAD-UUID")
        default_script = osa_run.call_args[0][0]
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run2, \
             mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab", return_value=None):
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        lead_handle="w1t2p0:LEAD-UUID", layout="tab")
        assert osa_run2.call_args[0][0] == default_script


class TestFocusPaneSelect:
    def test_focus_selects_window_tab_and_session(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("true")) as osa_run:
            iterm.focus("l")
        script = osa_run.call_args[0][0]
        assert "tell w to select" in script
        assert "select t" in script
        assert "tell s to select" in script


class TestIdBasedClose:
    """close()/is_alive() must target by unique iTerm session id (`handle`) when one is given,
    falling back to the bounded title match only when handle is empty or the id lookup finds
    nothing. Regression: two live tabs (a handoff predecessor/successor pair) can share the exact
    same title, so title-only targeting is a coin flip about which tab gets closed."""

    def test_close_with_handle_targets_id_only(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("true")) as osa_run:
            closed = iterm.close("[Lead] webapp", "w1t5p0:SOME-UUID", None)
        assert closed is True
        assert osa_run.call_count == 1
        script = osa_run.call_args[0][0]
        assert 'id of s) is "SOME-UUID"' in script
        assert "tell s to close" in script

    def test_close_with_handle_falls_back_to_title_when_id_not_found(self):
        with mock.patch.object(iterm, "run_osascript", side_effect=[_ok("false"), _ok("true")]) as osa_run:
            closed = iterm.close("[Lead] webapp", "w1t5p0:SOME-UUID", None)
        assert closed is True
        assert osa_run.call_count == 2
        title_script = osa_run.call_args_list[1][0][0]
        assert "name of s is equal to" in title_script

    def test_close_without_handle_uses_title_match_directly(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("true")) as osa_run:
            closed = iterm.close("[Lead] webapp")
        assert closed is True
        assert osa_run.call_count == 1
        script = osa_run.call_args[0][0]
        assert "name of s is equal to" in script
        assert "id of s" not in script

    def test_is_alive_with_handle_short_circuits_on_id_match(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("true")) as osa_run, \
             mock.patch.object(iterm, "title_is_live") as title_is_live:
            alive = iterm.is_alive("[Lead] webapp", "w1t5p0:SOME-UUID")
        assert alive is True
        title_is_live.assert_not_called()
        assert osa_run.call_count == 1

    def test_is_alive_falls_back_to_title_when_id_not_found(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("false")), \
             mock.patch.object(iterm, "running", return_value=True), \
             mock.patch.object(iterm, "live_session_names", return_value={"[Lead] webapp"}):
            alive = iterm.is_alive("[Lead] webapp", "w1t5p0:SOME-UUID")
        assert alive is True

    def test_is_alive_without_handle_uses_title_match_only(self):
        with mock.patch.object(iterm, "run_osascript") as osa_run, \
             mock.patch.object(iterm, "live_session_names", return_value={"[Lead] webapp"}):
            alive = iterm.is_alive("[Lead] webapp")
        assert alive is True
        osa_run.assert_not_called()   # no id lookup attempted at all without a handle


class TestIdBasedSend:
    """send() must target by unique iTerm session id (`handle`) when one is given, falling back to
    the bounded title match only when handle is empty or the id lookup finds nothing -- same
    reasoning as TestIdBasedClose: once Claude Code's own OSC titling clobbers a tab's title,
    title-match addressing misfires, which is what caused an earlier `relay send` to report 'tab
    was gone -- resumed' on a live executor."""

    def test_send_with_handle_targets_id_only(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("true")) as osa_run:
            ok = iterm.send("[Exec] e1", "do the thing", "w1t5p0:SOME-UUID")
        assert ok is True
        assert osa_run.call_count == 1
        script = osa_run.call_args[0][0]
        assert 'id of s) is "SOME-UUID"' in script
        assert 'write text "do the thing" newline NO' in script

    def test_send_with_handle_finds_session_even_when_title_is_not_exec(self):
        # The whole point: the live tab's title has already been clobbered away from "[Exec] e1"
        # by Claude's own OSC titling, but the id-based lookup still finds and writes to it.
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("true")) as osa_run:
            ok = iterm.send("[Exec] e1", "do the thing", "w1t5p0:SOME-UUID")
        assert ok is True
        script = osa_run.call_args[0][0]
        assert "name of s" not in script   # id path never even builds a title predicate

    def test_send_with_handle_falls_back_to_title_when_id_not_found(self):
        with mock.patch.object(iterm, "run_osascript", side_effect=[_ok("false"), _ok("true")]) as osa_run:
            ok = iterm.send("[Exec] e1", "do the thing", "w1t5p0:SOME-UUID")
        assert ok is True
        assert osa_run.call_count == 2
        title_script = osa_run.call_args_list[1][0][0]
        assert "name of s is equal to" in title_script

    def test_send_without_handle_uses_title_match_directly(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("true")) as osa_run:
            ok = iterm.send("[Exec] e1", "do the thing")
        assert ok is True
        assert osa_run.call_count == 1
        script = osa_run.call_args[0][0]
        assert "name of s is equal to" in script
        assert "id of s" not in script


class TestIdBasedFocus:
    """focus() must target by unique iTerm session id (`handle`) when one is given, falling back
    to the bounded title match only when handle is empty or the id lookup finds nothing."""

    def test_focus_with_handle_targets_id_only(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("true")) as osa_run:
            ok = iterm.focus("[Exec] e1", "w1t5p0:SOME-UUID")
        assert ok is True
        assert osa_run.call_count == 1
        script = osa_run.call_args[0][0]
        assert 'id of s) is "SOME-UUID"' in script
        assert "activate" in script
        assert "tell s to select" in script

    def test_focus_with_handle_falls_back_to_title_when_id_not_found(self):
        with mock.patch.object(iterm, "run_osascript", side_effect=[_ok("false"), _ok("true")]) as osa_run:
            ok = iterm.focus("[Exec] e1", "w1t5p0:SOME-UUID")
        assert ok is True
        assert osa_run.call_count == 2
        title_script = osa_run.call_args_list[1][0][0]
        assert "name of s is equal to" in title_script

    def test_focus_without_handle_uses_title_match_directly(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("true")) as osa_run:
            ok = iterm.focus("[Exec] e1")
        assert ok is True
        assert osa_run.call_count == 1
        script = osa_run.call_args[0][0]
        assert "name of s is equal to" in script
        assert "id of s" not in script


class TestPidOnTty:
    def test_matches_pid_by_tty_and_comm(self):
        ps_out = "  123 ttys000 login\n  456 ttys000 claude\n  789 ttys001 claude\n"
        with mock.patch.object(iterm.subprocess, "run",
                                return_value=subprocess.CompletedProcess(["ps"], 0, ps_out, "")):
            assert iterm.pid_on_tty("/dev/ttys000") == 456

    def test_no_match_returns_none(self):
        ps_out = "  123 ttys002 claude\n"
        with mock.patch.object(iterm.subprocess, "run",
                                return_value=subprocess.CompletedProcess(["ps"], 0, ps_out, "")):
            assert iterm.pid_on_tty("/dev/ttys000") is None

    def test_empty_tty_path_returns_none_without_running_ps(self):
        with mock.patch.object(iterm.subprocess, "run") as run_mock:
            assert iterm.pid_on_tty(None) is None
        run_mock.assert_not_called()


class TestPyApiHybrid:
    """spawn()'s hybrid placement: when iterm_pyapi.try_create_adjacent_tab succeeds (package
    installed, API enabled, lead session found), its returned session id is handed to the
    EXISTING AppleScript write-text/rename machinery via _target_by_session_id_block — placement
    only, no other behavior changes. Any failure (None) falls back to _create_target_block exactly
    as it did before this feature existed (see TestSpawnLeadWindow)."""

    def test_python_path_chosen_when_pyapi_succeeds(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run, \
             mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab",
                               return_value="NEW-TAB-SESSION-ID") as try_adjacent:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        lead_handle="w1t2p0:LEAD-UUID", layout="tab")
        try_adjacent.assert_called_once_with("w1t2p0:LEAD-UUID")
        script = osa_run.call_args[0][0]
        assert 'if (id of s) is "NEW-TAB-SESSION-ID" then' in script
        assert "set targetSession to s" in script
        # the AppleScript-only fallback machinery (window/session walk-and-branch) must NOT run
        assert "foundLeadWindow" not in script
        assert "leadWindow" not in script
        assert "create tab with default profile" not in script  # pyapi already made the tab

    def test_appl_script_path_unchanged_when_pyapi_returns_none(self):
        # Mirrors TestSpawnLeadWindow's fallback shape assertions — confirms the hybrid wiring
        # doesn't alter the fallback script AT ALL when placement fails for any reason.
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run, \
             mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab", return_value=None):
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        lead_handle="w1t2p0:LEAD-UUID", layout="tab")
        script = osa_run.call_args[0][0]
        assert "set leadWindow to w" in script
        assert "tell leadWindow to create tab with default profile" in script

    def test_pyapi_not_attempted_for_pane_layout(self):
        # Panes are inherently adjacent — no placement problem to solve, so the (possibly slow)
        # Python API connection attempt must not even be made.
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run, \
             mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab") as try_adjacent:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        lead_handle="w1t2p0:LEAD-UUID", layout="pane")
        try_adjacent.assert_not_called()

    def test_pyapi_not_attempted_without_lead_handle(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run, \
             mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab") as try_adjacent:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0)
        try_adjacent.assert_not_called()

    def test_import_blocked_end_to_end_produces_unchanged_appl_script(self, monkeypatch):
        # The real availability-gate, exercised through spawn() itself (not a mocked
        # try_create_adjacent_tab): with the `iterm2` package import genuinely blocked,
        # try_create_adjacent_tab degrades to None internally, and the generated AppleScript is
        # BYTE IDENTICAL to the pre-Python-API script shape (packet 007's zero-new-hard-dependency
        # requirement).
        monkeypatch.setitem(sys.modules, "iterm2", None)
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        lead_handle="w1t2p0:LEAD-UUID", layout="tab")
        script = osa_run.call_args[0][0]
        assert "set leadWindow to w" in script
        assert "tell leadWindow to create tab with default profile" in script
        # the pyapi-success-only assignment ("set targetSession to s", from
        # _target_by_session_id_block) must be absent — only the fallback shape's
        # "set targetSession to current session of leadWindow" should appear.
        assert "set targetSession to s\n" not in script


class TestLeadColor:
    def test_stable_and_in_palette(self):
        c1 = lead_guard.lead_color("lead-abc")
        c2 = lead_guard.lead_color("lead-abc")
        assert c1 == c2
        assert tuple(c1) in lead_guard.TAB_PALETTE

    def test_different_leads_can_differ(self):
        # Not guaranteed distinct (6-way hash), but these two known ids must not both collide
        # with everything — sanity that the hash actually varies.
        colors = {tuple(lead_guard.lead_color(f"lead-{i}")) for i in range(12)}
        assert len(colors) > 1


class TestTerminalAppBackend:
    def test_wid_parses_only_own_handles(self):
        assert terminal_app._wid("twid:42") == "42"
        assert terminal_app._wid("w0t0p0:UUID") is None   # foreign (iTerm) handle
        assert terminal_app._wid(None) is None
        assert terminal_app._wid("twid:nope") is None

    def test_spawn_captures_window_id_to_handle_file(self, tmp_path):
        handle_file = tmp_path / "handle"
        with mock.patch.object(terminal_app, "run_osascript", return_value=_ok("77\n")) as osa_run, \
             mock.patch.object(terminal_app, "rename_by_id", return_value=True) as ren:
            terminal_app.spawn(cwd="/tmp", prompt="p", label="[Exec] e1",
                               pidfile=str(tmp_path / "pid"), iterm_id_file=str(handle_file))
        script = osa_run.call_args[0][0]
        assert 'tell application "Terminal"' in script and "do script" in script
        # The window id is resolved from the NEW tab's tty (never "front window" — that races with
        # any window mid-close, and tabs have no scriptable `window` property).
        assert "tty of t" in script and "id of w" in script
        assert handle_file.read_text() == "twid:77"
        ren.assert_called_once_with("twid:77", "[Exec] e1")

    def test_spawn_ignores_tab_color(self, tmp_path):
        # Terminal.app has no tab colors — the shared kwarg must be accepted and produce no printf.
        with mock.patch.object(terminal_app, "run_osascript", return_value=_ok("5")) as osa_run, \
             mock.patch.object(terminal_app, "rename_by_id"):
            terminal_app.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid",
                               tab_color=(1, 2, 3))
        assert "printf" not in osa_run.call_args[0][0]

    def test_spawn_accepts_lead_handle_without_error(self, tmp_path):
        # Terminal.app addresses by window, not adjacent tabs — lead_handle is accepted (shared
        # backend signature) and has no effect on the generated script.
        with mock.patch.object(terminal_app, "run_osascript", return_value=_ok("5")) as osa_run, \
             mock.patch.object(terminal_app, "rename_by_id"):
            terminal_app.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid",
                               lead_handle="w1t2p0:LEAD-UUID")
        assert "LEAD-UUID" not in osa_run.call_args[0][0]

    def test_spawn_accepts_layout_without_split_verb(self, tmp_path):
        # Terminal.app has no split-pane scripting surface — layout is accepted (shared backend
        # signature) and produces no split verb in the generated script.
        with mock.patch.object(terminal_app, "run_osascript", return_value=_ok("5")) as osa_run, \
             mock.patch.object(terminal_app, "rename_by_id"):
            terminal_app.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", layout="pane")
        assert "split" not in osa_run.call_args[0][0]

    def test_send_never_injects(self):
        # Terminal.app cannot inject into a running process (verified live: `do script … in tab`
        # queues a SHELL command, the running claude receives nothing). send() must refuse without
        # even attempting osascript — relay then routes through its resume-fallback delivery.
        with mock.patch.object(terminal_app, "run_osascript") as osa_run:
            assert terminal_app.send("label", "hello", "twid:9") is False
            assert terminal_app.send("label", "hello", None) is False
        osa_run.assert_not_called()

    def test_is_alive_checks_window_exists(self):
        with mock.patch.object(terminal_app, "running", return_value=True), \
             mock.patch.object(terminal_app, "run_osascript", return_value=_ok("true")):
            assert terminal_app.is_alive("l", "twid:3") is True
        with mock.patch.object(terminal_app, "running", return_value=True), \
             mock.patch.object(terminal_app, "run_osascript", return_value=_ok("false")):
            assert terminal_app.is_alive("l", "twid:3") is False

    def test_close_and_focus_address_window(self):
        for fn in (terminal_app.close, terminal_app.focus):
            with mock.patch.object(terminal_app, "run_osascript", return_value=_ok("true")) as osa_run:
                assert fn("l", "twid:4") is True
            assert "window id 4" in osa_run.call_args[0][0]
            with mock.patch.object(terminal_app, "run_osascript") as osa_run:
                assert fn("l", None) is False
            osa_run.assert_not_called()

    def test_tty_by_id_always_none(self):
        assert terminal_app.tty_by_id("twid:1") is None  # no tab colors on Terminal.app


class TestBackendSelection:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("RELAY_TERMINAL", "terminal")
        assert backend.select() is terminal_app
        monkeypatch.setenv("RELAY_TERMINAL", "iterm")
        assert backend.select() is iterm

    def test_term_program_autodetect(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RELAY_TERMINAL", raising=False)
        # Point config lookup at an empty home so the real ~/.relay-tasks config can't interfere.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
        assert backend.select() is terminal_app
        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
        assert backend.select() is iterm

    def test_by_name_and_unknown(self):
        assert backend.by_name("iterm") is iterm
        assert backend.by_name("terminal") is terminal_app
        assert backend.by_name(None) is None
        assert backend.by_name("kitty") is None
