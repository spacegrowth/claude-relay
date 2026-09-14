"""relay board — pure renderer + the data collector wired through cmd_board (mocked terminal)."""
import importlib.machinery, importlib.util, json, os, sys, time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib")); sys.path.insert(0, str(REPO_ROOT / "scripts"))
import board_render  # noqa: E402


def _ts(seconds_ago=0):
    """A `generated`-shaped timestamp ("%Y-%m-%dT%H:%M:%S", local time, bin/relay's `now()` format)
    `seconds_ago` seconds in the past — for exercising board_render's stale/fresh header logic
    without a real relay module."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds_ago))


def _write_lead(relay, tmp_path, sid="lead-1", **kw):
    kw.setdefault("project", "proj")
    kw.setdefault("cwd", str(tmp_path))
    kw.setdefault("tab_label", "[Lead] proj")
    relay.lead_guard.write_marker(relay.STATE_ROOT, sid, **kw)
    return sid


def _write_exec(relay, tmp_path, sid="e1", owner="lead-1", status="reported"):
    relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
    relay.write_session(sid, {"session_id": sid, "worktree": str(tmp_path), "topic": "t", "scope": "t",
        "tab_label": "x", "model": "sonnet", "mcp": "none", "context": "200k", "agent": "relay-executor",
        "pid": None, "claude_session": None, "status": status, "current_packet": 1, "owner_lead": owner,
        "busy_since": relay.now(), "created": relay.now(), "updated": relay.now()})
    (relay.packets_dir(sid) / "001-packet.md").write_text("# do the thing\n")
    if status == "reported":
        (relay.packets_dir(sid) / "001-report.md").write_text(
            "Done.\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\nChanged: x\n")
    return sid


def load_relay_module(state_root):
    path = str(REPO_ROOT / "bin" / "relay")
    loader = importlib.machinery.SourceFileLoader("relay_cli", path)
    spec = importlib.util.spec_from_file_location("relay_cli", path, loader=loader)
    mod = importlib.util.module_from_spec(spec); sys.modules["relay_cli"] = mod; loader.exec_module(mod)
    mod.STATE_ROOT = state_root; mod.LEDGER = state_root / "sessions.jsonl"
    mod._probe_model = lambda alias: (None, "disabled in tests"); mod._cli_version = lambda: "test"
    # read_pid/read_iterm_id/read_iterm_id_at poll a file for up to 5s by default — test-side only,
    # shrink the DEFAULT to 0.5s (an explicit timeout from any caller is untouched); see
    # tests/test_relay.py::load_relay_module for the full rationale.
    _orig_read_pid, _orig_read_iterm_id, _orig_read_iterm_id_at = (
        mod.read_pid, mod.read_iterm_id, mod.read_iterm_id_at)
    mod.read_pid = lambda session_id, timeout=0.5: _orig_read_pid(session_id, timeout)
    mod.read_iterm_id = lambda session_id, timeout=0.5: _orig_read_iterm_id(session_id, timeout)
    mod.read_iterm_id_at = lambda path, timeout=0.5: _orig_read_iterm_id_at(path, timeout)
    return mod


@pytest.fixture
def relay(tmp_path):
    return load_relay_module(tmp_path / ".relay-tasks")


class TestRenderer:
    def test_empty_and_escaping(self):
        html = board_render.render({"leads": [], "executors": []})
        assert "relay board" in html and "localStorage" in html and "data-theme" in html
        assert 'class="app"' in html and 'class="side"' in html    # two-pane master/detail
        assert "no executor sessions yet" in html
        html = board_render.render({"leads": [{"session_id": "L<1>", "project": "<b>x</b>"}],
                                    "executors": [{"session_id": "e&1", "owner_lead": "L<1>", "status": "busy", "topic": "<t>"}]})
        assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html and "e&amp;1" in html

    def test_light_default_lead_colour_as_dot(self):
        html = board_render.render({"leads": [{"session_id": "L", "project": "p", "color": [1, 2, 3]}], "executors": []})
        assert "prefers-color-scheme" not in html                  # light by default, toggle decides
        assert 'class="cdot"' in html and "rgb(1,2,3)" in html      # tab colour is a dot only

    def test_reported_pinned_and_detail_panel(self):
        ex = {"session_id": "e1", "owner_lead": "L", "status": "reported", "topic": "parser", "model": "sonnet[1m]",
              "launch": "none/1m/A", "context": "1m", "agent": "relay-executor", "mcp": "none", "tokens": "1.2M/34k",
              "mb": "3.1", "pkt": "002", "reported": True, "heavy": True, "keep": True, "queued": 1, "worktree": "/w",
              "packets": [{"n": "001", "gist": "first", "report_path": "/p/r1",
                           "report_body": {"text": "FULL REPORT BODY HERE", "truncated": False, "path": "/p/r1"},
                           "diff_path": "/p/001-diff.html",
                           "tldr": {"outcome": "Did it.", "status": "clean", "risk": "weakened a test", "unverified": "the retry path"}},
                          {"n": "002", "gist": "second", "current": True}]}
        html = board_render.render({"leads": [{"session_id": "L", "project": "proj", "wake": "ok"}],
                                    "executors": [ex], "relay_bin": "/x/relay"})
        # rail: reported executor pinned under a "Needs review" group and selectable
        assert "Needs review" in html and 'data-target="ex-e1"' in html
        # detail panel: chips + dot-strip + full task/outcome + clean TL;DR grid + INLINE report (no file:// link)
        for needle in ('id="ex-e1"', "sonnet[1m]", "1.2M/34k", "pinned", "Did it.", 'class="pdot',
                       '<dl class="tldr">', "weakened a test", "the retry path",
                       "FULL REPORT BODY HERE", "Verify report", "Send follow-up", "in flight"):
            assert needle in html, needle
        assert "file://" not in html and "open report ↗" not in html   # inline, not a new path
        # closed executor: no destructive command, shown under a Closed group
        closed = dict(ex, status="closed", auto_closed="landed", rendered_status="closed (auto)")
        html2 = board_render.render({"leads": [{"session_id": "L"}], "executors": [closed], "relay_bin": "/x/relay"})
        assert ">Closed<" in html2 and "auto: landed" in html2
        assert "/x/relay close e1" not in html2 and "/x/relay resume e1" in html2

    def test_updated_badge_and_meta_refresh_only_when_live(self):
        data = {"leads": [], "executors": [], "generated": _ts(0)}
        html = board_render.render(data, live=True, refresh_seconds=7)
        assert 'http-equiv="refresh" content="7"' in html
        assert 'id="board-updated"' in html and 'class="upd"' in html and 'class="upd stale"' not in html
        html_snapshot = board_render.render(data)  # live=False (default) — the one-shot snapshot mode
        assert 'http-equiv="refresh"' not in html_snapshot and 'id="board-updated"' not in html_snapshot

    def test_updated_badge_turns_red_once_past_3x_refresh_seconds(self):
        # refresh_seconds=10 → stale threshold is 30s old; 35s old must read red, 20s old must not.
        stale_html = board_render.render({"leads": [], "executors": [], "generated": _ts(35)},
                                         live=True, refresh_seconds=10)
        assert 'class="upd stale"' in stale_html
        fresh_html = board_render.render({"leads": [], "executors": [], "generated": _ts(20)},
                                         live=True, refresh_seconds=10)
        assert 'class="upd stale"' not in fresh_html and 'class="upd"' in fresh_html

    def test_ended_but_alive_lead_renders_red_on_the_rail(self):
        html = board_render.render({"leads": [{"session_id": "L", "project": "webapp", "liveness": "ended_but_alive"}],
                                    "executors": []})
        assert 'class="gl lv-bad"' in html

    def test_lead_heavy_chip_shows_the_line_and_window_reading(self):
        # The board's lead chip (task: split the heaviness threshold by role) — reads the SAME
        # `heavy`/`heavy_reading` fields board_data computes via lead_nudge_threshold, never the
        # executors-only context_nudge_tokens.
        html = board_render.render({"leads": [{"session_id": "L", "project": "webapp", "heavy": True,
                                               "heavy_reading": "312k live, line 300k on a 1M window"}],
                                    "executors": []})
        assert 'class="wake heavy"' in html
        assert "312k live, line 300k on a 1M window" in html  # title attribute, not inlined text

    def test_non_heavy_lead_has_no_chip(self):
        html = board_render.render({"leads": [{"session_id": "L", "project": "webapp", "heavy": False}],
                                    "executors": []})
        assert 'class="wake heavy"' not in html


class TestLeadHeavyOnBoard:
    """board_data's lead chip (task: split the heaviness threshold by role) reads
    lead_nudge_threshold — the LEAD's own line, window-capped — never the executors-only
    context_nudge_tokens the executor chips use."""

    def test_1m_window_lead_not_heavy_below_300k_line(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path),
                                       model="claude-sonnet-5[1m]")
        monkeypatch.setattr(relay, "_lead_usage_for", lambda m: {"last_prompt": 200000})
        data = relay.board_data()
        assert data["leads"][0]["heavy"] is False

    def test_1m_window_lead_heavy_past_300k_line(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path),
                                       model="claude-sonnet-5[1m]")
        monkeypatch.setattr(relay, "_lead_usage_for", lambda m: {"last_prompt": 310000})
        data = relay.board_data()
        assert data["leads"][0]["heavy"] is True
        assert data["leads"][0]["heavy_reading"] == "310k live, line 300k on a 1M window"
        assert board_render.render(data).count('class="wake heavy"') == 1

    def test_unknown_window_lead_not_heavy_at_160k(self, relay, tmp_path, monkeypatch):
        # A bare model (no [1m] suffix, no probed tier entry) is a genuinely UNKNOWN window, never
        # guessed down to 200_000 (the bug: it used to cap this lead's line to 150k, so 160k read
        # as heavy when it should still be silent on the uncapped 300k line).
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path),
                                       model="claude-sonnet-5")  # no [1m] suffix
        monkeypatch.setattr(relay, "_lead_usage_for", lambda m: {"last_prompt": 160000})
        data = relay.board_data()
        assert data["leads"][0]["heavy"] is False

    def test_unknown_window_lead_heavy_at_310k(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp", cwd=str(tmp_path),
                                       model="claude-sonnet-5")  # no [1m] suffix
        monkeypatch.setattr(relay, "_lead_usage_for", lambda m: {"last_prompt": 310000})
        data = relay.board_data()
        assert data["leads"][0]["heavy"] is True
        assert data["leads"][0]["heavy_reading"] == "310k live, line 300k on a ? window"


class TestExecutorApproachingOnBoard:
    """Backlog row 87: `board_data`'s `approaching`/`approaching_reading` fields — the board's own
    amber sibling of `relay list`'s yellow footnote, computed alongside the existing executor
    `heavy` field (past `context_warn_tokens`, below `context_nudge_tokens`; never set once
    `heavy` already is)."""

    def test_approaching_at_125k(self, relay, tmp_path, monkeypatch):
        _write_lead(relay, tmp_path)
        _write_exec(relay, tmp_path, sid="e1")
        monkeypatch.setattr(relay, "_usage_for_session", lambda r: {"last_prompt": 125000})
        data = relay.board_data()
        ex = data["executors"][0]
        assert ex["approaching"] is True
        assert ex["heavy"] is False
        assert "125k" in ex["approaching_reading"]
        html = board_render.render(data)
        assert "approaching heavy" in html and "125k" in html
        assert 'class="chip approach"' in html

    def test_not_approaching_below_the_warn_line(self, relay, tmp_path, monkeypatch):
        _write_lead(relay, tmp_path)
        _write_exec(relay, tmp_path, sid="e1")
        monkeypatch.setattr(relay, "_usage_for_session", lambda r: {"last_prompt": 119000})
        data = relay.board_data()
        assert data["executors"][0]["approaching"] is False
        assert 'class="chip approach"' not in board_render.render(data)

    def test_past_the_rotate_line_is_heavy_not_approaching(self, relay, tmp_path, monkeypatch):
        _write_lead(relay, tmp_path)
        _write_exec(relay, tmp_path, sid="e1")
        monkeypatch.setattr(relay, "_usage_for_session", lambda r: {"last_prompt": 155000})
        data = relay.board_data()
        ex = data["executors"][0]
        assert ex["heavy"] is True
        assert ex["approaching"] is False
        html = board_render.render(data)
        assert 'class="chip flag">heavy</span>' in html
        assert 'class="chip approach"' not in html


class TestLiveBoard:
    """`relay board --live` (and the `board_live` config default): board.html gains a meta-refresh
    + "updated" badge and a sibling board.json is written, then every state-changing command keeps
    both rewritten in place with no server process (bin/relay's refresh_live_board)."""

    def test_live_writes_json_sidecar_and_meta_refresh(self, relay, tmp_path):
        _write_lead(relay, tmp_path)
        _write_exec(relay, tmp_path)
        with mock.patch.object(relay, "_lead_liveness", return_value="live"), \
             mock.patch.object(relay, "session_pid_alive", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_board(SimpleNamespace(json=False, out=None, open=False, lead=None, live=True))
        html_path, json_path = relay.STATE_ROOT / "board.html", relay.STATE_ROOT / "board.json"
        assert html_path.exists() and json_path.exists()
        html = html_path.read_text()
        assert 'http-equiv="refresh" content="10"' in html and 'id="board-updated"' in html
        data = json.loads(json_path.read_text())
        assert data["executors"][0]["session_id"] == "e1"

    def test_plain_board_writes_no_sidecar_or_refresh(self, relay, tmp_path):
        """Back-compat: an ordinary `relay board` (no --live, no config flag) stays the one-shot
        snapshot it always was — no sidecar, no meta-refresh, `args` need not even carry `live`."""
        _write_lead(relay, tmp_path)
        with mock.patch.object(relay, "_lead_liveness", return_value="live"):
            relay.cmd_board(SimpleNamespace(json=False, out=None, open=False, lead=None))
        html_path = relay.STATE_ROOT / "board.html"
        assert html_path.exists() and "http-equiv=\"refresh\"" not in html_path.read_text()
        assert not (relay.STATE_ROOT / "board.json").exists()

    def test_config_board_live_true_makes_plain_board_live(self, relay, tmp_path):
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "lead").mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "lead" / "config.json").write_text(json.dumps({"board_live": True}))
        _write_lead(relay, tmp_path)
        with mock.patch.object(relay, "_lead_liveness", return_value="live"):
            relay.cmd_board(SimpleNamespace(json=False, out=None, open=False, lead=None))
        assert (relay.STATE_ROOT / "board.json").exists()

    def test_is_board_live_active_via_config_or_prior_live_write(self, relay):
        assert relay._is_board_live_active({"board_live": False}) is False
        assert relay._is_board_live_active({"board_live": True}) is True
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "board.json").write_text("{}")
        assert relay._is_board_live_active({"board_live": False}) is True  # prior --live proves it

    def test_refresh_live_board_noop_when_not_live(self, relay):
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        relay.refresh_live_board()
        assert not (relay.STATE_ROOT / "board.html").exists()
        assert not (relay.STATE_ROOT / "board.json").exists()

    def test_refresh_live_board_never_sweeps(self, relay):
        """The one behavior the packet calls out explicitly: the refresh path must NEVER be a
        second, differently-scoped auto-close sweep — it always calls board_data(sweep=False)."""
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "board.json").write_text("{}")  # proves live is active
        with mock.patch.object(relay, "board_data", wraps=relay.board_data) as bd:
            relay.refresh_live_board()
        bd.assert_called_once_with(sweep=False)

    def test_board_data_sweep_false_never_calls_auto_close_sweep(self, relay, tmp_path, monkeypatch):
        _write_lead(relay, tmp_path, sid="lead-1")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-1")
        with mock.patch.object(relay, "auto_close_sweep") as sweep_mock, \
             mock.patch.object(relay, "_lead_liveness", return_value="live"):
            relay.board_data(sweep=False)
            sweep_mock.assert_not_called()
            relay.board_data(sweep=True)
            sweep_mock.assert_called_once()

    def test_check_rewrites_live_board_without_board_being_rerun(self, relay, tmp_path):
        """Acceptance: `relay board --live` writes board.html+json, and a SUBSEQUENT `relay check
        <sid>` rewrites both again (new content on disk) without `relay board` running a second
        time."""
        _write_lead(relay, tmp_path)
        _write_exec(relay, tmp_path, sid="e1", status="busy")
        with mock.patch.object(relay, "_lead_liveness", return_value="live"), \
             mock.patch.object(relay, "session_pid_alive", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_board(SimpleNamespace(json=False, out=None, open=False, lead=None, live=True))
            json_path = relay.STATE_ROOT / "board.json"
            before = json.loads(json_path.read_text())
            assert before["executors"][0]["status"] == "busy"
            (relay.packets_dir("e1") / "001-report.md").write_text(
                "Done.\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\nChanged: x\n")
            relay.cmd_check(SimpleNamespace(session_id="e1", all=False, json=True))
        after = json.loads(json_path.read_text())
        assert after["executors"][0]["status"] == "reported"

    def test_list_and_spawn_also_trigger_the_refresh(self, relay, tmp_path):
        """The other state-change moments the packet names: `relay list` and `relay spawn`."""
        _write_lead(relay, tmp_path)
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "board.json").write_text("{}")  # proves live is already active
        with mock.patch.object(relay, "refresh_live_board") as rlb:
            relay.cmd_list(SimpleNamespace(json=False, lead=None, all=True, closed=False))
        rlb.assert_called_once()

        with mock.patch.object(relay, "refresh_live_board") as rlb, \
             mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: {}), \
             mock.patch.object(relay, "auto_trust"), \
             mock.patch.object(relay, "read_pid", return_value=123):
            packet = tmp_path / "p.md"; packet.write_text("do a thing")
            relay.cmd_spawn(SimpleNamespace(worktree=str(tmp_path), topic="t", packet=str(packet),
                model=None, model_override=None, mcp=None, effort=None, keep=False, name="e2",
                scope=None, seed=None, lead=None, skip_perms=None, pane=None))
        rlb.assert_called_once()


    def test_refresh_live_board_atomic_write_leaves_no_tmp_and_full_content(self, relay, tmp_path):
        """`refresh_live_board` and `cmd_board --live` must write board.html/board.json via a
        sibling tmp file + os.replace (bin/relay's `_atomic_write_text`, same shape as
        `write_session`/`lead_guard._atomic_write_json`) — never a plain `write_text` a
        mid-reload browser could catch half-written. Proxy for that: no `.tmp` sibling survives
        the write, and both files hold complete, parseable content afterwards."""
        _write_lead(relay, tmp_path)
        _write_exec(relay, tmp_path)
        with mock.patch.object(relay, "_lead_liveness", return_value="live"), \
             mock.patch.object(relay, "session_pid_alive", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_board(SimpleNamespace(json=False, out=None, open=False, lead=None, live=True))
        html_path, json_path = relay.STATE_ROOT / "board.html", relay.STATE_ROOT / "board.json"
        tmps = list(relay.STATE_ROOT.glob("*.tmp"))
        assert tmps == [], f"leftover tmp files: {tmps}"
        assert "</html>" in html_path.read_text()          # a truncated write couldn't close the tag
        data = json.loads(json_path.read_text())            # a half-written file wouldn't even parse
        assert data["executors"][0]["session_id"] == "e1"
        # a second rewrite (refresh_live_board, the list/check/send/spawn path) must also stay clean
        (relay.packets_dir("e1") / "001-report.md").write_text(
            "Done.\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\nChanged: x\n")
        with mock.patch.object(relay, "_lead_liveness", return_value="live"):
            relay.refresh_live_board()
        assert list(relay.STATE_ROOT.glob("*.tmp")) == []
        assert "</html>" in html_path.read_text()
        assert json.loads(json_path.read_text())["executors"][0]["status"] == "reported"


class TestLiveOff:
    """`relay board --live off` — the only way to turn live mode back off once the board.json
    sidecar exists (see `_is_board_live_active`'s docstring: its mere presence re-arms the rewrite
    forever)."""

    def test_live_off_removes_sidecar_and_prints_one_line(self, relay, capsys):
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "board.json").write_text("{}")   # proves live was active
        relay.cmd_board(SimpleNamespace(json=False, out=None, open=False, lead=None, live="off"))
        assert not (relay.STATE_ROOT / "board.json").exists()
        out_lines = [l for l in capsys.readouterr().out.splitlines() if l]
        assert len(out_lines) == 1 and "off" in out_lines[0]

    def test_live_off_with_config_board_live_true_says_config_still_holds_it_on(self, relay, capsys):
        relay.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "lead").mkdir(parents=True, exist_ok=True)
        (relay.STATE_ROOT / "lead" / "config.json").write_text(json.dumps({"board_live": True}))
        (relay.STATE_ROOT / "board.json").write_text("{}")
        relay.cmd_board(SimpleNamespace(json=False, out=None, open=False, lead=None, live="off"))
        out_lines = [l for l in capsys.readouterr().out.splitlines() if l]
        assert len(out_lines) == 1
        assert "config" in out_lines[0] and "board_live" in out_lines[0]


