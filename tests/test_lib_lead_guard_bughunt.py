"""
Bug-hunt tests for lib/lead_guard.py — the quarter tests/test_lead_guard.py leaves uncovered.

Almost every function in this module carries an explicit robustness promise in its own docstring
("Never raises", "Fully defensive", "degrades to X", "a single bad marker must never blank the
whole list"). Those promises are the contract this file tests: the hooks that call them are
fail-open by design (hooks/pretool_route_guard.py: "any error … → exit 0 (allow)"), so a helper
that raises where it promised not to turns a visible failure into a silent one.

Part 1 of 3 — state on disk: config, gate sizing, the Bash taxonomy, markers, tombstones, the
grace window, the ledger and the wake bookkeeping. Part 2 is the pure policy layer
(tests/test_lib_lead_guard_policy_bughunt.py); part 3 is locks, escalation and peer addressing
(tests/test_lib_lead_guard_locks_bughunt.py).

Run: pytest tests/test_lib_lead_guard_bughunt.py -v
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
import lead_guard as lg  # noqa: E402


@pytest.fixture
def sr(tmp_path):
    """An isolated state root. Never ~/.relay-tasks."""
    root = tmp_path / ".relay-tasks"
    root.mkdir()
    return root


@pytest.fixture
def a_file(tmp_path):
    """A path that is a FILE — using it as a state_root makes every mkdir/write below it fail,
    which is how the 'never raises' branches get exercised without monkeypatching."""
    p = tmp_path / "not-a-dir"
    p.write_text("x")
    return p


def armed(sr, sid="lead-1", **fields):
    lg.write_marker(sr, sid, project="proj", cwd="/tmp", color=[1, 2, 3], **fields)
    return sid


def make_executor(sr, name, *, status="reported", packet=1, owner=None, report=True):
    d = sr / name
    (d / "packets").mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(json.dumps(
        {"session_id": name, "status": status, "current_packet": packet, "owner_lead": owner}))
    if report:
        (d / "packets" / f"{packet:03d}-report.md").write_text("done.\n")
    return d


# ── config (README: 'missing keys fall back to defaults; unknown keys are ignored') ────────────
class TestConfig:
    def test_missing_corrupt_and_non_dict_config_all_give_pure_defaults(self, sr):
        """load_config: 'Unknown keys ignored; missing/corrupt file → pure defaults. Never
        throws.'"""
        assert lg.load_config(sr) == lg.LEAD_DEFAULTS
        (sr / "lead").mkdir()
        for content in ("{ not json", "[]", '"a string"', "null"):
            lg.config_path(sr).write_text(content)
            assert lg.load_config(sr) == lg.LEAD_DEFAULTS, content

    def test_unknown_keys_are_ignored_and_known_ones_win(self, sr):
        (sr / "lead").mkdir()
        lg.config_path(sr).write_text(json.dumps({"edit_line_threshold": 5, "nonsense": True}))
        cfg = lg.load_config(sr)
        assert cfg["edit_line_threshold"] == 5 and "nonsense" not in cfg

    def test_load_config_never_throws_on_an_unreadable_file(self, sr):
        (sr / "lead").mkdir()
        lg.config_path(sr).mkdir()          # a directory where a file is expected
        assert lg.load_config(sr) == lg.LEAD_DEFAULTS

    def test_an_ill_typed_threshold_is_carried_through_verbatim(self, sr):
        """AMBIGUOUS-lib-2: README promises only that unknown keys are ignored, so a known key
        with the WRONG TYPE is neither ignored nor usable. This pins the observable half — the
        value reaches callers unconverted — so the finding stays visible if it is ever fixed."""
        (sr / "lead").mkdir()
        lg.config_path(sr).write_text(json.dumps({"edit_line_threshold": "40"}))
        cfg = lg.load_config(sr)
        assert cfg["edit_line_threshold"] == "40"
        with pytest.raises(TypeError):
            lg.exceeds_gate(50, False, cfg)


