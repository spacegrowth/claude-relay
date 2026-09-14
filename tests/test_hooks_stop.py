"""
Bug-hunt suite: hooks/stop_lead_watch.py — the Stop hook (asyncRewake) that wakes an IDLE lead.

Oracle, in the shared bug-hunt priority order:
  1. README.md § "A second layer underneath" (L425-445) and the config table (L633-650);
     docs/wake-watch-design.md; docs/async-rewake-findings.md.
  2. skills/mode/SKILL.md L104-110 (announce-and-wait) and L144-167 (the autonomous stop-list).
  3. The hook's own module docstring (L2-23): "exit 0 → silent, lead stays idle; exit 2 → the idle
     lead WAKES with this script's stderr", "Gated to fire ONCE per event", "HARD RULE: any error →
     exit 0 (fail open, never brick normal usage)".
  4. lib/lead_guard.py's #22/#23 blocks (L813-1011), which document the two-phase surfaced stamp.

Every run here sets `RELAY_NO_NOTIFY=1` and puts stub `terminal-notifier`/`osascript` FIRST on
PATH, so no test can post a real desktop banner; the one test that deliberately drops the
kill-switch asserts against those stubs.

Run: pytest tests/test_hooks_stop.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

STOP = "stop_lead_watch.py"

# All shared with tests/conftest_hooks.py so the two halves of this suite cannot drift apart.
DRIVERS, DRIVER_IDS = H.DRIVERS, H.DRIVER_IDS
WAKE, SILENT = H.WAKE, H.SILENT
stop_payload = H.stop_payload
armed = H.armed_lead_with_config


# =================================================================================================
# HARD RULE — fail open
# =================================================================================================

class TestStopHookFailOpen:
    """"HARD RULE: any error → exit 0 (fail open, never brick normal usage)" (stop_lead_watch:22).
    Exit 2 is a WAKE, so a crash that leaked a non-zero code would wake the lead with a traceback;
    anything other than a deliberate 2 must be 0."""

    @pytest.mark.parametrize("name", sorted(H.MALFORMED_STDIN))
    def test_malformed_stdin_is_silent(self, name, tmp_path):
        armed(tmp_path)
        run = H.run_hook(STOP, None, tmp_path, raw=H.MALFORMED_STDIN[name])
        assert run.returncode == SILENT, run.stderr
        assert run.stdout == "" and run.stderr == ""

    def test_home_unset_is_silent(self, tmp_path):
        run = H.run_hook(STOP, stop_payload(tmp_path), tmp_path, env_extra={"HOME": ""})
        assert run.returncode == SILENT and run.stderr == ""

    def test_state_root_is_a_file_is_silent(self, tmp_path):
        H.state_root(tmp_path).write_text("not a directory")
        assert H.run_hook(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    def test_missing_session_id_is_silent(self, tmp_path):
        armed(tmp_path)
        H.make_executor(tmp_path)
        run = H.run_hook(STOP, {"cwd": str(tmp_path)}, tmp_path)
        assert run.returncode == SILENT and run.stderr == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_corrupt_executor_session_json_is_survived(self, drv, tmp_path):
        """executor_reports:775-782 skips a session dir it can't parse. One truncated
        session.json must not take the whole wake down with it — the OTHER executor still wakes."""
        armed(tmp_path)
        H.make_executor(tmp_path, "exec-broken")
        (H.state_root(tmp_path) / "exec-broken" / "session.json").write_text('{"session_id"')
        H.make_executor(tmp_path, "exec-ok")
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE
        assert "exec-ok" in run.stderr and "exec-broken" not in run.stderr

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_executor_session_json_missing_keys_is_skipped(self, drv, tmp_path):
        """A session.json with no `session_id` raises inside executor_reports' per-dir try — that
        dir is skipped, the hook is not."""
        armed(tmp_path)
        d = H.make_executor(tmp_path, "exec-1")
        (d / "session.json").write_text(json.dumps({"current_packet": 1, "status": "busy",
                                                    "owner_lead": "lead-1"}))
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_corrupt_config_falls_back_to_defaults(self, drv, tmp_path):
        """load_config:243 — "missing/corrupt file → pure defaults", so `auto_wake` is still on."""
        root = H.arm_lead(tmp_path, project="proj")
        (root / "lead").mkdir(parents=True, exist_ok=True)
        (root / "lead" / "config.json").write_text("{ broken")
        H.make_executor(tmp_path, status="reported")   # not in-flight → no poller to hang on
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE

    def test_unwritable_state_root_still_announces(self, tmp_path):
        """The pending stamp, the ledger and the claim are all best-effort. A read-only state root
        must degrade to "announce anyway", never to "stay silent" — a silent wake is the exact
        failure #22 was written about."""
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        os.chmod(H.state_root(tmp_path) / "lead" / "lead-1", 0o500)
        try:
            run = H.run_hook(STOP, stop_payload(tmp_path), tmp_path)
            assert run.returncode == WAKE
            assert "exec-1" in run.stderr
        finally:
            os.chmod(H.state_root(tmp_path) / "lead" / "lead-1", 0o700)


# =================================================================================================
# The zero-impact path
# =================================================================================================