class TestEndedButAliveOnBoard:
    """§Also bullet: `relay board` used to call `_lead_liveness` directly, so a tombstoned lead
    whose tab is still alive rendered green "live" on the board while `relay list` already showed
    red "ended?" for the same marker. board_data must use the same verdict as `relay list`."""

    def test_board_data_matches_lists_ended_but_alive_verdict(self, relay, tmp_path):
        _write_lead(relay, tmp_path, sid="lead-1")
        relay.lead_guard.tombstone_lead(relay.STATE_ROOT, "lead-1")
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            data = relay.board_data(sweep=False)
        assert data["leads"][0]["liveness"] == "ended_but_alive"
        assert board_render.render(data).count('class="gl lv-bad"') >= 1


class TestBrokenExecutorOnBoard:
    """Backlog row 78: `relay list` already surfaces a present-but-unreadable executor
    session.json as a red `broken` row instead of silently dropping it (D2's executor-side
    sibling); the board used to do `if "error" in s: continue` and just lose the session. Now
    board_data hands it over as the SAME `{"session_id", "broken": true}` shape `list --json`
    uses, and board_render draws it with the same red treatment bad lead liveness already gets."""

    def test_board_data_includes_broken_session_and_normal_row_is_unaffected(self, relay, tmp_path):
        _write_lead(relay, tmp_path, sid="lead-1")
        _write_exec(relay, tmp_path, sid="e1", owner="lead-1", status="reported")
        relay.session_dir("broke1").mkdir(parents=True, exist_ok=True)
        (relay.session_dir("broke1") / "session.json").write_text("{not json")
        with mock.patch.object(relay, "_lead_liveness", return_value="live"), \
             mock.patch.object(relay, "session_pid_alive", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            data = relay.board_data(sweep=False)  # must not raise on the unreadable session.json
        execs = {e["session_id"]: e for e in data["executors"]}
        assert execs["e1"]["status"] == "reported" and execs["e1"]["reported"] is True  # regression: normal row unaffected
        assert execs["broke1"] == {"session_id": "broke1", "broken": True}
        assert any("broke1" in w["text"] and w["level"] == "bad" for w in data["warnings"])

    def test_broken_session_excluded_from_sweep_and_survives_owner_lead_filter(self, relay, tmp_path, monkeypatch):
        _write_lead(relay, tmp_path, sid="lead-1")
        _write_exec(relay, tmp_path, sid="e1", owner="lead-1", status="busy")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "lead-1")
        relay.session_dir("broke1").mkdir(parents=True, exist_ok=True)
        (relay.session_dir("broke1") / "session.json").write_text("nope, not json")
        with mock.patch.object(relay, "_lead_liveness", return_value="live"), \
             mock.patch.object(relay, "session_pid_alive", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True), \
             mock.patch.object(relay, "auto_close_sweep", return_value=[]) as sweep_mock:
            data = relay.board_data(lead_sid="some-other-lead")  # scoped to a lead that owns neither row
        sids = [e.get("session_id") for e in data["executors"]]
        assert "e1" not in sids                                  # a real row owned by a DIFFERENT lead IS filtered
        assert "broke1" in sids                                  # but "no owner known" must never be filtered out
        _, kwargs = sweep_mock.call_args
        assert kwargs["sids"] == ["e1"] and "broke1" not in kwargs["sids"]  # broken never handed to the auto-close sweep

    def test_render_shows_broken_label_session_id_and_bad_class(self):
        html = board_render.render({"leads": [], "executors": [{"session_id": "broke1", "broken": True}]})
        assert "broke1" in html and ">broken<" in html
        assert 'class="gl lv-bad"' in html and 'class="nm lv-bad"' in html   # same red treatment as bad lead liveness
        assert "session.json unreadable" in html

    def test_render_broken_row_does_not_crash_normal_rows_still_render(self):
        ex = {"session_id": "e1", "owner_lead": "L", "status": "busy", "topic": "t"}
        html = board_render.render({"leads": [{"session_id": "L", "project": "proj"}],
                                    "executors": [ex, {"session_id": "broke1", "broken": True}]})
        assert 'data-target="ex-e1"' in html and 'data-target="ex-broke1"' in html