# ── edit sizing + gate exemption (hooks/pretool_route_guard.py's inputs) ───────────────────────
class TestEditSizing:
    @pytest.mark.parametrize("tool,inp,want", [
        ("Write", {"content": ""}, 0),
        ("Write", {"content": "one"}, 1),
        ("Write", {"content": "a\nb\nc"}, 3),
        ("Write", {"content": "a\nb\n"}, 3),          # a trailing newline reads as an empty line
        ("Edit", {"new_string": "x\ny"}, 2),
        ("Edit", {}, 0),
        ("MultiEdit", {"edits": [{"new_string": "a"}, {"new_string": "b\nc"}, "junk"]}, 3),
        ("MultiEdit", {"edits": None}, 0),
        ("NotebookEdit", {"content": "a\nb"}, 0),      # unknown tool → 0
    ])
    def test_edit_line_count_shapes(self, tool, inp, want):
        """edit_line_count: 'Write → its content; Edit → new_string; MultiEdit → sum of each
        edit's new_string. Any unexpected shape degrades to 0 (fail-open …).'"""
        assert lg.edit_line_count(tool, inp) == want

    def test_edit_line_count_degrades_to_zero_on_a_hostile_input(self):
        """'Any unexpected shape degrades to 0 (fail-open: an unparseable edit is never
        blocked — under-counting is the safe direction).'"""
        assert lg.edit_line_count("Write", None) == 0
        assert lg.edit_line_count("Edit", "not-a-dict") == 0

    def test_is_new_file_never_raises_and_says_no_when_it_cannot_tell(self, tmp_path):
        """is_new_file: any error → False, i.e. never gate on a guess."""
        assert lg.is_new_file({"file_path": str(tmp_path / "nope.py")}) is True
        existing = tmp_path / "there.py"
        existing.write_text("x")
        assert lg.is_new_file({"file_path": str(existing)}) is False
        assert lg.is_new_file({}) is False
        assert lg.is_new_file(None) is False
        assert lg.is_new_file({"file_path": object()}) is False

    def test_gate_exemption_covers_the_state_root_and_packet_naming(self, sr, tmp_path):
        """is_gate_exempt: 'anything under the relay state root …, or a packet file by naming
        convention (*-packet.md) wherever the lead chose to draft it … any error → not exempt.'"""
        assert lg.is_gate_exempt(sr, str(sr / "bh" / "packets" / "001-packet.md")) is True
        assert lg.is_gate_exempt(sr, str(tmp_path / "drafts" / "007-packet.md")) is True
        assert lg.is_gate_exempt(sr, str(tmp_path / "src" / "app.py")) is False
        assert lg.is_gate_exempt(sr, "") is False
        assert lg.is_gate_exempt(sr, None) is False
        assert lg.is_gate_exempt(sr, object()) is False

    def test_exceeds_gate_reads_the_threshold_and_the_new_file_switch(self):
        cfg = dict(lg.LEAD_DEFAULTS)
        assert lg.exceeds_gate(cfg["edit_line_threshold"], False, cfg) is True
        assert lg.exceeds_gate(cfg["edit_line_threshold"] - 1, False, cfg) is False
        assert lg.exceeds_gate(1, True, cfg) is True
        assert lg.exceeds_gate(1, True, dict(cfg, block_on_new_file=False)) is False


# ── the Bash taxonomy (§10: permissive on custody, strict on provisioning) ─────────────────────
class TestBashTaxonomy:
    @pytest.mark.parametrize("cmd", [
        "git commit -m x", "git push origin main", "systemctl restart api",
        "clickhouse-client -q 'select 1'", "pytest tests/ -q", "npm test", "npm run test:unit",
        "make test", "cargo test", "go test ./...", "tox -e py39",
        "cat README.md", "grep -rn foo lib/", "git status"])
    def test_custody_and_reads_free_pass(self, cmd):
        """classify_bash_command: custody verbs and unclassified reads both return None —
        'this function never signals "block", only "log or don't"'."""
        assert lg.classify_bash_command(cmd) is None

    @pytest.mark.parametrize("cmd,rule", [
        ("npm install lodash", "npm-install"), ("npm ci", "npm-install"),
        ("npm run build", "npm-run-build"), ("yarn add react", "package-install"),
        ("pip3 install ruff", "package-install"), ("tsc -p .", "compiler"),
        ("git clone https://x/y", "git-clone"), ("sed -i '' s/a/b/ f", "sed-inplace"),
        ("cat <<EOF > f", "heredoc"), ("echo x | tee /etc/f", "tee-mutation"),
        ("rsync -a a/ b/", "rsync"), ("systemctl daemon-reload", "service-file-write")])
    def test_implementation_verbs_are_named(self, cmd, rule):
        assert lg.classify_bash_command(cmd) == rule

    def test_custody_wins_over_implementation_on_overlap(self):
        """'CUSTODY_RULES are checked first so any pattern overlap resolves toward the
        free-pass, per §10's permissive-on-custody instruction.'"""
        assert lg.classify_bash_command("npm run test:unit && npm run build") is None

    def test_a_non_string_command_degrades_to_unclassified(self):
        """'Never raises: an unparseable/non-string `cmd` degrades to None.'"""
        class Hostile:
            def __str__(self):
                raise ValueError("boom")
        assert lg.classify_bash_command(None) is None
        assert lg.classify_bash_command(Hostile()) is None


