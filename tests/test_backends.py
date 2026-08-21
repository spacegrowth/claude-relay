"""
Layer 1 (pure Python, no real AppleScript, CI-able) unit tests for the terminal backends:
iterm.py's shared command builder + tab-color escapes, terminal_app.py's window-id addressing
(osascript mocked, scripts inspected), and backend.py's selection order.

Run: pytest tests/test_backends.py -v
"""
import os
import shlex
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

    def test_spawn_embeds_printf_when_colored(self, tmp_path):
        # §15b: the cmd (printf included) lives in the bootstrap FILE now, not the typed script —
        # the typed line must stay short and quote-free. Same invariant, new surface.
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")):
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile=str(tmp_path / "pid"),
                        tab_color=(9, 8, 7), rename_delay=0)
        boot = (tmp_path / iterm.BOOTSTRAP_FILENAME).read_text()
        assert "printf '\\033]6;1;bg;red;brightness;9" in boot

    def test_spawn_omits_printf_without_color(self, tmp_path):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile=str(tmp_path / "pid"),
                        rename_delay=0)
        # "printf '" (the invocation, quote included) — pytest's tmp dir is named after this very
        # test, so the bare word "printf" legitimately appears in the bootstrap PATH in the script.
        assert "printf '" not in osa_run.call_args[0][0]
        assert "printf '" not in (tmp_path / iterm.BOOTSTRAP_FILENAME).read_text()


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
        assert "tell leadWindow to set newTab to (create tab with default profile)" in script
        # fallback branches for when the lead's session isn't found must still be present
        assert "tell current window to set newTab to (create tab with default profile)" in script

    def test_without_lead_handle_matches_todays_shape(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0)
        script = osa_run.call_args[0][0]
        assert "foundLeadWindow" not in script
        assert "leadWindow" not in script
        assert "  if (count of windows) is 0 then\n" in script
        assert "    tell current window to set newTab to (create tab with default profile)\n" in script


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
        assert "create tab with default profile" not in script.split("else if (count of windows)")[0]
        # fallback branches for when the lead's session isn't found must still be present
        assert "tell current window to set newTab to (create tab with default profile)" in script

    def test_pane_without_lead_handle_degrades_to_tab_shape(self):
        # no lead_handle at all → no session to split against, same as today's tab-only shape
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                        layout="pane")
        script = osa_run.call_args[0][0]
        assert "split vertically" not in script
        assert "  if (count of windows) is 0 then\n" in script
        assert "    tell current window to set newTab to (create tab with default profile)\n" in script

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


class TestTitleById:
    """title_by_id: the identity-aware title read (backlog §2). live_session_names answers "does
    ANY tab carry this title", which is the wrong question when two tabs legitimately share a label
    (a handoff predecessor/successor pair). This asks ONE session what it is currently called."""

    def test_returns_that_sessions_title(self):
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("[Lead] webapp\n")) as osa_run:
            assert iterm.title_by_id("w1t5p0:SOME-UUID") == "[Lead] webapp"
        script = osa_run.call_args[0][0]
        assert 'id of s) is "SOME-UUID"' in script     # matched by identity, not by title
        assert "name of s" in script

    def test_no_handle_is_none_without_calling_osascript(self):
        with mock.patch.object(iterm, "run_osascript") as osa_run:
            assert iterm.title_by_id(None) is None
            assert iterm.title_by_id("") is None
        osa_run.assert_not_called()

    def test_unresolved_session_is_none(self):
        # The AppleScript's `return ""` fallthrough: no session has that id (tab closed).
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("")):
            assert iterm.title_by_id("w1t5p0:GONE") is None

    def test_osascript_failure_is_none(self):
        with mock.patch.object(iterm, "run_osascript",
                                return_value=subprocess.CompletedProcess(["osascript"], 1, "", "boom")):
            assert iterm.title_by_id("w1t5p0:SOME-UUID") is None


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
        assert "tell leadWindow to set newTab to (create tab with default profile)" in script

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
        assert "tell leadWindow to set newTab to (create tab with default profile)" in script
        # the pyapi-success-only assignment ("set targetSession to s", from
        # _target_by_session_id_block) must be absent — only the fallback shape's
        # "set targetSession to current session of newTab" should appear.
        assert "set targetSession to s\n" not in script
        assert "set targetSession to current session of newTab" in script


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