class TestCmdBoard:
    def _seed(self, relay, tmp_path):
        relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="proj", cwd=str(tmp_path), tab_label="[Lead] proj")
        relay.packets_dir("e1").mkdir(parents=True, exist_ok=True)
        relay.write_session("e1", {"session_id": "e1", "worktree": str(tmp_path), "topic": "t", "scope": "t", "tab_label": "x",
            "model": "sonnet", "mcp": "none", "context": "200k", "agent": "relay-executor", "pid": None, "claude_session": None,
            "status": "reported", "current_packet": 1, "owner_lead": "lead-1", "busy_since": relay.now(),
            "created": relay.now(), "updated": relay.now()})
        (relay.packets_dir("e1") / "001-packet.md").write_text("# do the thing\n")
        (relay.packets_dir("e1") / "001-report.md").write_text("Done the thing.\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\nChanged: x\n")
        relay.packets_dir("orph").mkdir(parents=True, exist_ok=True)
        relay.write_session("orph", {"session_id": "orph", "worktree": str(tmp_path), "topic": "o", "scope": "o", "tab_label": "y",
            "model": "haiku", "pid": None, "claude_session": None, "status": "busy", "current_packet": 1, "owner_lead": "gone-lead",
            "busy_since": relay.now(), "busy_since_epoch": __import__("time").time(), "created": relay.now(), "updated": relay.now()})
        (relay.packets_dir("orph") / "001-packet.md").write_text("o")

    def test_json_and_html(self, relay, tmp_path, capsys):
        self._seed(relay, tmp_path)
        with mock.patch.object(relay, "_lead_liveness", return_value="live"), \
             mock.patch.object(relay, "session_pid_alive", return_value=True), \
             mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.cmd_board(SimpleNamespace(json=True, out=None, open=False, lead=None))
            d = json.loads(capsys.readouterr().out)
            assert [m["session_id"] for m in d["leads"]] == ["lead-1"] and d["leads"][0]["liveness"] == "live"
            ex = {e["session_id"]: e for e in d["executors"]}
            assert ex["e1"]["launch"] == "none/200k/A/?" and ex["e1"]["packets"][0]["tldr"]["outcome"] == "Done the thing."
            assert ex["e1"]["packets"][0]["report_body"]["text"].startswith("Done the thing.")
            assert ex["orph"]["orphan"] is True and any("no longer armed" in w["text"] for w in d["warnings"])
            out = tmp_path / "b.html"
            relay.cmd_board(SimpleNamespace(json=False, out=str(out), open=False, lead=None))
        html = out.read_text()
        assert "proj" in html and "Done the thing." in html and "orphan" in html and "Unowned / orphaned" in html
        assert "relay-board-theme" in html