class TestStopHookZeroImpact:
    """hooks.json's own description: "Silent on non-lead and executor sessions (each hook
    fast-exits when the lead marker is absent)"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_non_lead_session_is_silent_and_writes_nothing(self, drv, tmp_path):
        run = drv(STOP, stop_payload(tmp_path, sid="stranger"), tmp_path)
        assert run.returncode == SILENT and run.stderr == ""
        assert not H.state_root(tmp_path).exists()

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_executor_session_is_silent(self, drv, tmp_path):
        """An executor has a top-level state dir but no lead marker — is_lead is False, so the
        hook must not run any of App 1/App 2 for it."""
        armed(tmp_path)
        H.make_executor(tmp_path, "exec-1", status="reported")
        run = drv(STOP, stop_payload(tmp_path, sid="exec-1"), tmp_path)
        assert run.returncode == SILENT and run.stderr == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_tombstoned_lead_is_silent(self, drv, tmp_path):
        """is_lead:441-446 — a tombstone means "exited but resumable": "until a resume revives it,
        the gate and the WAKE must stay off"."""
        root = armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        lg.tombstone_lead(root, "lead-1")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_nothing_new_is_silent(self, drv, tmp_path):
        """An armed lead with no executors and no commits ends its turn silently."""
        armed(tmp_path)
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == SILENT and run.stderr == ""


# =================================================================================================
# App 1 — an executor reported
# =================================================================================================

class TestStopHookReportWake:

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_report_at_stop_time_wakes_with_the_marker_and_a_brief(self, drv, tmp_path):
        """_report_lines:188-198 — each line "carries a BRIEF of the report (its first line) so you
        know what happened at a glance"; the 🚦 marker is what the model echoes (L123-125)."""
        armed(tmp_path)
        H.make_executor(tmp_path, report="Split-pane layout works; 9 tests, suite green, staged.\n")
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE
        assert run.stderr.startswith("🚦 [relay] — review needed: %s:" % lg.WAKE_DELIVERY_NEEDLE)
        assert "executor 'exec-1' reported (packet 001)" in run.stderr
        assert "Split-pane layout works" in run.stderr
        assert str(H.state_root(tmp_path) / "exec-1" / "packets" / "001-report.md") in run.stderr
        assert run.stdout == "", "a Stop hook's stdout is not the wake channel — stderr is"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_report_brief_strips_heading_markers_and_blank_lines(self, drv, tmp_path):
        """_report_brief:174-185 — "The first MEANINGFUL line ... Heading markers stripped,
        whitespace collapsed"."""
        armed(tmp_path)
        H.make_executor(tmp_path, report="\n\n###    Gate   works    fine\n\nmore text\n")
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert "— Gate works fine" in run.stderr

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_empty_report_still_wakes_with_the_path(self, drv, tmp_path):
        """An empty (or unreadable) report yields no brief — the line falls back to naming the
        path so the lead can still go look."""
        armed(tmp_path)
        H.make_executor(tmp_path, report="")
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE
        assert "report at " in run.stderr

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_brief_is_capped(self, drv, tmp_path):
        """_report_brief's maxlen=200 keeps one runaway line out of the wake text."""
        armed(tmp_path)
        H.make_executor(tmp_path, report="x" * 5000 + "\n")
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert "x" * 200 in run.stderr and "x" * 201 not in run.stderr

    def _git(self, repo, *args):
        import subprocess
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_wake_line_carries_the_diff_size(self, drv, tmp_path):
        """Item 4 (lead-context-burn note): the diff's size travels next to the report in the wake
        line too — the very first thing a woken lead sees."""
        armed(tmp_path)
        repo = tmp_path / "repo"; repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "a.py").write_text("one\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-m", "init")
        (repo / "a.py").write_text("changed\n")
        self._git(repo, "add", "a.py")
        H.make_executor(tmp_path, report="Fixed it.\n", worktree=str(repo))
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE
        assert "(diff: 1 files +1/-1)" in run.stderr

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_wake_line_never_prints_more_than_first_line_plus_diff_size(self, drv, tmp_path):
        """Item 4 (row 65): the wake must never surface the report's TL;DR block or body — only its
        first line, the diff size, and a pointer to the report file. The report below carries every
        TL;DR field plus a body section, so a leak would show up in `run.stderr`."""
        armed(tmp_path)
        repo = tmp_path / "repo"; repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "a.py").write_text("one\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-m", "init")
        (repo / "a.py").write_text("changed\n")
        self._git(repo, "add", "a.py")
        report = ("Changed the source file; suite green, staged.\n\n"
                  "Status: clean\nRisk flags: none\nUNVERIFIED: none\nChanged: a.py\n\n"
                  "## What changed\n- a.py:1 — changed it.\n\n"
                  "My changes are staged, not committed, ready for the lead to review.\n")
        H.make_executor(tmp_path, report=report, worktree=str(repo))
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE
        assert "(diff: 1 files +1/-1)" in run.stderr
        assert "Changed the source file; suite green, staged." in run.stderr  # the first line only
        for leak in ("Risk flags", "UNVERIFIED", "Changed:", "## What changed",
                     "ready for the lead to review"):
            assert leak not in run.stderr, f"{leak!r} leaked into the wake line"
        # exactly two lines for this one report: the head+brief line, and the "report: <path>" line
        report_lines = [l for l in run.stderr.splitlines() if "exec-1" in l or "report:" in l]
        assert len(report_lines) == 2

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_several_reports_are_all_surfaced_in_one_wake(self, drv, tmp_path):
        armed(tmp_path)
        H.make_executor(tmp_path, "exec-a", report="alpha done\n")
        H.make_executor(tmp_path, "exec-b", report="beta done\n")
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE
        assert "exec-a" in run.stderr and "exec-b" in run.stderr

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_auto_wake_kill_switch(self, drv, tmp_path):
        """README:633-650 config table / LEAD_DEFAULTS `auto_wake`: with it off, App 1 is off."""
        armed(tmp_path, auto_wake=False)
        H.make_executor(tmp_path, status="reported")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_unicode_report_brief_survives(self, drv, tmp_path):
        armed(tmp_path)
        H.make_executor(tmp_path, report="✅ 日本語 — ünïcodé brief 🚦\n")
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert "日本語 — ünïcodé brief" in run.stderr