# ================================================================================================
# 2026-08-01 SPAWN-MISFIRE INCIDENT (~/.relay-tasks/incident-spawn-misfire-2026-08-01.md)
#
# A lead's `relay spawn --name alerts-badge` typed its ENTIRE bootstrap — `cd … && printf … &&
# echo $$ > …/pid && exec claude --dangerously-skip-permissions --session-id 290f5187-… '<packet>'`
# plus a follow-up `/rename [Exec] alerts-badge` — into an unrelated live lead's tab. relay's own
# #20 no-PID/no-title check DETECTED it and retried cleanly, but only AFTER the payload had already
# landed somewhere, and it never said where. The trigger (confirmed by Vamsi): he was cycling iTerm
# tabs while the spawn ran. Damage was luck-dependent — the victim tab happened to be running a
# Claude REPL, so the payload was inert text; in a plain SHELL the `exec claude --session-id <uuid>`
# would have replaced that shell with a duplicate executor on the same session id as the retry.
#
# The three classes below cover the incident's three asks in order.
# ================================================================================================


# The binding statements the PRE-FIX _create_target_block emitted, verbatim from scripts/iterm.py at
# commit 8620672. Kept here as the repro's input: this is the shape that misfired, and no fragment
# of the current code may resolve its target this way again.
PRE_FIX_TARGET_BLOCK = (
    "  if (count of windows) is 0 then\n"
    "    set newWindow to (create window with default profile)\n"
    "    set targetSession to current session of newWindow\n"
    "  else\n"
    "    tell current window to create tab with default profile\n"
    "    set targetSession to current session of current window\n"
    "  end if\n"
)


def _target_bindings(script):
    """Every `set targetSession to <expr>` right-hand side in an AppleScript fragment."""
    key = "set targetSession to "
    return [ln.strip()[len(key):] for ln in script.splitlines() if ln.strip().startswith(key)]


def _resolve_binding(expr, created, focused_at_read_time):
    """MODEL of how iTerm evaluates the target-binding expressions these blocks emit.

    Deliberately tiny and total: an expression it doesn't know raises, so this model cannot silently
    drift from the code it is asserting about. The one fact it encodes is the incident's root cause —
    `current session of <window>` is evaluated WHEN THAT STATEMENT RUNS and yields whatever tab is
    selected in that window at that instant, whereas a binding through the object `create …`
    RETURNED yields the created session no matter where focus went.

    (A model, not real AppleScript: exercising the true evaluation needs a live iTerm, which the
    packet forbids. It is the focus semantics that are modelled; the expressions come from the real
    code.)"""
    if expr in ("current session of newTab", "current session of newWindow",
                "(split vertically with default profile)", "s"):
        return created
    if expr in ("current session of current window", "current session of leadWindow"):
        return focused_at_read_time
    raise AssertionError(f"unmodelled target-binding expression: {expr!r} — extend the model")