# ── marker read/write, tombstones, grace ──────────────────────────────────────────────────────
class TestMarkerState:
    def test_a_truncated_marker_reads_as_empty(self, sr):
        """read_marker returns {} on any error. write_marker is a plain write_text (no
        tmp+rename), so a crash or a full disk mid-write leaves exactly this shape."""
        sid = armed(sr)
        raw = lg.marker_path(sr, sid).read_text()
        lg.marker_path(sr, sid).write_text(raw[:len(raw) // 2])   # a half-written marker
        assert lg.read_marker(sr, sid) == {}

    @pytest.mark.parametrize("content", ["{ truncated", "", "[1, 2]", "null"])
    def test_an_unreadable_marker_is_not_a_lead(self, sr, content):
        """is_lead: 'Marker absent (or any error) → not lead → the hooks fast-exit-allow, which
        is the entire zero-impact path.' A marker whose CONTENT cannot be read is 'any error':
        every other reader (autonomous_state, wake_hook_state, touch_lead, update_marker) treats
        it as absent, so is_lead calling it armed makes the session's state self-contradictory."""
        sid = armed(sr)
        lg.marker_path(sr, sid).write_text(content)
        assert lg.read_marker(sr, sid) == {}
        assert lg.is_lead(sr, sid) is False

    def test_a_marker_that_is_a_directory_reads_as_empty(self, sr):
        lg.marker_path(sr, "ghost").parent.mkdir(parents=True)
        lg.marker_path(sr, "ghost").mkdir()
        assert lg.read_marker(sr, "ghost") == {}

    def test_is_lead_never_raises_on_a_hostile_state_root(self):
        assert lg.is_lead(None, "sid") is False

    def test_two_leads_in_one_project_keep_separate_markers(self, sr):
        """Markers are keyed by session id, so two live leads over the same project must not
        share or clobber state."""
        armed(sr, "lead-a")
        armed(sr, "lead-b")
        assert lg.is_lead(sr, "lead-a") and lg.is_lead(sr, "lead-b")
        lg.set_autonomous(sr, "lead-a", True)
        assert lg.autonomous_state(lg.read_marker(sr, "lead-a")) == (True, "command")
        assert lg.autonomous_state(lg.read_marker(sr, "lead-b")) == (False, "config")
        assert {m["session_id"] for m in lg.list_leads(sr)} == {"lead-a", "lead-b"}

    def test_a_marker_for_a_session_that_no_longer_exists_is_still_listed(self, sr):
        """list_leads returns markers as stored — liveness is a separate signal (`relay list`'s
        ghost/unreachable), not something list_leads may silently drop."""
        armed(sr, "gone")
        assert [m["session_id"] for m in lg.list_leads(sr)] == ["gone"]

    def test_one_malformed_marker_never_blanks_the_list(self, sr):
        """list_leads: 'a single bad marker must never blank the whole list' — strengthened by
        D2 (BUG-lib-8's mitigation): a marker that can't be read as a real dict now shows up as
        its own distinct BROKEN row instead of silently vanishing."""
        armed(sr, "good")
        (sr / "lead" / "bad").mkdir(parents=True)
        (sr / "lead" / "bad" / "marker.json").write_text("{ truncated")
        (sr / "lead" / "listy").mkdir(parents=True)
        (sr / "lead" / "listy" / "marker.json").write_text("[1, 2]")   # valid JSON, wrong shape
        lg.config_path(sr).write_text("{}")                            # not a marker dir
        leads = lg.list_leads(sr)
        assert {m["session_id"] for m in leads} == {"good", "bad", "listy"}
        broken = {m["session_id"]: m for m in leads if m.get("broken")}
        assert set(broken) == {"bad", "listy"}
        assert all(m == {"session_id": sid, "broken": True} for sid, m in broken.items())

    def test_list_leads_sorts_oldest_first_and_tolerates_a_missing_started(self, sr):
        armed(sr, "b", started="2026-01-02T00:00:00")
        armed(sr, "a", started="2026-01-01T00:00:00")
        armed(sr, "nostart")
        m = lg.read_marker(sr, "nostart")
        m.pop("started")
        lg.marker_path(sr, "nostart").write_text(json.dumps(m))
        assert [m["session_id"] for m in lg.list_leads(sr)] == ["nostart", "a", "b"]

    def test_list_leads_never_raises_and_returns_a_list(self):
        assert lg.list_leads(None) == []
        assert lg.list_leads("/definitely/not/here") == []

    def test_update_marker_preserves_every_other_field(self, sr):
        """update_marker: 'the safe counterpart to write_marker, which rewrites the whole marker
        and silently drops anything the caller forgot to re-pass (§1)'."""
        sid = armed(sr, predecessor={"session_id": "old"})
        assert lg.update_marker(sr, sid, tab_label="[Lead] x") is True
        m = lg.read_marker(sr, sid)
        assert m["tab_label"] == "[Lead] x" and m["predecessor"] == {"session_id": "old"}
        assert m["project"] == "proj"

    def test_update_marker_and_touch_lead_are_silent_no_ops_without_a_marker(self, sr):
        assert lg.update_marker(sr, "nobody", x=1) is False
        lg.touch_lead(sr, "nobody")            # must not raise
        assert lg.read_marker(sr, "nobody") == {}

    def test_marker_writers_never_raise_when_the_marker_is_read_only(self, sr):
        """touch_lead/update_marker/tombstone_lead/revive_lead all promise 'nothing here ever
        raises (the Stop hook's fail-open contract must hold even if the heartbeat can't be
        written)'.

        BUG-lib-8's atomic-write fix makes write_marker/touch_lead write via a sibling tmp file +
        os.replace, so blocking THEIR write needs the containing DIRECTORY read-only (a replace
        only needs write permission on the directory, not the target file's own mode — chmod'ing
        just the marker file no longer stops it). update_marker/tombstone_lead/revive_lead still
        write straight onto the marker in place, so chmod'ing the marker file itself still blocks
        those, same as before."""
        sid = armed(sr)
        lead_dir_path = lg.lead_dir(sr, sid)
        lead_dir_path.chmod(0o555)
        try:
            lg.touch_lead(sr, sid)   # must not raise — the marker is simply left untouched
        finally:
            lead_dir_path.chmod(0o755)

        lg.marker_path(sr, sid).chmod(0o444)
        try:
            assert lg.update_marker(sr, sid, x=1) is False
            assert lg.tombstone_lead(sr, sid) is False
        finally:
            lg.marker_path(sr, sid).chmod(0o644)
        lg.tombstone_lead(sr, sid)
        lg.marker_path(sr, sid).chmod(0o444)
        try:
            assert lg.revive_lead(sr, sid) is False
        finally:
            lg.marker_path(sr, sid).chmod(0o644)

    def test_touch_lead_restamps_version_and_timeout_from_the_live_plugin_root(self, sr, tmp_path):
        """touch_lead: 'ALSO re-stamps plugin_version/stop_hook_timeout by reading … from THAT
        root … Only overwrites a stamped field when the freshly-read value is present AND differs.'"""
        sid = armed(sr, plugin_version="0.0.1", stop_hook_timeout=10)
        root = tmp_path / "plugin"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / "hooks").mkdir()
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "9.9.9"}))
        (root / "hooks" / "hooks.json").write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"timeout": 2400}]}]}}))
        lg.touch_lead(sr, sid, plugin_root=root)
        m = lg.read_marker(sr, sid)
        assert m["plugin_version"] == "9.9.9" and m["stop_hook_timeout"] == 2400

    def test_an_unreadable_plugin_root_leaves_the_stamp_alone(self, sr, tmp_path):
        sid = armed(sr, plugin_version="0.0.1", stop_hook_timeout=10)
        lg.touch_lead(sr, sid, plugin_root=tmp_path / "nowhere")
        m = lg.read_marker(sr, sid)
        assert (m["plugin_version"], m["stop_hook_timeout"]) == ("0.0.1", 10)
        assert lg._read_plugin_version(tmp_path / "nowhere") is None
        assert lg._read_stop_hook_timeout(tmp_path / "nowhere") is None

    def test_a_tombstoned_lead_is_not_armed_but_revives_losslessly(self, sr):
        """tombstone_lead/revive_lead + is_lead: 'until a resume revives it, the gate and the
        wake must stay off'; the revive is 'lossless'."""
        sid = armed(sr, predecessor={"session_id": "old"})
        before = lg.read_marker(sr, sid)
        assert lg.tombstone_lead(sr, sid) is True
        assert lg.tombstone_lead(sr, sid) is False       # already tombstoned → stay quiet
        assert lg.is_lead(sr, sid) is False
        assert lg.revive_lead(sr, sid) is True
        assert lg.revive_lead(sr, sid) is False          # not a tombstone → silent no-op
        assert lg.is_lead(sr, sid) is True
        after = lg.read_marker(sr, sid)
        assert {k: v for k, v in after.items() if k != "last_active"} == \
               {k: v for k, v in before.items() if k != "last_active"}

    def test_tombstone_helpers_are_no_ops_without_a_marker(self, sr):
        assert lg.tombstone_lead(sr, "nobody") is False
        assert lg.revive_lead(sr, "nobody") is False
        assert lg.is_tombstoned(None) is False
        assert lg.is_tombstoned(["not", "a", "dict"]) is False
        assert lg.is_tombstoned({"ended": True}) is True

    def test_tombstone_records_a_supplied_clock(self, sr):
        sid = armed(sr)
        lg.tombstone_lead(sr, sid, now_ts=0)
        assert lg.read_marker(sr, sid)["ended_at"].startswith("19") or \
               lg.read_marker(sr, sid)["ended_at"].startswith("1970")

    def test_clear_lead_removes_the_subtree_and_never_raises(self, sr):
        sid = armed(sr)
        lg.clear_lead(sr, sid)
        assert not lg.lead_dir(sr, sid).exists()
        lg.clear_lead(sr, sid)          # second call: nothing there → must stay silent

    def test_autonomous_state_defaults_safely_for_old_and_broken_markers(self, sr):
        """autonomous_state: 'a lead armed before this feature existed (no key at all) reads as
        (False, "config"), i.e. the safe wait-for-human default.'"""
        assert lg.autonomous_state({}) == (False, "config")
        assert lg.autonomous_state(None) == (False, "config")
        assert lg.autonomous_state("nonsense") == (False, "config")
        assert lg.autonomous_state({"autonomous": True, "autonomous_source": "bogus"}) == \
               (True, "config")
        assert lg.set_autonomous(sr, "nobody", True) is False

    def test_arming_resets_the_posture_config_decides_not_the_old_marker(self, sr):
        """write_marker's comment: the posture is 'written on EVERY arm (never preserved …): the
        posture is opt-in-each-time, so a fresh lead-start resets it'."""
        sid = armed(sr)
        lg.set_autonomous(sr, sid, True)
        assert lg.autonomous_state(lg.read_marker(sr, sid)) == (True, "command")
        armed(sr, sid)                                   # re-arm with the default posture
        assert lg.autonomous_state(lg.read_marker(sr, sid)) == (False, "config")

    def test_grace_expires_and_survives_a_clock_that_went_backwards(self, sr):
        """set_grace/in_grace: 'Stored as an absolute unix ts so the hook just compares against
        time.time()' — an expired window must close, and a backwards clock must not reopen one
        that was never set."""
        sid = armed(sr)
        lg.set_grace(sr, sid, 120, now_ts=1000)
        assert lg.in_grace(sr, sid, now_ts=1100) is True
        assert lg.in_grace(sr, sid, now_ts=1120) is False      # boundary: not < now
        assert lg.in_grace(sr, sid, now_ts=5000) is False
        assert lg.in_grace(sr, sid, now_ts=0) is True          # clock skew widens, never crashes

    def test_a_corrupt_grace_file_closes_the_window(self, sr):
        sid = armed(sr)
        lg.grace_path(sr, sid).write_text("not-a-number")
        assert lg.in_grace(sr, sid) is False
        assert lg.in_grace(sr, "never-armed") is False

    def test_wake_hook_state_surfaces_rather_than_hides(self, sr):
        """wake_hook_state: 'any bad input degrades to "stale" (surface, don't hide)'."""
        assert lg.wake_hook_state({}, 1800) == "unknown"
        assert lg.wake_hook_state({"stop_hook_timeout": None}, 1800) == "stale"
        assert lg.wake_hook_state({"stop_hook_timeout": 600}, 1800) == "stale"
        assert lg.wake_hook_state({"stop_hook_timeout": "nope"}, 1800) == "stale"
        assert lg.wake_hook_state({"stop_hook_timeout": 1800}, 1800) == "ok"
        assert lg.wake_hook_state({"stop_hook_timeout": 3000}, 1800) == "ok"

    def test_pick_lead_color_falls_back_to_the_hash_when_anything_goes_wrong(self, sr, monkeypatch):
        """pick_lead_color: 'Fully defensive: any error → lead_color fallback.'"""
        def boom(*a, **k):
            raise RuntimeError("disk gone")
        monkeypatch.setattr(lg, "read_marker", boom)
        assert lg.pick_lead_color(sr, "sid") == lg.lead_color("sid")

    def test_pick_lead_color_is_rearm_stable_and_collision_free(self, sr):
        """'Re-arm stable: if this lead's marker already claims a CURRENT palette color, returns
        it unchanged' / 'walks TAB_PALETTE forward … to find an unused color'."""
        first = lg.pick_lead_color(sr, "lead-a")
        armed(sr, "lead-a")
        lg.update_marker(sr, "lead-a", color=first)
        assert lg.pick_lead_color(sr, "lead-a") == first
        second = lg.pick_lead_color(sr, "lead-b")
        assert second != first
        lg.update_marker(sr, "lead-a", color=[7, 7, 7])          # a stale, off-palette colour
        assert lg.pick_lead_color(sr, "lead-a") in [list(c) for c in lg.TAB_PALETTE]

    def test_find_terminal_notifier_probes_brew_paths_when_path_lookup_fails(self, monkeypatch):
        """find_terminal_notifier: '`shutil.which` alone gives FALSE negatives in Stop-hook /
        launchd shells whose PATH lacks Homebrew's bin dir, so also probe the standard brew
        locations' — and None when it is genuinely absent."""
        monkeypatch.setattr(lg.shutil, "which", lambda _n: None)
        monkeypatch.setattr(lg.os, "access", lambda p, m: False)
        assert lg.find_terminal_notifier() is None
        monkeypatch.setattr(lg.os, "access",
                            lambda p, m: p == "/opt/homebrew/bin/terminal-notifier")
        assert lg.find_terminal_notifier() == "/opt/homebrew/bin/terminal-notifier"
        monkeypatch.setattr(lg.shutil, "which", lambda _n: "/usr/bin/terminal-notifier")
        assert lg.find_terminal_notifier() == "/usr/bin/terminal-notifier"