class TestStopHookSkipsLandedReports:
    """Row 70 item 4 (issue 03-surfaced-not-carried-on-adopt.md), belt-and-braces half: after a
    handoff the wake re-surfaced two reports the PREDECESSOR had already reviewed AND COMMITTED as
    "✅ executor reported … review needed" — a turn burned per stale report, and a real risk of
    re-reviewing committed work. A report whose claimed files are already clean at HEAD has landed
    (the same test auto-close uses); the wake skips it and ledgers `wake_skipped_landed`."""

    def _git(self, repo, *args):
        import subprocess
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    def _repo(self, tmp_path, dirty=False):
        repo = tmp_path / "repo"; repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "a.py").write_text("one\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-m", "init")
        if dirty:
            (repo / "a.py").write_text("still in flight\n")
        return repo

    REPORT = ("Fixed the thing; suite green, staged.\n\nStatus: clean\nRisk flags: none\n"
              "UNVERIFIED: none\nChanged: one module\n\n## What changed\n- `a.py:1` — rewritten\n")

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_landed_report_does_not_wake_the_lead(self, drv, tmp_path):
        armed(tmp_path)
        repo = self._repo(tmp_path)          # claimed a.py is clean → the lead committed it
        H.make_executor(tmp_path, report=self.REPORT, worktree=str(repo), status="reported")
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == SILENT
        assert "wake_skipped_landed" in H.ledger_events(tmp_path)

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_unlanded_report_still_wakes(self, drv, tmp_path):
        armed(tmp_path)
        repo = self._repo(tmp_path, dirty=True)   # the work is still sitting in the worktree
        H.make_executor(tmp_path, report=self.REPORT, worktree=str(repo), status="reported")
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE
        assert "wake_skipped_landed" not in H.ledger_events(tmp_path)

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_skipped_landed_report_is_stamped_surfaced_and_never_asked_about_again(self, drv,
                                                                                     tmp_path):
        """Lead review of packet 001: skipping alone left the report in `new_reports_for` forever —
        a git status on EVERY Stop hook, and `relay list` naming it under "NOT yet proven
        delivered" for good. Landing IS the terminal outcome, so the skip stamps it surfaced."""
        armed(tmp_path)
        repo = self._repo(tmp_path)
        H.make_executor(tmp_path, report=self.REPORT, worktree=str(repo), status="reported")
        assert lg.new_reports_for(H.state_root(tmp_path), "lead-1")   # it is pending before
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT
        assert lg.new_reports_for(H.state_root(tmp_path), "lead-1") == []
        assert lg.load_surfaced(H.state_root(tmp_path), "lead-1") == {"exec-1:1"}

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_landed_skip_is_ledgered_once_not_on_every_stop(self, drv, tmp_path):
        """The consequence of the stamp: a second Stop hook has nothing left to skip, so it neither
        re-runs `report_landed` nor writes a second `wake_skipped_landed`."""
        armed(tmp_path)
        repo = self._repo(tmp_path)
        H.make_executor(tmp_path, report=self.REPORT, worktree=str(repo), status="reported")
        drv(STOP, stop_payload(tmp_path), tmp_path)
        drv(STOP, stop_payload(tmp_path), tmp_path)
        assert H.ledger_events(tmp_path).count("wake_skipped_landed") == 1

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_unlanded_report_is_not_stamped_surfaced(self, drv, tmp_path):
        """The stamp is the landed path's alone: a real wake still goes through the two-phase
        pending → proven-delivery promotion, never straight to surfaced."""
        armed(tmp_path)
        repo = self._repo(tmp_path, dirty=True)
        H.make_executor(tmp_path, report=self.REPORT, worktree=str(repo), status="reported")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE
        assert lg.load_surfaced(H.state_root(tmp_path), "lead-1") == set()

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_report_claiming_nothing_still_wakes(self, drv, tmp_path):
        """"No claims" is not evidence of landing — an ops report must still reach its lead."""
        armed(tmp_path)
        repo = self._repo(tmp_path)
        H.make_executor(tmp_path, report="Investigated; nothing staged.\n", worktree=str(repo),
                        status="reported")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE


class TestStopHookOwnershipScoping:
    """new_reports_for:1077-1090 — "ONLY reports from executors this lead owns ... Another lead's
    executors and UNOWNED ones ... never wake this lead"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_another_leads_executor_never_wakes_this_lead(self, drv, tmp_path):
        armed(tmp_path, "lead-1")
        lg.write_marker(H.state_root(tmp_path), "lead-2")
        H.make_executor(tmp_path, owner_lead="lead-2", status="reported")
        assert drv(STOP, stop_payload(tmp_path, sid="lead-1"), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_unowned_executor_never_wakes_a_lead(self, drv, tmp_path):
        """"otherwise every stale unowned report on the machine would spam every new lead"."""
        armed(tmp_path)
        H.make_executor(tmp_path, owner_lead=None, status="reported")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("status", ["closed", "superseded"])
    def test_closed_and_superseded_executors_never_nag(self, drv, status, tmp_path):
        """Field bug 2026-08-21 — "A closed/superseded executor is a DELIBERATE 'done with it'
        (manual close, retire, or auto-close) — its reports must never nag again"
        (new_reports_for:1091-1098).

        NOTE, so nobody reads more into this test than it proves: the outcome is enforced TWICE,
        independently — `executor_reports:781` already drops a closed/superseded session before
        `new_reports_for` ever looks at it, and `new_reports_for:1091-1098` drops it again. This
        test pins the OBSERVABLE contract (no wake), so it stays green if either filter is removed
        and only fails if BOTH are. Verified by mutation: deleting "superseded" from the
        new_reports_for filter alone does not fail it. The belt-and-braces is deliberate in the
        source, so that is the right thing to assert here; the sibling test below is the one that
        pins the filter new_reports_for uniquely owns (ownership)."""
        armed(tmp_path)
        H.make_executor(tmp_path, status=status)
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("status", ["closed", "superseded"])
    def test_a_closed_executor_is_dropped_by_new_reports_for_too(self, drv, status, tmp_path):
        """The half `executor_reports` does NOT cover, so the second filter is genuinely tested:
        the field bug was that a closed executor kept waking its lead "even when the surfaced stamp
        was refused ... or an older packet's key was never stamped". Here the executor is closed
        AND owned by this lead AND has an unsurfaced report — the exact shape that used to nag —
        and the helper the hook actually calls must return nothing for it."""
        armed(tmp_path)
        H.make_executor(tmp_path, status=status)
        root = H.state_root(tmp_path)
        assert lg.load_surfaced(root, "lead-1") == set()      # nothing stamped: it WOULD be fresh
        assert lg.new_reports_for(root, "lead-1") == []
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_dead_executor_with_an_unseen_report_still_wakes(self, drv, tmp_path):
        """Same block: "`dead` stays nag-worthy ON PURPOSE — a crash with an unseen report is
        exactly what the wake exists for"."""
        armed(tmp_path)
        H.make_executor(tmp_path, status="dead")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_already_surfaced_report_does_not_wake_again(self, drv, tmp_path):
        """"never announces the same report twice" — a key in surfaced_reports.json is done."""
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        lg.mark_surfaced(H.state_root(tmp_path), "lead-1", ["exec-1:1"])
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_report_the_lead_already_verified_does_not_wake_again(self, drv, tmp_path):
        """Backlog row 80 (gate-200944-r2 packet 001): the same `reported` wake re-fired after the
        lead's review fork had already run `relay verify` on the packet. Drives the real CLI as the
        owning lead (CLAUDE_CODE_SESSION_ID=lead-1) against the same tmp HOME the hook reads: the
        first Stop announces (pending), verify picks it up, the next Stop is silent."""
        import subprocess
        armed(tmp_path)
        repo = H.git_repo(tmp_path / "wt")
        H.git_commit(repo, "init", filename="src.py", body="one\n")
        (repo / "src.py").write_text("changed\n")
        subprocess.run(["git", "-C", str(repo), "add", "src.py"], check=True)
        H.make_executor(tmp_path, status="reported", worktree=str(repo),
                        report="Changed src.py; staged.\n\nStatus: clean\nRisk flags: none\n"
                               "UNVERIFIED: none\nChanged: src.py\n")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE

        bin_dir, log = H.stub_bin(tmp_path)
        env = H._hook_env(tmp_path, log, bin_dir, extra={"CLAUDE_CODE_SESSION_ID": "lead-1"})
        subprocess.run([sys.executable, str(H.REPO_ROOT / "bin" / "relay"), "verify", "exec-1"],
                       env=env, capture_output=True, text=True, timeout=60)
        root = H.state_root(tmp_path)
        assert lg.load_surfaced(root, "lead-1") == {"exec-1:1"}
        assert "exec-1:1" not in lg.load_pending(root, "lead-1")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_new_packets_report_wakes_even_after_the_previous_one_was_seen(self, drv, tmp_path):
        """The key is `<sid>:<packet>`, so packet 2 is a fresh event for an executor whose packet 1
        was already surfaced."""
        armed(tmp_path)
        H.make_executor(tmp_path, packet=2, status="reported")
        lg.mark_surfaced(H.state_root(tmp_path), "lead-1", ["exec-1:1"])
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE and "packet 002" in run.stderr


# =================================================================================================
# #22/#23 — the two-phase stamp: announced ≠ delivered
# =================================================================================================