class TestSpawnMisfireRepro:
    """Ask 1, repro half: the focus race, at the mock seam, against the pre-fix binding shape."""

    def test_repro_prefix_binding_sends_payload_to_the_focused_tab(self):
        # THE BUG. Tab creation made session NEW-EXEC, then the human selected another tab before
        # the next statement ran, so `current session of current window` resolved to the victim.
        created, victim = "NEW-EXEC-SESSION", "LEAD-DATA-PROVIDER-SESSION"
        bindings = _target_bindings(PRE_FIX_TARGET_BLOCK)
        resolved = [_resolve_binding(b, created, focused_at_read_time=victim) for b in bindings]
        assert victim in resolved, "expected the pre-fix shape to be redirectable by focus"
        # …and with focus left alone it looked perfectly healthy — which is why this shipped.
        assert all(r == created for r in
                   [_resolve_binding(b, created, focused_at_read_time=created) for b in bindings])

    def test_fixed_binding_is_immune_to_focus_moving(self):
        # Same race, current code: every binding the real spawn() emits resolves to the created
        # session even when focus moves to the victim between create and send.
        created, victim = "NEW-EXEC-SESSION", "LEAD-DATA-PROVIDER-SESSION"
        for kwargs in ({}, {"lead_handle": "w1t2p0:LEAD-UUID"},
                       {"lead_handle": "w1t2p0:LEAD-UUID", "layout": "pane"}):
            with mock.patch.object(iterm, "run_osascript", return_value=_ok("OK\nX\nY")) as osa_run, \
                 mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab", return_value=None):
                iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                            **kwargs)
            bindings = _target_bindings(osa_run.call_args[0][0])
            assert bindings, f"no target binding found for {kwargs}"
            for b in bindings:
                assert _resolve_binding(b, created, focused_at_read_time=victim) == created, \
                    f"focus-dependent binding {b!r} survived in the {kwargs} path"

    def test_no_focus_dependent_binding_survives_anywhere(self):
        # Belt and braces on the fragments directly, including the pyapi-placement path.
        fragments = [iterm._create_target_block(),
                     iterm._create_target_block("w1t2p0:LEAD-UUID", "tab"),
                     iterm._create_target_block("w1t2p0:LEAD-UUID", "pane"),
                     iterm._target_by_session_id_block("NEW-TAB-SESSION-ID")]
        for frag in fragments:
            assert "set targetSession to current session of current window" not in frag
            assert "set targetSession to current session of leadWindow" not in frag


class TestSpawnTargetOrAbort:
    """Ask 1, fix half: the payload goes to the re-checked session id or nowhere at all."""

    def _spawn(self, stdout, returncode=0):
        r = (_ok(stdout) if returncode == 0
             else subprocess.CompletedProcess(["osascript"], returncode, stdout, "boom"))
        with mock.patch.object(iterm, "run_osascript", return_value=r) as osa_run:
            out = iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0)
        return out, osa_run.call_args[0][0]

    def test_unresolvable_target_aborts_and_localizes(self):
        out, _ = self._spawn("NOTARGET\n\n[Lead] data_provider")
        assert out["ok"] is False and out["reason"] == "no-target"
        assert out["session_id"] is None
        assert out["front_title"] == "[Lead] data_provider"   # Ask 2's "which tab do I inspect"

    def test_target_that_vanished_before_the_send_aborts(self):
        out, _ = self._spawn("GONE\nDEAD-SESSION\n[Lead] data_provider")
        assert out["ok"] is False and out["reason"] == "target-gone"
        assert out["session_id"] == "DEAD-SESSION"

    def test_successful_send_reports_the_session_it_typed_into(self):
        out, _ = self._spawn("OK\nNEW-EXEC-SESSION\n[Exec] alerts-badge")
        assert out["ok"] is True and out["reason"] == "ok"
        assert out["session_id"] == "NEW-EXEC-SESSION"

    def test_osascript_failure_is_reported_as_indeterminate(self):
        out, _ = self._spawn("", returncode=1)
        assert out["ok"] is False and out["reason"] == "script-failed"

    def test_unresolved_target_aborts_before_any_write(self):
        # An unbound target still returns ahead of every `write text` in the script.
        _, script = self._spawn("OK\nS\nT")
        assert script.index("if not targetFound then return") < script.index("write text")

    def test_every_write_is_inside_an_id_match(self):
        # #25's structural invariant, and the reason the GONE verdict needs no separate re-check:
        # NO `write text` is reachable except from inside the loop iteration whose session id
        # matched `sid`. A write addressed any other way — notably through a held `targetSession`
        # reference — is what let the cutoff-jump payload land in the frontmost tab.
        _, script = self._spawn("OK\nS\nT")
        lines = script.splitlines()
        writes = [i for i, ln in enumerate(lines) if "write text" in ln]
        assert writes, "no write at all — the send would be a no-op"
        # Scan back to the nearest block boundary rather than a fixed window (§15b added a
        # sacrificial empty write + delay between the `if` and the launch-line write): the guard
        # holds iff an `if (id of s) is sid then` opens the enclosing block with no `end if`
        # in between. Stricter than the old 2-line window, not looser.
        for i in writes:
            opened = False
            for ln in reversed([l.strip() for l in lines[:i]]):
                if ln == "end if":
                    break
                if ln == "if (id of s) is sid then":
                    opened = True
                    break
            assert opened, f"write not inside an id match: {lines[i].strip()!r}"

    def test_no_write_addresses_a_held_reference(self):
        _, script = self._spawn("OK\nS\nT")
        assert "set sid to id of targetSession" in script      # resolved ONCE, immediately
        assert "tell targetSession" not in script              # …and never written through again

    def test_missed_walk_reports_gone_without_writing(self):
        # didWrite is set only inside the id match, so "the walk found nothing" and "nothing was
        # typed" are the same fact — that is what makes the GONE verdict trustworthy.
        _, script = self._spawn("OK\nS\nT")
        assert "set didWrite to false\n" in script
        assert "          set didWrite to true\n" in script
        assert 'if not didWrite then return "GONE"' in script

    def test_send_path_has_no_focus_dependent_addressing(self):
        # Packet #25 asks this explicitly: grep the emitted script for the focus-dependent forms.
        # The ONE permitted use is the frontmost-title capture for localization, which never
        # addresses a write — so it is excluded by line, not by weakening the check.
        _, script = self._spawn("OK\nS\nT")
        for ln in script.splitlines():
            if "set frontTitle to name of current session of current window" in ln:
                continue
            assert "current session of current window" not in ln, ln
            assert "front window" not in ln, ln

    def test_frontmost_title_is_captured_before_the_send(self):
        _, script = self._spawn("OK\nS\nT")
        assert script.index("set frontTitle to name of current session of current window") \
            < script.index("write text")