# ── the ledger + report surfacing ──────────────────────────────────────────────────────────────
class TestLedgerAndReports:
    def test_append_ledger_writes_one_json_record_per_line(self, sr):
        """'Byte-identical shape to bin/relay's own append_ledger.'"""
        lg.append_ledger(sr, "blocked", session_id="s", lines=99)
        lg.append_ledger(sr, "route", session_id="s")
        recs = [json.loads(ln) for ln in (sr / "sessions.jsonl").read_text().splitlines()]
        assert [r["event"] for r in recs] == ["blocked", "route"]
        assert recs[0]["lines"] == 99 and "ts" in recs[0]

    def test_a_failed_ledger_write_is_swallowed(self, a_file):
        """'Best-effort; a failed ledger write must never turn into a blocked or errored tool
        call.'"""
        lg.append_ledger(a_file, "blocked", session_id="s")     # state_root is a FILE

    def test_executor_reports_skips_closed_superseded_and_unreadable_sessions(self, sr):
        """executor_reports: only sessions with a written report for their CURRENT packet, and
        never a closed/superseded one. 'Never throws.'"""
        make_executor(sr, "live", status="reported")
        make_executor(sr, "shut", status="closed")
        make_executor(sr, "gone", status="superseded")
        make_executor(sr, "working", status="busy", report=False)
        (sr / "broken").mkdir()
        (sr / "broken" / "session.json").write_text("{ truncated")
        got = {sid for sid, _n, _p in lg.executor_reports(sr)}
        assert got == {"live"}
        assert lg.executor_reports(sr / "nowhere") == []
        assert lg.executor_reports(None) == []

    def test_new_reports_for_is_ownership_scoped(self, sr):
        """new_reports_for: 'ONLY reports from executors this lead owns … Another lead's
        executors and UNOWNED ones … never wake this lead.'"""
        armed(sr, "L1")
        make_executor(sr, "mine", owner="L1")
        make_executor(sr, "theirs", owner="L2")
        make_executor(sr, "unowned", owner=None)
        assert [sid for _k, sid, _n, _p in lg.new_reports_for(sr, "L1")] == ["mine"]

    def test_a_closed_executor_never_nags_again_but_a_dead_one_does(self, sr):
        """The 2026-08-21 field bug, quoted in the code: 'closed executors kept waking their lead
        forever. `dead` stays nag-worthy on purpose.'"""
        armed(sr, "L1")
        make_executor(sr, "closed-one", status="closed", owner="L1")
        make_executor(sr, "dead-one", status="dead", owner="L1")
        assert [sid for _k, sid, _n, _p in lg.new_reports_for(sr, "L1")] == ["dead-one"]

    def test_surfacing_a_report_stops_it_waking_the_lead_again(self, sr):
        armed(sr, "L1")
        make_executor(sr, "e1", owner="L1", packet=3)
        keys = [k for k, _s, _n, _p in lg.new_reports_for(sr, "L1")]
        assert keys == ["e1:3"]
        lg.mark_surfaced(sr, "L1", keys)
        assert lg.new_reports_for(sr, "L1") == []
        assert lg.load_surfaced(sr, "L1") == {"e1:3"}

    def test_a_corrupt_surfaced_file_reads_as_an_empty_set(self, sr):
        armed(sr, "L1")
        lg._surfaced_path(sr, "L1").write_text("{ truncated")
        assert lg.load_surfaced(sr, "L1") == set()

    def test_surfacing_never_raises_when_the_state_root_is_unwritable(self, a_file):
        lg.mark_surfaced(a_file, "L1", ["e:1"])                 # must not raise
        assert lg.load_surfaced(a_file, "L1") == set()

    def test_read_session_json_and_owner_status_helpers_degrade_quietly(self, sr):
        make_executor(sr, "e1", owner="L1", status="busy")
        assert lg.read_session_json(sr, "e1")["owner_lead"] == "L1"
        assert lg._executor_owner(sr, "e1") == "L1"
        assert lg._executor_status(sr, "e1") == "busy"
        assert lg.read_session_json(sr, "nope") == {}
        assert lg.read_session_json(None, "e1") == {}
        assert lg._executor_owner(sr, "nope") is None
        assert lg._executor_status(sr, "nope") is None