class TestStopHookTwoPhaseStamp:
    """lead_guard:813-827 — "An announce records the keys as PENDING, which does NOT suppress a
    later announce — so an undelivered wake naturally retries on the lead's next Stop. Only PROVEN
    delivery promotes pending → surfaced"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_announce_marks_pending_never_surfaced(self, drv, tmp_path):
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE
        root = H.state_root(tmp_path)
        assert lg.load_surfaced(root, "lead-1") == set()
        assert lg.load_pending(root, "lead-1") == {"exec-1:1": {"announces": 1}}

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_undelivered_wake_retries_on_the_next_stop(self, drv, tmp_path):
        """The §13 bug in reverse: a wake that fired but was dropped MUST be re-announced."""
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE
        assert lg.load_pending(H.state_root(tmp_path), "lead-1")["exec-1:1"]["announces"] == 2

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_retry_is_capped_and_the_cap_is_ledgered(self, drv, tmp_path):
        """WAKE_RETRY_CAP:828-830 — "give up (and stamp) after this many unproven announces — a
        lead whose harness never sets stop_hook_active must not be re-announced at forever"."""
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        codes = [drv(STOP, stop_payload(tmp_path), tmp_path).returncode for _ in range(4)]
        assert codes == [WAKE, WAKE, WAKE, SILENT]
        root = H.state_root(tmp_path)
        assert lg.load_pending(root, "lead-1") == {}
        assert lg.load_surfaced(root, "lead-1") == {"exec-1:1"}
        capped = [r for r in H.ledger(tmp_path) if r["event"] == "wake_retry_capped"]
        assert capped and capped[-1]["keys"] == ["exec-1:1"]

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_relay_own_continuation_promotes_pending_to_surfaced(self, drv, tmp_path):
        """relay_announce_delivered:992-1011 — a stop_hook_active re-run with relay's OWN
        outstanding claim is proof; promote_pending then stamps and `wake_delivered` is ledgered."""
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE
        run = drv(STOP, stop_payload(tmp_path, stop_hook_active=True), tmp_path)
        assert run.returncode == SILENT
        root = H.state_root(tmp_path)
        assert lg.load_surfaced(root, "lead-1") == {"exec-1:1"}
        assert lg.load_pending(root, "lead-1") == {}
        delivered = [r for r in H.ledger(tmp_path) if r["event"] == "wake_delivered"]
        assert delivered and delivered[-1]["keys"] == ["exec-1:1"]

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_foreign_hooks_continuation_promotes_nothing(self, drv, tmp_path):
        """THE #23 INCIDENT (lead_guard:906-917, 2026-07-22): "Claude Code sets that flag whenever
        the session was continued by ANY blocking Stop hook ... every post-block turn looked like
        relay's own post-wake re-run ... Two executor reports sat silent for ~2 hours."

        With NO outstanding relay claim, a stop_hook_active run must promote nothing AND must still
        announce the unsurfaced report."""
        root = armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE
        lg.clear_announce_claim(root, "lead-1")     # somebody else's continuation, not ours
        run = drv(STOP, stop_payload(tmp_path, stop_hook_active=True), tmp_path)
        assert run.returncode == WAKE, "the report must be re-announced, not silently deduped"
        assert lg.load_surfaced(root, "lead-1") == set()
        assert lg.load_pending(root, "lead-1")["exec-1:1"]["announces"] == 2

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_claim_with_a_transcript_needs_the_needle_in_it(self, drv, tmp_path):
        """"an outstanding claim whose wake text ACTUALLY REACHED the transcript". A transcript
        that never received the wake is not proof, so the key stays pending."""
        root = armed(tmp_path)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("some unrelated entry\n")
        H.make_executor(tmp_path, status="reported")
        assert drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)),
                   tmp_path).returncode == WAKE
        run = drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript),
                                     stop_hook_active=True), tmp_path)
        assert run.returncode == WAKE
        assert lg.load_surfaced(root, "lead-1") == set()

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_needle_in_the_transcript_proves_delivery(self, drv, tmp_path):
        """The other half: once relay's own wake text lands past the recorded offset, the claim is
        honoured and the report is stamped surfaced."""
        root = armed(tmp_path)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("older entry\n")
        H.make_executor(tmp_path, status="reported")
        assert drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)),
                   tmp_path).returncode == WAKE
        with open(transcript, "a") as f:            # the wake reached the lead's transcript
            f.write(json.dumps({"type": "user", "text": lg.WAKE_DELIVERY_NEEDLE}) + "\n")
        run = drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript),
                                     stop_hook_active=True), tmp_path)
        assert run.returncode == SILENT
        assert lg.load_surfaced(root, "lead-1") == {"exec-1:1"}

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_needle_written_before_the_claim_offset_is_not_proof(self, drv, tmp_path):
        """record_announce_claim:954-963 — "Records where the transcript ENDS at this moment, so
        the later delivery check only ever matches text written AFTER this announce." An older
        wake's text must not certify a newer announce."""
        root = armed(tmp_path)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(lg.WAKE_DELIVERY_NEEDLE + " (from an earlier, already-handled wake)\n")
        H.make_executor(tmp_path, status="reported")
        assert drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)),
                   tmp_path).returncode == WAKE
        run = drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript),
                                     stop_hook_active=True), tmp_path)
        assert run.returncode == WAKE
        assert lg.load_surfaced(root, "lead-1") == set()

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_claim_answers_exactly_one_continuation(self, drv, tmp_path):
        """"One-shot: the claim is consumed either way (a claim answers exactly one continuation;
        a retry writes a fresh one)"."""
        root = armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        drv(STOP, stop_payload(tmp_path), tmp_path)
        assert lg.load_announce_claim(root, "lead-1") != {}
        drv(STOP, stop_payload(tmp_path, stop_hook_active=True), tmp_path)
        assert lg.load_announce_claim(root, "lead-1") == {}

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_external_channel_stamp_stops_the_retry(self, drv, tmp_path):
        """mark_surfaced:799-810 — the #17 channels (`relay check`/`diff`/`close`) prove delivery
        another way and drop the pending entry, so the wake stops retrying."""
        root = armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        drv(STOP, stop_payload(tmp_path), tmp_path)
        lg.mark_surfaced(root, "lead-1", ["exec-1:1"])
        assert lg.load_pending(root, "lead-1") == {}
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT


# =================================================================================================
# App 2 — commits the lead made this turn
# =================================================================================================

