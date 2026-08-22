"""relay board — pure renderer + the data collector wired through cmd_board (mocked terminal)."""
import importlib.machinery, importlib.util, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib")); sys.path.insert(0, str(REPO_ROOT / "scripts"))
import board_render  # noqa: E402


def load_relay_module(state_root):
    path = str(REPO_ROOT / "bin" / "relay")
    loader = importlib.machinery.SourceFileLoader("relay_cli", path)
    spec = importlib.util.spec_from_file_location("relay_cli", path, loader=loader)
    mod = importlib.util.module_from_spec(spec); sys.modules["relay_cli"] = mod; loader.exec_module(mod)
    mod.STATE_ROOT = state_root; mod.LEDGER = state_root / "sessions.jsonl"
    mod._probe_model = lambda alias: (None, "disabled in tests"); mod._cli_version = lambda: "test"
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
            assert ex["e1"]["launch"] == "none/200k/A" and ex["e1"]["packets"][0]["tldr"]["outcome"] == "Done the thing."
            assert ex["e1"]["packets"][0]["report_body"]["text"].startswith("Done the thing.")
            assert ex["orph"]["orphan"] is True and any("no longer armed" in w["text"] for w in d["warnings"])
            out = tmp_path / "b.html"
            relay.cmd_board(SimpleNamespace(json=False, out=str(out), open=False, lead=None))
        html = out.read_text()
        assert "proj" in html and "Done the thing." in html and "orphan" in html and "Unowned / orphaned" in html
        assert "relay-board-theme" in html