# ── #22/#23: pending wakes, the retry cap, and relay's own delivery receipt ────────────────────
class TestWakeBookkeeping:
    def test_an_unproven_announce_stays_pending_so_it_retries(self, sr):
        """#22's fix: 'An announce records the keys as PENDING, which does NOT suppress a later
        announce — so an undelivered wake naturally retries on the lead's next Stop.'"""
        armed(sr, "L1")
        make_executor(sr, "e1", owner="L1")
        assert lg.mark_pending(sr, "L1", ["e1:1"]) == []
        assert lg.load_pending(sr, "L1") == {"e1:1": {"announces": 1}}
        assert [s for _k, s, _n, _p in lg.new_reports_for(sr, "L1")] == ["e1"]

    def test_the_retry_cap_stamps_surfaced_after_three_announces(self, sr):
        """WAKE_RETRY_CAP: 'give up (and stamp) after this many unproven announces — a lead whose
        harness never sets stop_hook_active must not be re-announced at forever.'"""
        armed(sr, "L1")
        for i in range(lg.WAKE_RETRY_CAP - 1):
            assert lg.mark_pending(sr, "L1", ["e1:1"]) == []
        assert lg.mark_pending(sr, "L1", ["e1:1"]) == ["e1:1"]
        assert lg.load_pending(sr, "L1") == {}
        assert lg.load_surfaced(sr, "L1") == {"e1:1"}

    def test_proven_delivery_promotes_pending_and_empties_the_file(self, sr):
        armed(sr, "L1")
        lg.mark_pending(sr, "L1", ["e1:1", "e2:2"])
        assert lg.promote_pending(sr, "L1") == ["e1:1", "e2:2"]
        assert lg.load_pending(sr, "L1") == {}
        assert lg.load_surfaced(sr, "L1") == {"e1:1", "e2:2"}
        assert lg.promote_pending(sr, "L1") == []
        assert not lg._pending_path(sr, "L1").exists()

    def test_proof_by_another_channel_drops_the_pending_retry(self, sr):
        """mark_surfaced: 'proven by another channel → stop retrying it'."""
        armed(sr, "L1")
        lg.mark_pending(sr, "L1", ["e1:1"])
        lg.mark_surfaced(sr, "L1", ["e1:1"])
        assert lg.load_pending(sr, "L1") == {}
        lg.drop_pending(sr, "L1", ["e1:1"])          # nothing pending → early return, no crash

    def test_pending_helpers_never_raise_on_a_corrupt_or_unwritable_store(self, sr, a_file):
        armed(sr, "L1")
        lg._pending_path(sr, "L1").write_text("{ truncated")
        assert lg.load_pending(sr, "L1") == {}
        lg.mark_pending(a_file, "L1", ["e:1"])       # unwritable state root → swallowed
        lg.promote_pending(a_file, "L1")

    def test_a_write_failure_while_capping_or_promoting_is_swallowed(self, sr):
        """Both paths write surfaced_reports.json by hand; a failure there must not raise into
        the Stop hook."""
        armed(sr, "L1")
        lg._surfaced_path(sr, "L1").write_text("[]")
        lg._surfaced_path(sr, "L1").chmod(0o444)
        try:
            for _ in range(lg.WAKE_RETRY_CAP):
                lg.mark_pending(sr, "L1", ["e1:1"])
            lg.mark_pending(sr, "L1", ["e2:1"])
            lg.mark_pending(sr, "L1", ["e2:1"])
            assert lg.promote_pending(sr, "L1") == []
        finally:
            lg._surfaced_path(sr, "L1").chmod(0o644)

    def test_no_outstanding_claim_means_somebody_elses_continuation(self, sr):
        """#23: 'No outstanding claim → somebody else's continuation → treat it as an ordinary
        Stop.' The 2026-07-22 incident is exactly this being assumed True."""
        armed(sr, "L1")
        assert lg.relay_announce_delivered(sr, "L1") is False

    def test_a_claim_is_consumed_by_exactly_one_continuation(self, sr):
        """'One-shot: the claim is consumed either way (a claim answers exactly one
        continuation; a retry writes a fresh one).'"""
        armed(sr, "L1")
        lg.record_announce_claim(sr, "L1", "sync")
        assert lg.relay_announce_delivered(sr, "L1") is True
        assert lg.relay_announce_delivered(sr, "L1") is False

    def test_an_unprovable_async_claim_reads_as_not_delivered(self, sr):
        """'An unprovable "async" claim reads as NOT delivered, which merely costs a retry
        (capped) — the safe side of the incident.'"""
        armed(sr, "L1")
        lg.record_announce_claim(sr, "L1", "async")
        assert lg.relay_announce_delivered(sr, "L1") is False

    def test_delivery_is_proven_only_by_the_wake_text_past_the_recorded_offset(self, sr, tmp_path):
        """record_announce_claim: 'Records where the transcript ENDS at this moment, so the later
        delivery check only ever matches text written after this announce.'"""
        armed(sr, "L1")
        t = tmp_path / "transcript.jsonl"
        t.write_text(f'{{"old": "{lg.WAKE_DELIVERY_NEEDLE}"}}\n')   # BEFORE the announce
        lg.record_announce_claim(sr, "L1", "async", transcript_path=t)
        assert lg.relay_announce_delivered(sr, "L1") is False
        lg.record_announce_claim(sr, "L1", "async", transcript_path=t)
        with open(t, "a") as f:
            f.write(f'{{"new": "{lg.WAKE_DELIVERY_NEEDLE}"}}\n')
        assert lg.relay_announce_delivered(sr, "L1") is True

    def test_the_transcript_scan_is_bounded_to_the_tail(self, sr, tmp_path):
        """_transcript_has: 'Reads at most the last _TRANSCRIPT_SCAN_MAX bytes; a shrunken/rotated
        file falls back to scanning that tail.'"""
        t = tmp_path / "big.jsonl"
        with open(t, "w") as f:
            f.write("x" * (lg._TRANSCRIPT_SCAN_MAX + 4096))
            f.write(lg.WAKE_DELIVERY_NEEDLE)
        assert lg._transcript_has(t, 0, lg.WAKE_DELIVERY_NEEDLE) is True
        assert lg._transcript_has(t, 10 ** 12, lg.WAKE_DELIVERY_NEEDLE) is True   # bogus offset
        assert lg._transcript_has(tmp_path / "nope", 0, "x") is False

    def test_claim_helpers_never_raise_on_a_hostile_store(self, sr, a_file):
        armed(sr, "L1")
        lg._announce_claim_path(sr, "L1").write_text("{ truncated")
        assert lg.load_announce_claim(sr, "L1") == {}
        lg._announce_claim_path(sr, "L1").unlink()
        lg._announce_claim_path(sr, "L1").mkdir()
        lg.clear_announce_claim(sr, "L1")                 # unlink on a directory → swallowed
        lg.record_announce_claim(a_file, "L1", "sync")    # unwritable → swallowed
        lg.record_announce_claim(sr, "L1", "sync", transcript_path=sr / "nope")

    def test_handoff_nudge_is_a_one_time_bit(self, sr):
        """handoff_nudged/mark_handoff_nudged: 'a single onetime bit'."""
        armed(sr, "L1")
        assert lg.handoff_nudged(sr, "L1") is False
        lg.mark_handoff_nudged(sr, "L1")
        assert lg.handoff_nudged(sr, "L1") is True
        assert lg.handoff_nudged(None, "L1") is False
        lg.mark_handoff_nudged(Path("/proc/definitely/not/writable"), "L1")

    def test_transcript_mb_is_zero_for_anything_unreadable(self, sr, tmp_path):
        """transcript_mb: '0.0 on any error/missing/None path'."""
        t = tmp_path / "t.jsonl"
        t.write_bytes(b"x" * (2 * 1024 * 1024))
        assert round(lg.transcript_mb(t), 2) == 2.0
        assert lg.transcript_mb(None) == 0.0
        assert lg.transcript_mb(tmp_path / "nope") == 0.0

    def test_head_tracking_round_trips_and_degrades_quietly(self, sr, a_file):
        armed(sr, "L1")
        lg.write_head(sr, "L1", "  abc123\n")
        assert lg.read_head(sr, "L1") == "abc123"
        lg.write_head(sr, "L1", None)
        assert lg.read_head(sr, "L1") == ""
        lg._head_path(sr, "L1").unlink()
        lg._head_path(sr, "L1").mkdir()
        assert lg.read_head(sr, "L1") == ""
        lg.write_head(a_file, "L1", "x")             # unwritable → swallowed

    def test_git_helpers_degrade_to_empty_outside_a_repo(self, tmp_path):
        """git_head: '"" if not a git repo / any error'; new_commits: 'Empty on any error or when
        there's nothing new.'"""
        assert lg.git_head(tmp_path) == ""
        assert lg.new_commits(tmp_path, "") == []
        assert lg.new_commits(tmp_path, "deadbeef") == []
        assert lg.new_commits(tmp_path / "nope", "deadbeef") == []

    def test_has_inflight_executors_is_ownership_scoped_and_counts_stalled(self, sr):
        """has_inflight_executors: '`stalled` counts as in-flight (wake-watch design §6)' and
        'a lead never idles waiting on another lead's executor OR an unowned one'."""
        assert lg.has_inflight_executors(sr / "nowhere") is False
        make_executor(sr, "busy1", status="busy", owner="L1", report=False)
        make_executor(sr, "stall1", status="stalled", owner="L2", report=False)
        (sr / "junk").mkdir()
        (sr / "junk" / "session.json").write_text("{ truncated")
        (sr / "no-session-json").mkdir()          # e.g. the lead/ dir itself
        assert lg.has_inflight_executors(sr) is True
        assert lg.has_inflight_executors(sr, owner_lead="L1") is True
        assert lg.has_inflight_executors(sr, owner_lead="L2") is True
        assert lg.has_inflight_executors(sr, owner_lead="L3") is False
        assert lg.has_inflight_executors(None) is False

    def test_only_busy_and_stalled_count_as_in_flight(self, sr):
        make_executor(sr, "rep", status="reported", owner="L1")
        assert lg.has_inflight_executors(sr, owner_lead="L1") is False