class TestStopHookCommitSurfacing:
    """stop_lead_watch:11-12 — "App 2 — the lead made NEW commit(s) this turn (covers the
    Bash/`git commit` vector the PreToolUse edit-gate can't see)". LEAD_DEFAULTS:33-35 /
    README:633-650: `surface_commits` is default OFF."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_commits_are_not_surfaced_by_default(self, drv, tmp_path):
        """"OFF by default — waking the lead about its OWN (often user-approved) commits reads as
        a spurious 'review needed'"."""
        armed(tmp_path)
        repo = H.git_repo(tmp_path / "repo")
        H.git_commit(repo, "first")
        assert drv(STOP, stop_payload(tmp_path, cwd=str(repo)), tmp_path).returncode == SILENT
        H.git_commit(repo, "second")
        assert drv(STOP, stop_payload(tmp_path, cwd=str(repo)), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_opted_in_commits_wake_once_each(self, drv, tmp_path):
        """"surface each commit once" (stop_lead_watch:277): the recorded HEAD advances, so the
        same commit never wakes the lead twice."""
        armed(tmp_path, surface_commits=True)
        repo = H.git_repo(tmp_path / "repo")
        H.git_commit(repo, "baseline")
        assert drv(STOP, stop_payload(tmp_path, cwd=str(repo)), tmp_path).returncode == SILENT
        H.git_commit(repo, "the interesting one")
        run = drv(STOP, stop_payload(tmp_path, cwd=str(repo)), tmp_path)
        assert run.returncode == WAKE
        assert "you made 1 commit(s) this turn" in run.stderr
        assert "the interesting one" in run.stderr
        assert drv(STOP, stop_payload(tmp_path, cwd=str(repo)), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_head_is_recorded_even_when_surfacing_is_off(self, drv, tmp_path):
        """The HEAD stamp advances regardless of `surface_commits` (stop_lead_watch:276-277), so
        turning the option ON later cannot flood the lead with a backlog of old commits."""
        armed(tmp_path)
        repo = H.git_repo(tmp_path / "repo")
        H.git_commit(repo, "one")
        drv(STOP, stop_payload(tmp_path, cwd=str(repo)), tmp_path)
        head = lg.read_head(H.state_root(tmp_path), "lead-1")
        assert len(head) == 40
        H.write_config(tmp_path, surface_commits=True, auto_close=False, poll_seconds=1)
        assert drv(STOP, stop_payload(tmp_path, cwd=str(repo)), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_non_git_cwd_is_silent(self, drv, tmp_path):
        """git_head:1125-1134 returns "" outside a repo — no baseline, nothing to surface."""
        armed(tmp_path, surface_commits=True)
        run = drv(STOP, stop_payload(tmp_path, cwd=str(tmp_path)), tmp_path)
        assert run.returncode == SILENT
        assert lg.read_head(H.state_root(tmp_path), "lead-1") == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_missing_cwd_falls_back_to_the_process_cwd(self, drv, tmp_path):
        """stop_lead_watch:268 — `payload.get("cwd") or os.getcwd()`; a payload with no cwd must
        not raise."""
        armed(tmp_path, surface_commits=True)
        assert drv(STOP, {"session_id": "lead-1"}, tmp_path).returncode in (SILENT, WAKE)


# =================================================================================================
# The one-time handoff nudge
# =================================================================================================

class TestStopHookHandoffNudge:
    """stop_lead_watch:14-17 and README:455-470 — token-first, MB as the secondary "session age"
    signal; "Fires ONCE ever per lead"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_transcript_mb_trips_the_nudge_once_ever(self, drv, tmp_path):
        armed(tmp_path, handoff_nudge_mb=0.05)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 200_000)                     # ~0.19MB
        run = drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)), tmp_path)
        assert run.returncode == WAKE
        assert "this lead session is getting heavy" in run.stderr
        assert "/relay:handoff" in run.stderr
        assert "several compactions in" in run.stderr
        assert lg.handoff_nudged(H.state_root(tmp_path), "lead-1") is True
        nudged = [r for r in H.ledger(tmp_path) if r["event"] == "handoff_nudged"]
        assert len(nudged) == 1
        # ...and never again, however heavy it gets.
        transcript.write_text("x" * 5_000_000)
        assert drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)),
                   tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_live_context_tokens_are_the_primary_signal(self, drv, tmp_path):
        """"token-first: say the real signal when it's the one that tripped" (L303-305). A SMALL
        transcript whose last request carried a lot of live context must still nudge, and the
        reading must quote tokens, not MB."""
        armed(tmp_path, lead_nudge_tokens=1000, context_nudge_tokens=1000, handoff_nudge_mb=1000)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant", "timestamp": "2026-09-05T10:00:00Z",
            "message": {"id": "msg_1", "model": "claude-opus-5",
                        "usage": {"input_tokens": 500, "cache_read_input_tokens": 900,
                                  "cache_creation_input_tokens": 100, "output_tokens": 10}},
        }) + "\n")
        run = drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)), tmp_path)
        assert run.returncode == WAKE
        # A LEAD's own reading names the line and the window (lead_guard.lead_nudge_reading_text):
        # this marker carries no `model`, so lead_window_for's window is genuinely unknown (None,
        # never guessed at 200_000 — see backlog row 49 / the None-window bugfix), which leaves the
        # line UNCAPPED at lead_nudge_tokens (pinned to 1000 here, this test's config) — see
        # lead_guard.lead_nudge_threshold. The unknown window renders as "?".
        assert "1.5k live, line 1.0k on a ? window" in run.stderr
        assert "MB transcript" not in run.stderr
        tok = [r for r in H.ledger(tmp_path) if r["event"] == "handoff_nudged"][0]
        assert tok["tokens"] == 1500

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_light_session_is_not_nudged(self, drv, tmp_path):
        armed(tmp_path)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("tiny\n")
        run = drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)), tmp_path)
        assert run.returncode == SILENT
        assert lg.handoff_nudged(H.state_root(tmp_path), "lead-1") is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_handoff_nudge_kill_switch(self, drv, tmp_path):
        """LEAD_DEFAULTS `handoff_nudge` (L52)."""
        armed(tmp_path, handoff_nudge=False, handoff_nudge_mb=0.001)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 200_000)
        assert drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)),
                   tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_nudge_rides_an_existing_report_wake(self, drv, tmp_path):
        """"rides whatever wake already fires below rather than opening a second exit-2 path"
        (L287-288): one wake carries both the report and the nudge."""
        armed(tmp_path, handoff_nudge_mb=0.05)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x" * 200_000)
        H.make_executor(tmp_path, status="reported")
        run = drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)), tmp_path)
        assert run.returncode == WAKE
        assert "executor 'exec-1' reported" in run.stderr
        assert "getting heavy" in run.stderr

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_missing_transcript_path_never_nudges(self, drv, tmp_path):
        """transcript_mb:1013-1023 returns 0.0 for a None path — no reading, no nudge."""
        armed(tmp_path, handoff_nudge_mb=0.05)
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_unparseable_transcript_does_not_break_the_hook(self, drv, tmp_path):
        armed(tmp_path, handoff_nudge_mb=0.05)
        transcript = tmp_path / "t.jsonl"
        transcript.write_bytes(b"\x00\xff not jsonl at all\n" * 20000)
        run = drv(STOP, stop_payload(tmp_path, transcript_path=str(transcript)), tmp_path)
        assert run.returncode == WAKE      # MB still trips it; the unreadable usage is tolerated
        assert "MB transcript" in run.stderr


# =================================================================================================
# Backlog row 87 — the 🟠 "approaching heavy" line on the lead's own wake
# =================================================================================================

class TestStopHookCtxWarnWake:
    """stop_lead_watch.py's `_ctx_warn_lines` — the sibling of row 57's desktop banner
    (bin/relay's `_maybe_warn_ctx_heavy`) that rides the LEAD's own Stop-hook wake instead of only
    a footnote/banner the lead has to go looking for. One 🟠 line per OWNED executor past
    `context_warn_tokens` but below `context_nudge_tokens`, once per executor sid."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_owned_executor_past_the_warn_line_gets_the_line(self, drv, tmp_path):
        armed(tmp_path, context_warn_tokens=120000, context_nudge_tokens=150000)
        H.make_executor(tmp_path, status="busy", report=None, claude_session="exec-1-cs")
        H.executor_transcript(tmp_path, "exec-1-cs", 125_000)
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE
        assert "\U0001f7e0 exec-1 is approaching heavy" in run.stderr
        assert "125k ctx" in run.stderr
        assert "warn line 120k" in run.stderr
        assert "rotate line 150k" in run.stderr
        assert "relay retire exec-1" in run.stderr
        assert "surface that line verbatim" in run.stderr
        # The wake's own once-only key is claimed — a second attempt at the SAME key must fail.
        assert lg.claim_notification(H.state_root(tmp_path), "lead-1", "exec-1:ctx-warn-wake") is False

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_second_stop_does_not_repeat_it(self, drv, tmp_path):
        armed(tmp_path, context_warn_tokens=120000, context_nudge_tokens=150000)
        H.make_executor(tmp_path, status="busy", report=None, claude_session="exec-1-cs")
        H.executor_transcript(tmp_path, "exec-1-cs", 125_000)
        first = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert first.returncode == WAKE
        second = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert "\U0001f7e0" not in second.stderr

    @pytest.mark.parametrize("tokens", [119_000, 155_000], ids=["below-warn", "past-rotate"])
    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_outside_the_warn_band_produces_no_line(self, drv, tokens, tmp_path):
        armed(tmp_path, context_warn_tokens=120000, context_nudge_tokens=150000)
        H.make_executor(tmp_path, status="busy", report=None, claude_session="exec-1-cs")
        H.executor_transcript(tmp_path, "exec-1-cs", tokens)
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert "\U0001f7e0" not in run.stderr

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_unowned_executor_never_gets_the_line(self, drv, tmp_path):
        armed(tmp_path, context_warn_tokens=120000, context_nudge_tokens=150000)
        H.make_executor(tmp_path, status="busy", report=None, owner_lead="lead-2",
                         claude_session="exec-1-cs")
        H.executor_transcript(tmp_path, "exec-1-cs", 125_000)
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert "\U0001f7e0" not in run.stderr

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_ctx_warn_wake_kill_switch(self, drv, tmp_path):
        """LEAD_DEFAULTS `ctx_warn_wake` — a separate switch from `notify_on_wake` (README/row 87):
        false here silences only the wake line, not the banner/footnote/board chip."""
        armed(tmp_path, context_warn_tokens=120000, context_nudge_tokens=150000,
              ctx_warn_wake=False)
        H.make_executor(tmp_path, status="busy", report=None, claude_session="exec-1-cs")
        H.executor_transcript(tmp_path, "exec-1-cs", 125_000)
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert "\U0001f7e0" not in run.stderr
        assert run.returncode == SILENT

    def test_a_wake_of_only_ctx_warn_lines_skips_the_banner_tier(self, tmp_path):
        """Item 2: row 57's `_maybe_warn_ctx_heavy` already fired this sid's ONE desktop banner
        (from `_check_one`, under its own `<sid>:ctx-warn` key) — a wake whose `lines` are ALL 🟠
        must not fire a second banner for it here."""
        armed(tmp_path, context_warn_tokens=120000, context_nudge_tokens=150000)
        H.make_executor(tmp_path, status="busy", report=None, claude_session="exec-1-cs")
        H.executor_transcript(tmp_path, "exec-1-cs", 125_000)
        run = H.run_hook(STOP, stop_payload(tmp_path), tmp_path, no_notify=False)
        assert run.returncode == WAKE
        assert "\U0001f7e0 exec-1 is approaching heavy" in run.stderr
        assert H.stub_calls(tmp_path / "stub-calls.log") == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_line_rides_an_existing_report_wake(self, drv, tmp_path):
        armed(tmp_path, context_warn_tokens=120000, context_nudge_tokens=150000)
        H.make_executor(tmp_path, status="reported", claude_session="exec-1-cs")
        H.executor_transcript(tmp_path, "exec-1-cs", 125_000)
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        assert run.returncode == WAKE
        assert "executor 'exec-1' reported" in run.stderr
        assert "\U0001f7e0 exec-1 is approaching heavy" in run.stderr


# =================================================================================================
# The announce instruction — posture-aware (SKILL.md §6f / task #16)
# =================================================================================================

class TestStopHookAnnounceInstruction:

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_manual_posture_says_announce_and_wait(self, drv, tmp_path):
        """SKILL.md:104-110 and hooks.json's `rewakeMessage` — the default posture is
        announce-and-WAIT: "Do NOT auto-review, auto-commit, or otherwise act"."""
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        err = drv(STOP, stop_payload(tmp_path), tmp_path).stderr
        assert "WAIT for their direction" in err
        assert "Do NOT auto-review, auto-commit" in err
        assert "AUTONOMOUS MODE" not in err

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_autonomous_posture_swaps_the_instruction(self, drv, tmp_path):
        """stop_lead_watch:126-129 — "This text is injected at the exact beat autonomous mode
        redefines ... a hardcoded 'WAIT / do NOT act' would silently override the posture the user
        just granted". The posture is read from the MARKER (autonomous_state:507-519)."""
        root = armed(tmp_path)
        lg.set_autonomous(root, "lead-1", True)
        H.make_executor(tmp_path, status="reported")
        err = drv(STOP, stop_payload(tmp_path), tmp_path).stderr
        assert "AUTONOMOUS MODE" in err
        assert "announce, ACT, and record" in err

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_autonomous_wake_still_carries_the_commit_gate(self, drv, tmp_path):
        """SKILL.md:150-167 / README:382-402 — autonomy does NOT relax the five-condition
        auto-commit gate, and the wake text is where the lead reads it."""
        root = armed(tmp_path)
        lg.set_autonomous(root, "lead-1", True)
        H.make_executor(tmp_path, status="reported")
        err = drv(STOP, stop_payload(tmp_path), tmp_path).stderr
        assert "ALL FIVE hold" in err
        assert "--for-autocommit --in-plan --diff-reviewed" in err
        assert "clean-with-caveats STOPS" in err
        assert "COUNTS-MATCH never means the report is true" in err

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_autonomous_off_is_the_default_for_a_fresh_marker(self, drv, tmp_path):
        """LEAD_DEFAULTS `autonomous_mode` False — "the safe default, and it must stay the
        default"; a marker with no key at all reads as manual (autonomous_state:517-519)."""
        root = armed(tmp_path)
        m = lg.read_marker(root, "lead-1")
        m.pop("autonomous", None)
        lg.marker_path(root, "lead-1").write_text(json.dumps(m))
        H.make_executor(tmp_path, status="reported")
        assert "AUTONOMOUS MODE" not in drv(STOP, stop_payload(tmp_path), tmp_path).stderr


# =================================================================================================
# Notifications
# =================================================================================================

class TestStopHookNotification:
    """README:736 — "RELAY_NO_NOTIFY: suppress all notification banners"; LEAD_DEFAULTS
    `notify_on_wake`. Both are asserted against STUB binaries first on PATH, so the test can prove
    a banner was or was not attempted without one ever reaching the desktop."""

    def test_relay_no_notify_suppresses_every_banner(self, tmp_path):
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        run = H.run_hook(STOP, stop_payload(tmp_path), tmp_path)   # RELAY_NO_NOTIFY=1 by default
        assert run.returncode == WAKE
        assert H.stub_calls(tmp_path / "stub-calls.log") == []

    def test_notify_on_wake_false_suppresses_every_banner(self, tmp_path):
        """_notify:59-60 checks the config key BEFORE the env kill-switch."""
        armed(tmp_path, notify_on_wake=False)
        H.make_executor(tmp_path, status="reported")
        run = H.run_hook(STOP, stop_payload(tmp_path), tmp_path, no_notify=False)
        assert run.returncode == WAKE
        assert H.stub_calls(tmp_path / "stub-calls.log") == []

    def test_a_wake_does_post_a_banner_when_nothing_suppresses_it(self, tmp_path):
        """The positive control that makes the two tests above meaningful: with neither switch
        set, tier 2 (`terminal-notifier`) fires, titled by project and clickable back to the lead
        (README:435-441)."""
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        run = H.run_hook(STOP, stop_payload(tmp_path), tmp_path, no_notify=False)
        assert run.returncode == WAKE
        calls = " ".join(H.stub_calls(tmp_path / "stub-calls.log"))
        assert "terminal-notifier" in calls
        assert "relay · proj" in calls                  # title names the project
        assert "-group relay-lead-1" in calls           # coalesces per lead
        assert "focus lead-1" in calls                  # click jumps to the lead's tab
        assert "osascript" not in calls                 # tier 3 is a FALLBACK, not an extra banner

    def test_notification_never_touches_the_real_desktop_tools(self, tmp_path):
        """The stub dir is first on PATH and lead_guard.find_terminal_notifier probes PATH first
        (L209-220), so even the "no kill-switch" path is contained. This test pins that: the
        binary actually invoked is the stub inside tmp_path."""
        armed(tmp_path)
        H.make_executor(tmp_path, status="reported")
        H.run_hook(STOP, stop_payload(tmp_path), tmp_path, no_notify=False)
        for call in H.stub_calls(tmp_path / "stub-calls.log"):
            if call.startswith("/"):
                assert call.startswith(str(tmp_path)), call
