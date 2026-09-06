"""
Bug-hunt suite: hooks/hooks.json — the manifest itself.

The manifest is the only thing that decides whether any of the other five hooks ever runs. A typo
in a script path, a lost executable bit, a matcher that doesn't match, or a `timeout` shorter than
the hook's own internal deadline all fail the same silent way: the feature simply never fires, and
nothing says a word. Every check here is about that class of defect.

Oracle:
  1. hooks/hooks.json's own `description` field, and the manifest values other code READS BACK —
     lead_guard._read_stop_hook_timeout:568-574 and bin/relay's `stop_hook_timeout()` (L349) both
     parse `hooks.Stop[0].hooks[0].timeout` by that exact path, so the shape is load-bearing.
  2. lead_guard.wake_hook_state:536-559 — the timeout must be >= `poll_seconds` or a lead reads
     'stale' ("the harness lets the poller run long enough to catch a late report").
  3. stop_lead_watch.py's own deadline arithmetic (L331: `poll_seconds`, L236: a 25s auto-close
     sweep before it).
  4. Each hook script's docstring for which event and payload shape it assumes.

Run: pytest tests/test_hooks_manifest.py -q
"""
import json
import os
import re
import shlex
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

MANIFEST_PATH = H.REPO_ROOT / "hooks" / "hooks.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text())
HOOKS = MANIFEST["hooks"]

PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"


def every_entry():
    """(event, matcher, entry) for every command entry in the manifest."""
    for event, groups in HOOKS.items():
        for group in groups:
            for entry in group["hooks"]:
                yield event, group.get("matcher"), entry


def command_paths():
    """The REAL script path each manifest entry points at: plugin-root placeholder resolved, THEN
    shell-unquoted via shlex — the same two steps the real shell performs launching the hook (env
    expansion, then word-splitting/quote-removal). BUG-hooks-2's fix double-quotes the placeholder
    (`"${CLAUDE_PLUGIN_ROOT}"/hooks/...`) — double quotes still permit `/bin/sh` to expand the
    variable, unlike single quotes, which suppress expansion entirely and make the hook look for a
    script literally named `${CLAUDE_PLUGIN_ROOT}` — so a naive string substitution alone would
    leave literal quote characters in the "path" every other test in this file treats as a real
    filesystem path."""
    out = []
    for event, _matcher, entry in every_entry():
        cmd = entry["command"].replace(PLUGIN_ROOT_VAR, str(H.REPO_ROOT))
        out.append((event, shlex.split(cmd)[0]))
    return out


# =================================================================================================
# Shape
# =================================================================================================

class TestManifestShape:

    def test_manifest_is_valid_json_with_a_description(self):
        assert isinstance(MANIFEST.get("description"), str) and MANIFEST["description"]

    def test_every_entry_is_a_command_hook(self):
        for event, _m, entry in every_entry():
            assert entry["type"] == "command", event
            assert isinstance(entry["command"], str) and entry["command"], event

    def test_the_events_are_exactly_the_four_the_scripts_implement(self):
        """PreToolUse (both gates), Stop (the lead watcher), SessionStart and SessionEnd (the
        arming state machine). executor_escalation.py is deliberately NOT here — it is armed
        per-executor via `--settings` at spawn (lead_guard.build_escalation_settings:2212-2240),
        because executors launch plain with no plugin loaded."""
        assert set(HOOKS) == {"PreToolUse", "Stop", "SessionStart", "SessionEnd"}

    def test_every_command_is_rooted_at_the_plugin_root_placeholder(self):
        """A relative or absolute path would break on any install that isn't the author's.
        BUG-hooks-2's fix double-quotes the placeholder (`"${CLAUDE_PLUGIN_ROOT}"/...`) so a plugin
        root containing a space still word-splits to one argv element — still rooted at the same
        placeholder, just wrapped in double quotes, which (unlike single quotes) still let
        `/bin/sh` expand the variable."""
        quoted_root = '"%s"' % PLUGIN_ROOT_VAR
        for _event, _m, entry in every_entry():
            assert entry["command"].startswith(quoted_root), entry["command"]

    def test_no_hook_is_registered_twice(self):
        paths = [p for _e, p in command_paths()]
        assert len(paths) == len(set(paths))


# =================================================================================================
# The scripts the manifest points at
# =================================================================================================