class TestBootstrapInertWhenMisdelivered:
    """Ask 3: a mis-delivered payload must be a no-op — including in a plain SHELL, the incident's
    real corruption mode. These tests execute the REAL payload with a real shell; no terminal app is
    involved anywhere."""

    TARGET = "TARGET-SESSION-UUID"

    def _bootstrap(self, tmp_path):
        """A bootstrap FILE shaped exactly like spawn()'s (§15b): pidfile via $$, then
        `exec <claude>`. The 'claude' here is a stub script that records that it ran, so an exec
        is observable. Returns (typed_line, marker, pidfile) — typed_line is what iTerm now types:
        `sh <file> <sid>`."""
        marker, pidfile = tmp_path / "claude-ran", tmp_path / "pid"
        stub = tmp_path / "fake-claude"
        stub.write_text(f"#!/bin/sh\necho \"$@\" > {shlex.quote(str(marker))}\n")
        stub.chmod(0o755)
        cmd = (f"cd {shlex.quote(str(tmp_path))} && echo $$ > {shlex.quote(str(pidfile))} "
               f"&& exec {shlex.quote(str(stub))} --session-id 290f5187 'do the packet'")
        boot = iterm.write_bootstrap_file(str(pidfile), cmd)
        return f"sh {boot} {self.TARGET}", marker, pidfile

    def _run(self, typed, session_env, shell="bash"):
        env = dict(os.environ)
        env.pop("ITERM_SESSION_ID", None)
        env.pop("TERM_SESSION_ID", None)
        if session_env is not None:
            env["ITERM_SESSION_ID"] = session_env
        return subprocess.run([shell, "-c", typed], capture_output=True, text=True, env=env,
                              timeout=30)

    @pytest.mark.parametrize("shell", ["bash", "zsh"])
    @pytest.mark.parametrize("wrong_session", [
        "w1t3p0:LEAD-DATA-PROVIDER-SESSION",   # the incident: a different live tab
        "",                                    # variable present but empty
        None,                                  # no session id at all (not a terminal we know)
    ])
    def test_misdelivered_payload_runs_nothing(self, tmp_path, shell, wrong_session):
        typed, marker, pidfile = self._bootstrap(tmp_path)
        r = self._run(typed, wrong_session, shell=shell)
        assert not marker.exists(), "the mis-delivered payload EXECED — the corruption mode is live"
        assert not pidfile.exists(), "the mis-delivered payload ran the pre-exec chain"
        assert r.returncode == 0, "a mis-delivered payload must be harmless, not an error"
        assert "mis-delivered" in r.stdout

    @pytest.mark.parametrize("shell", ["bash", "zsh"])
    def test_truncated_sid_argument_is_also_inert(self, tmp_path, shell):
        # §15b: truncation can now only shorten the TYPED line. A clipped trailing $1 must
        # mismatch the guard and no-op — even when delivered to the CORRECT tab.
        typed, marker, pidfile = self._bootstrap(tmp_path)
        r = self._run(typed[:-8], f"w9t9p0:{self.TARGET}", shell=shell)
        assert not marker.exists() and not pidfile.exists()
        assert r.returncode == 0
        assert "mis-delivered" in r.stdout

    @pytest.mark.parametrize("shell", ["bash", "zsh"])
    def test_positive_control_the_real_tab_still_runs_it(self, tmp_path, shell):
        # The other half of the same run: the guard must not break healthy spawns. Same typed
        # line, delivered to the tab whose $ITERM_SESSION_ID carries the target id.
        typed, marker, pidfile = self._bootstrap(tmp_path)
        r = self._run(typed, f"w9t9p0:{self.TARGET}", shell=shell)
        assert r.returncode == 0, r.stderr
        assert pidfile.exists(), "healthy bootstrap did not write its pidfile"
        assert marker.exists(), "healthy bootstrap did not exec claude"
        assert "--session-id 290f5187" in marker.read_text()

    def test_typed_line_carries_the_runtime_session_id(self, tmp_path):
        # §15b replacement for the inline-guard contract test: spawn() never staples a Python-side
        # id onto the launch. The typed line is `sh <bootstrap file> ` & sid — the id is
        # AppleScript's `id of targetSession`, appended as $1 at send time, and the file's guard
        # compares $ITERM_SESSION_ID against that $1, so the guard's id and the typed-into session
        # remain the same value by construction (#24's property, preserved across the protocol move).
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("OK\nS\nT")) as osa_run:
            iterm.spawn(cwd="/tmp", prompt="p" * 4096, label="l",
                        pidfile=str(tmp_path / "pid"), rename_delay=0)
        script = osa_run.call_args[0][0]
        boot = tmp_path / iterm.BOOTSTRAP_FILENAME
        assert f'write text ("sh {iterm.osa(str(boot))} " & sid)' in script
        # The typed line stays short and quote-free NO MATTER how long the prompt is — the whole
        # point of the file protocol. The >4KB prompt must be in the file, never in the script.
        assert "p" * 100 not in script
        content = boot.read_text()
        assert 'x$1' in content and "exec " in content and ("p" * 4096) in content
        # Truncation of the typed line can only ever produce: a partial `sh <path>` (command not
        # found / can't open — harmless) or a clipped $1 (guard mismatch → inert). No quotes exist
        # to leave open.
        typed = f"sh {boot} "
        assert '"' not in typed and "'" not in typed


# ================================================================================================
# #25 — THE RESIDUAL SEAM (incident updates 2026-08-01-later and 2026-08-02)
#
# #24 shipped target-or-abort and the payload guard, and BOTH worked: a `badge-flow` spawn misfired
# twice into two different wrong tabs and the guard ran nothing in either. But targeting still
# missed, and the 2026-08-02 `cutoff-jump` misfire pinned down why it wasn't a creation bug: the
# typed payload's guard carried the CORRECT intended session id, so creation and resolution were
# both right — yet the write landed in the frontmost tab. The send step's addressing was the hole.
# Second, smaller failure from the same evidence: the guard's else-echo arrived truncated, so the
# victim shell sat at a `dquote>` continuation prompt and swallowed the follow-up /rename line.
# ================================================================================================


class TestByIdWalkMatchOrAbort:
    """Incident audit item 1: the by-session-id walk must bind via an actual match or abort — and
    must not be defeated by the two id FORMATS in play (`w#t#p#:UUID` handles vs bare `id of
    session`)."""

    def test_repro_handle_form_id_would_miss_every_session(self):
        # THE BUG SHAPE. Feed the walk a handle-form id without normalizing: it compares
        # "w1t9p0:UUID" against `id of session` (bare "UUID"), so no session can ever match. Before
        # #24 that miss fell through to the frontmost tab; the point here is that the COMPARISON
        # itself is unsatisfiable, which is what made the fall-through reachable at all.
        handle, bare = "w1t9p0:81C69A6A-DEAD-BEEF", "81C69A6A-DEAD-BEEF"
        unnormalized = f'if (id of s) is "{handle}" then'
        assert unnormalized not in iterm._target_by_session_id_block(handle), \
            "walk still compares against the handle form — it would miss every session"
        # …post-fix both forms normalize to the same, satisfiable comparison.
        assert f'if (id of s) is "{bare}" then' in iterm._target_by_session_id_block(handle)
        assert iterm._target_by_session_id_block(handle) == iterm._target_by_session_id_block(bare)

    def test_walk_miss_sets_no_target_and_types_nothing(self):
        # The walk only ever sets targetFound INSIDE the match, so a miss reaches spawn()'s
        # NOTARGET abort — verdict returned, nothing typed anywhere.
        block = iterm._target_by_session_id_block("SOME-UUID")
        for line in block.splitlines():
            if "set targetFound to true" in line:
                assert line.startswith("          "), "targetFound set outside the match branch"
        assert block.count("set targetFound to true") == 1
        with mock.patch.object(iterm, "run_osascript", return_value=_ok("NOTARGET\n\n[Lead] fdata")), \
             mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab", return_value="GONE-ID"):
            out = iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid",
                              rename_delay=0, lead_handle="w1t2p0:LEAD", layout="tab")
        assert out["ok"] is False and out["reason"] == "no-target"
        assert out["front_title"] == "[Lead] fdata"

    def test_pyapi_handoff_id_is_normalized_on_the_way_in(self):
        # The pyapi path hands its session id straight to the walk; whichever form it returns, the
        # emitted comparison is the bare one the walk can actually match.
        for returned in ("81C69A6A-DEAD-BEEF", "w3t1p0:81C69A6A-DEAD-BEEF"):
            with mock.patch.object(iterm, "run_osascript", return_value=_ok("OK\nS\nT")) as osa_run, \
                 mock.patch.object(iterm.iterm_pyapi, "try_create_adjacent_tab", return_value=returned):
                iterm.spawn(cwd="/tmp", prompt="p", label="l", pidfile="/tmp/pid", rename_delay=0,
                            lead_handle="w1t2p0:LEAD", layout="tab")
            script = osa_run.call_args[0][0]
            assert 'if (id of s) is "81C69A6A-DEAD-BEEF" then' in script
            assert "w3t1p0" not in script