class TestManifestScriptsAreRunnable:
    """Every one of these is a silent-failure mode: Claude Code does not report a hook whose
    command cannot be executed."""

    @pytest.mark.parametrize("event,path", command_paths(), ids=lambda v: str(v)[-40:])
    def test_the_referenced_script_exists(self, event, path):
        assert os.path.isfile(path), "%s hook points at a missing script: %s" % (event, path)

    @pytest.mark.parametrize("event,path", command_paths(), ids=lambda v: str(v)[-40:])
    def test_the_referenced_script_is_executable(self, event, path):
        """The manifest names the script directly (no interpreter), so the exec bit is what makes
        it runnable at all."""
        assert os.stat(path).st_mode & stat.S_IXUSR, "%s is not executable" % path

    @pytest.mark.parametrize("event,path", command_paths(), ids=lambda v: str(v)[-40:])
    def test_the_referenced_script_has_the_python3_shebang(self, event, path):
        with open(path) as f:
            assert f.readline().strip() == "#!/usr/bin/env python3", path

    @pytest.mark.parametrize("name", H.ALL_HOOKS)
    def test_every_hook_script_compiles(self, name):
        """A syntax error in a hook is a hook that never runs — and a `python3 -m py_compile` is
        the only check that catches it before a user does."""
        r = subprocess.run([sys.executable, "-m", "py_compile", str(H.HOOKS_DIR / name)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    @pytest.mark.parametrize("name", H.ALL_HOOKS)
    def test_every_hook_script_defines_main_and_guards_it(self, name):
        """All six are dual-use: run as a script by the harness, imported as a module by
        sessionstart's `_notify_rearm`, executor_escalation's `_notify_human`, and this suite."""
        src = (H.HOOKS_DIR / name).read_text()
        assert re.search(r"^def main\(\):", src, re.M), name
        assert 'if __name__ == "__main__":' in src, name

    def test_the_escalation_hook_is_shipped_even_though_the_manifest_omits_it(self):
        """It is armed at spawn, not by the manifest — but it must still exist and be runnable,
        because build_escalation_settings bakes its absolute path into every executor's
        --settings file."""
        p = H.HOOKS_DIR / "executor_escalation.py"
        assert p.is_file() and os.stat(p).st_mode & stat.S_IXUSR
        built = lg.build_escalation_settings(str(H.REPO_ROOT), "exec-1")
        cmd = built["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert cmd.split()[0] == str(p)
        assert cmd.split()[1] == "exec-1", "the executor's relay NAME must ride as argv[1]"

    def test_no_hook_script_is_left_unreferenced(self):
        """Every script in hooks/ is reachable either from the manifest or from
        build_escalation_settings. An orphan would be dead code that looks live."""
        on_disk = {p.name for p in H.HOOKS_DIR.glob("*.py")}
        from_manifest = {os.path.basename(p) for _e, p in command_paths()}
        from_spawn = {"executor_escalation.py"}
        assert on_disk == from_manifest | from_spawn


# =================================================================================================
# Matchers
# =================================================================================================

class TestManifestMatchers:

    def _matchers(self, event):
        return [g.get("matcher") for g in HOOKS[event]]

    def test_the_edit_gate_matches_the_three_tools_it_sizes(self):
        """lead_guard.edit_line_count:309-327 knows exactly Write / Edit / MultiEdit; the matcher
        must deliver those three and the guard must be the one that gets them."""
        matchers = self._matchers("PreToolUse")
        assert "Edit|Write|MultiEdit" in matchers
        group = [g for g in HOOKS["PreToolUse"] if g.get("matcher") == "Edit|Write|MultiEdit"][0]
        assert "pretool_route_guard.py" in group["hooks"][0]["command"]
        for tool in ("Edit", "Write", "MultiEdit"):
            assert re.search("Edit|Write|MultiEdit", tool), tool

    def test_the_bash_gate_matches_bash_only(self):
        group = [g for g in HOOKS["PreToolUse"] if g.get("matcher") == "Bash"][0]
        assert "pretool_bash_gate.py" in group["hooks"][0]["command"]

    def test_the_two_pretool_gates_do_not_share_a_matcher(self):
        """The Bash gate never denies and the edit gate does; crossing them would either start
        blocking Bash (which the docs promise it does not) or stop sizing edits."""
        matchers = self._matchers("PreToolUse")
        assert len(matchers) == len(set(matchers))
        assert not re.search("Edit|Write|MultiEdit", "Bash")

    def test_stop_and_sessionend_match_everything(self):
        """Both hooks do their own `is_lead` fast-exit (stop_lead_watch:219, and SessionEnd logs
        every session's reason for incident attribution), so neither wants a matcher."""
        assert self._matchers("Stop") == [None]
        assert self._matchers("SessionEnd") == [None]

    def test_sessionstart_matches_every_source(self):
        """The hook branches on `source` itself (resume/clear/startup/compact — L37-38, L106), so
        it must receive all of them; `*` is the match-everything matcher."""
        assert self._matchers("SessionStart") == ["*"]

    def test_each_event_routes_to_the_script_that_documents_that_event(self):
        """A script wired to the wrong event reads a payload shape it does not understand and
        fast-exits forever. Each docstring names its own event on its first line."""
        expected = {
            "pretool_route_guard.py": "PreToolUse",
            "pretool_bash_gate.py": "PreToolUse",
            "stop_lead_watch.py": "Stop",
            "sessionstart_lead_rearm.py": "SessionStart",
            "sessionend_lead_cleanup.py": "SessionEnd",
        }
        for event, path in command_paths():
            assert expected[os.path.basename(path)] == event, path
            doc = open(path).read().split('"""')[1]
            assert event in doc, "%s's docstring does not name %s" % (path, event)


# =================================================================================================
# The Stop hook's async-rewake contract
# =================================================================================================

STOP_ENTRY = HOOKS["Stop"][0]["hooks"][0]


class TestStopHookManifestEntry:
    """docs/async-rewake-findings.md, quoted in stop_lead_watch:19-23: "runs in the background;
    exit 0 → silent, lead stays idle; exit 2 → the idle lead WAKES with this script's stderr + the
    hook's rewakeMessage"."""

    def test_the_stop_hook_is_async(self):
        """Without `asyncRewake` the whole App-1 background poller is impossible — the harness
        would block the turn on it instead of letting it watch for a late report."""
        assert STOP_ENTRY["asyncRewake"] is True

    def test_only_the_stop_hook_is_async(self):
        """A PreToolUse gate that returned asynchronously could not deny anything."""
        for event, _m, entry in every_entry():
            if event != "Stop":
                assert "asyncRewake" not in entry, event

    def test_the_rewake_message_carries_the_announce_and_wait_instruction(self):
        """skills/mode/SKILL.md:104-110 — the harness-side message must itself say announce-and-
        WAIT, because it is what the model sees alongside the hook's stderr."""
        msg = STOP_ENTRY["rewakeMessage"]
        assert "🚦 [relay] — review needed:" in msg
        assert "wait for their direction" in msg.lower()
        assert "do not auto-review or auto-commit" in msg.lower()

    def test_the_rewake_marker_matches_the_one_the_hook_emits(self):
        """The hook's stderr (stop_lead_watch:167) and the manifest's rewakeMessage must open with
        the SAME marker, or the lead's on-screen text is inconsistent between the two halves of one
        wake."""
        marker = "🚦 [relay] — review needed:"
        assert STOP_ENTRY["rewakeMessage"].startswith(marker)
        src = (H.HOOKS_DIR / "stop_lead_watch.py").read_text()
        assert marker.replace("🚦", "\\U0001f6a6") in src or marker in src

    def test_the_rewake_summary_is_a_one_liner(self):
        s = STOP_ENTRY["rewakeSummary"]
        assert "\n" not in s and s.startswith("🚦 [relay]")


class TestStopHookTimeoutConsistency:
    """"a hook that runs past its manifest timeout is a bug" — the harness kills it, the poller
    dies mid-window, and the late report it was watching for is never surfaced. This is the exact
    failure lead_guard.wake_hook_state:536-559 exists to detect after the fact."""

    def test_the_timeout_is_declared(self):
        """wake_hook_state:546-549 — a missing timeout is "a 0.1.0-era hook ... killed at the
        harness default, THE ORIGINAL MISSED-WAKE BUG"."""
        assert isinstance(STOP_ENTRY.get("timeout"), int)

    def test_the_timeout_exceeds_the_default_poll_window(self):
        """stop_lead_watch:331 runs its loop for `poll_seconds` (default 1800). The manifest
        timeout must be at least that, or wake_hook_state stamps every lead 'stale'."""
        assert STOP_ENTRY["timeout"] >= lg.LEAD_DEFAULTS["poll_seconds"]

    def test_the_timeout_covers_the_whole_worst_case_run(self):
        """The poll window is not the only thing inside the timeout: the hook first spends up to
        25s on the auto-close sweep (stop_lead_watch:236-238) and the loop overshoots its deadline
        by up to one `poll_interval` (L332-334, sleep-then-check). All of it must fit."""
        worst = (lg.LEAD_DEFAULTS["poll_seconds"] + 25 + lg.LEAD_DEFAULTS["poll_interval"])
        assert STOP_ENTRY["timeout"] >= worst, (
            "timeout %s < worst-case run %s" % (STOP_ENTRY["timeout"], worst))

    def test_a_default_lead_reads_as_a_healthy_wake_hook(self):
        """The end-to-end version of the two checks above, through the function `relay list`
        actually calls: with the shipped manifest and shipped defaults, a freshly stamped lead must
        read 'ok', never 'stale'."""
        marker = {"stop_hook_timeout": STOP_ENTRY["timeout"]}
        assert lg.wake_hook_state(marker, lg.LEAD_DEFAULTS["poll_seconds"]) == "ok"

    def test_lead_guard_reads_the_timeout_back_out_of_this_exact_shape(self):
        """_read_stop_hook_timeout:568-574 indexes `hooks.Stop[0].hooks[0].timeout` positionally.
        Reordering or wrapping the Stop entry would silently return None — and None is the
        'stale' verdict above."""
        assert lg._read_stop_hook_timeout(str(H.REPO_ROOT)) == STOP_ENTRY["timeout"]

    def test_the_plugin_version_is_readable_from_the_same_root(self):
        """touch_lead re-stamps both from the live plugin root on every lead turn."""
        assert lg._read_plugin_version(str(H.REPO_ROOT)) == json.loads(
            (H.REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]


# =================================================================================================
# The manifest's own description
# =================================================================================================

class TestManifestDescriptionIsTrue:
    """The description is what a user reads to decide whether to install the plugin; every claim
    in it is checkable against the scripts."""

    def test_it_claims_the_pretool_gate_blocks_large_inline_edits(self):
        d = MANIFEST["description"]
        assert "PreToolUse gate blocks large inline Edit/Write/MultiEdit" in d

    def _stranger_payload(self, tmp_path):
        """One payload carrying every field any of the four events could want, from a session id
        that no marker anywhere mentions."""
        return {"session_id": "a-stranger", "tool_name": "Write", "reason": "exit",
                "source": "startup", "cwd": str(tmp_path),
                "tool_input": {"file_path": str(tmp_path / "n.py"), "content": "x" * 100,
                               "command": "npm install left-pad"}}

    @pytest.mark.parametrize("hook", [os.path.basename(p) for _e, p in command_paths()])
    def test_every_hook_is_silent_for_a_stranger(self, hook, tmp_path):
        """"Silent on non-lead and executor sessions" — no output, no decision, exit 0."""
        assert "Silent on non-lead and executor sessions" in MANIFEST["description"]
        run = H.run_hook(hook, self._stranger_payload(tmp_path), tmp_path)
        assert run.returncode == 0, hook
        assert run.stdout == "" and run.stderr == "", hook

    @pytest.mark.parametrize("hook", [os.path.basename(p) for _e, p in command_paths()])
    def test_no_hook_creates_state_for_a_stranger(self, hook, tmp_path):
        """CONTRACT — hooks/hooks.json's own description: "Silent on non-lead and executor
        sessions (EACH HOOK FAST-EXITS WHEN THE LEAD MARKER IS ABSENT)". README.md:320-322 says the
        same from the user's side: "it only acts in /relay:mode sessions — every other session on
        the machine is untouched", and hooks/pretool_route_guard.py:4-5 repeats it: "Everywhere
        else (non-lead sessions, executor sessions, EVERY OTHER PROJECT ON THE MACHINE) it
        fast-exits and allows."

        BUG-hooks-3 (fixed): `hooks/sessionend_lead_cleanup.py` used to log to the ledger BEFORE
        any marker check, and `append_ledger`'s `root.mkdir(parents=True, exist_ok=True)` created
        `~/.relay-tasks` on the way — so ending ANY Claude Code session, in ANY project, on a
        machine where relay was merely installed, created state forever, unbounded. D3 (the
        decision made): log only when `~/.relay-tasks` already exists (relay has been used on this
        machine) — `if sid and os.path.isdir(STATE_ROOT):`. A machine that has run relay keeps full
        incident attribution for a stranger session (see the sibling test below); a machine that
        has NOT run relay is now genuinely untouched, which is what this test proves."""
        H.run_hook(hook, self._stranger_payload(tmp_path), tmp_path)
        assert not H.state_root(tmp_path).exists(), \
            "%s created state for a session that is not a lead" % hook

    def test_a_stranger_on_a_machine_that_already_uses_relay_still_only_ledgers(self):
        """D3's OTHER half: once `~/.relay-tasks` already exists (this machine has used relay
        before), a stranger session's SessionEnd still keeps full incident attribution — it must
        not ARM anything. No marker, no lead dir — the write is confined to one ledger line."""
        import tempfile
        home = tempfile.mkdtemp()
        H.state_root(home).mkdir(parents=True)   # relay has been used on this machine before
        H.run_hook("sessionend_lead_cleanup.py",
                   {"session_id": "a-stranger", "reason": "exit"}, home)
        root = H.state_root(home)
        assert H.ledger_events(home) == ["session_end"]
        assert not (root / "lead").exists()
        assert lg.is_lead(root, "a-stranger") is False

    def test_it_claims_the_stop_hook_never_auto_acts(self):
        assert "never auto-acts" in MANIFEST["description"]

    def test_it_claims_sessionend_cleans_up(self):
        assert "SessionEnd cleans up lead state" in MANIFEST["description"]


# =================================================================================================
# BUG-hooks-2 — hook commands are not shell-safe
# =================================================================================================

class TestManifestCommandQuoting:

    def test_manifest_commands_survive_a_plugin_root_with_a_space(self):
        """CONTRACT — the manifest's own `description` promises the gate "blocks large inline
        Edit/Write/MultiEdit" and that the Stop hook "wakes the idle lead"; both are unconditional.
        lib/lead_guard.py's own shell-command builder quotes for exactly this reason
        (hooks/stop_lead_watch.py:90: `"-execute", f"'{RELAY_BIN}' focus {lead_sid}"`).

        ACTUAL (as originally found) — hooks/hooks.json:11 et al. spelled the command as
        `${CLAUDE_PLUGIN_ROOT}/hooks/pretool_route_guard.py` with no quoting. A plugin installed
        under a path containing a space (`--plugin-dir "~/My Plugins/claude-relay"`) word-splits
        into an argv whose first element does not exist, so the hook never runs. Nothing reports
        it: a hook that fails to launch is indistinguishable from a hook that fast-exited, which
        is precisely the silent-failure class this manifest suite exists to catch.

        FIX — DOUBLE-quote the placeholder in every entry:
            "command": "\"${CLAUDE_PLUGIN_ROOT}\"/hooks/pretool_route_guard.py"
        Single quotes were tried first and are WRONG: `/bin/sh` never expands a variable inside
        single quotes, so `'${CLAUDE_PLUGIN_ROOT}'/hooks/x.py` looks for a script literally named
        `${CLAUDE_PLUGIN_ROOT}` and fails every hook, every time (lead-found live: "No such file or
        directory"). Double quotes still let the shell expand the variable while surviving a space
        in the resolved path. This test can't tell the two forms apart (it never actually invokes
        `/bin/sh`, only shlex — see the module docstring) which is exactly why the single-quoted
        form shipped and broke every hook before it was caught by manual proof, not by this
        suite."""
        spaced = "/Users/someone/My Plugins/claude-relay"
        for _event, _m, entry in every_entry():
            resolved = entry["command"].replace(PLUGIN_ROOT_VAR, spaced)
            argv = shlex.split(resolved)
            assert len(argv) == 1, "%r word-splits into %r" % (resolved, argv)

    def test_the_escalation_settings_command_survives_a_plugin_root_with_a_space(self):
        """Same defect, second location: lead_guard.build_escalation_settings:2233 builds
        `f"{hook_path} {exec_name}"`. With a spaced plugin root the executor's Stop hook never
        launches, so the §9 escalation push is silently disarmed for every executor."""
        spaced = "/Users/someone/My Plugins/claude-relay"
        cmd = lg.build_escalation_settings(spaced, "exec-1")["hooks"]["Stop"][0]["hooks"][0]["command"]
        argv = shlex.split(cmd)
        assert argv == [os.path.join(spaced, "hooks", "executor_escalation.py"), "exec-1"], argv

    def test_the_command_is_shell_safe_for_the_normal_install_path(self):
        """Whatever BUG-hooks-2 resolves to, the paths relay actually ships under today parse to a
        single argv element — this is what keeps the bug LATENT rather than live."""
        for _event, path in command_paths():
            assert shlex.split(path) == [path]