class TestGuardDoesNotWedgeVictimShell:
    """Incident update 2026-08-01-later: the guard's else-echo arrived truncated in the victim tab,
    leaving the shell at a `dquote>` continuation prompt that swallowed the follow-up /rename line.
    These run REAL shells; no terminal app is involved."""

    # The pre-#25 guard, verbatim from scripts/iterm.py at v0.3.35 — the repro's input.
    OLD_SHAPE = ('_relay_sid="${ITERM_SESSION_ID:-$TERM_SESSION_ID}"; if [ "${_relay_sid##*:}" = "%s" ]; '
                 'then %s; else echo "[relay] bootstrap for iTerm session %s was mis-delivered to '
                 'this tab (${_relay_sid:-no session id}); ignored, nothing was run."; fi')
    RENAME = "/rename [Exec] cutoff-jump"

    def _feed(self, line1, shell):
        """Both payload lines into a real shell, exactly as iTerm delivers them. A shell left with
        an unterminated quote reports an EOF/unmatched-quote error and never runs line 2 — the
        non-interactive face of the `dquote>` prompt the human saw."""
        return subprocess.run([shell], input=line1 + "\n" + self.RENAME + "\n",
                              capture_output=True, text=True, timeout=30,
                              env={k: v for k, v in os.environ.items()
                                   if k not in ("ITERM_SESSION_ID", "TERM_SESSION_ID")})

    def _truncated(self, payload, marker):
        assert marker in payload
        return payload[:payload.index(marker) + len(marker)]

    @pytest.mark.parametrize("shell", ["bash", "zsh"])
    def test_repro_old_shape_swallows_the_rename_line(self, shell):
        old = self.OLD_SHAPE % ("TARGET-UUID", "cd /tmp && exec claude --session-id 290f5187", "TARGET-UUID")
        r = self._feed(self._truncated(old, "was"), shell)
        blob = r.stdout + r.stderr
        assert ("unexpected EOF" in blob or "unmatched" in blob), \
            f"expected an unterminated-quote wedge, got: {blob!r}"
        assert "/rename" not in blob, "the rename line should have been swallowed by the open quote"

    @pytest.mark.parametrize("shell", ["bash", "zsh"])
    def test_typed_line_cut_at_every_offset_never_wedges(self, shell, tmp_path):
        # §15b: the only thing typed now is `sh <file> <sid>`. Cut it at EVERY offset, feed it
        # plus the /rename line to a real shell: no truncation point may leave a quote or block
        # open (no wedge), and none may execute the bootstrap (the file's guard needs the full
        # matching $1, which only the untruncated line carries — and even that no-ops here, since
        # the test shell has no matching $ITERM_SESSION_ID).
        marker = tmp_path / "claude-ran"
        boot = iterm.write_bootstrap_file(
            str(tmp_path / "pid"), f"echo ran > {shlex.quote(str(marker))}")
        typed = f"sh {boot} TARGET-UUID"
        for cut in range(1, len(typed) + 1):
            r = self._feed(typed[:cut], shell)
            blob = r.stdout + r.stderr
            assert "unexpected EOF" not in blob and "unmatched" not in blob and "dquote" not in blob, \
                f"offset {cut}: wedge — {blob!r}"
            assert ("/rename" in blob or "[Exec]" in blob), \
                f"offset {cut}: the rename line was swallowed — {blob!r}"
            assert not marker.exists(), f"offset {cut}: a truncated line RAN the bootstrap"

    @pytest.mark.parametrize("shell", ["bash", "zsh"])
    def test_misdelivered_typed_line_exits_zero(self, shell, tmp_path):
        # The full typed line in a wrong shell (no matching $ITERM_SESSION_ID): notice once,
        # rc 0 — a victim shell must not be left with a non-zero $? for a no-op it never asked for.
        boot = iterm.write_bootstrap_file(str(tmp_path / "pid"), "echo should-not-run")
        r = subprocess.run([shell], input=f"sh {boot} TARGET-UUID\n", capture_output=True,
                           text=True, timeout=30,
                           env={k: v for k, v in os.environ.items()
                                if k not in ("ITERM_SESSION_ID", "TERM_SESSION_ID")})
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert r.stdout.count("mis-delivered") == 1
        assert "should-not-run" not in r.stdout


class TestBuildClaudeCmdMcpFlags:
    """mcp_flags (from lead_guard.mcp_cli_flags) land verbatim, shell-quoted, on fresh AND resumed
    launches — MCP loading is per-process, so a resume needs them again."""
    def test_none_spec_flags_are_quoted_and_present(self):
        flags = lead_guard.mcp_cli_flags("none")
        cmd = iterm.build_claude_cmd("x", mcp_flags=flags)
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config '{\"mcpServers\":{}}'" in cmd

    def test_resume_keeps_mcp_flags(self):
        cmd = iterm.build_claude_cmd("x", resume_id="cs-1", mcp_flags=["--strict-mcp-config", "--mcp-config", "/p/mcp.json"])
        assert "--resume cs-1" in cmd and "--strict-mcp-config --mcp-config /p/mcp.json" in cmd

    def test_inherit_adds_nothing(self):
        assert "mcp" not in iterm.build_claude_cmd("x", mcp_flags=lead_guard.mcp_cli_flags("inherit"))
