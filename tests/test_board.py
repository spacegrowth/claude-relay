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
    return mod


@pytest.fixture
def relay(tmp_path):
    return load_relay_module(tmp_path / ".relay-tasks")


class TestRenderer:
    def test_empty_and_escaping(self):
        html = board_render.render({"leads": [], "executors": []})
        assert "relay board" in html and "localStorage" in html and "data-theme" in html
        html = board_render.render({"leads": [{"session_id": "L<1>", "project": "<b>x</b>"}],
                                    "executors": [{"session_id": "e&1", "owner_lead": "L<1>", "status": "busy", "topic": "<t>"}]})
        assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html and "e&amp;1" in html

    def test_light_default_no_left_accent(self):
        html = board_render.render({"leads": [{"session_id": "L", "project": "p", "color": [1, 2, 3]}], "executors": []})
        assert "border-left-color" not in html          # no accent stripe
        assert 'class="dot"' in html and "rgb(1,2,3)" in html   # tab colour only as a dot
        assert "prefers-color-scheme" not in html      # light by default, toggle decides

    def test_executor_rows_and_timeline(self):
        ex = {"session_id": "e1", "owner_lead": "L", "status": "reported", "topic": "parser", "model": "sonnet[1m]",
              "launch": "none/1m/A", "tokens": "1.2M/34k", "mb": "3.1", "pkt": "002", "reported": True, "heavy": True,
              "keep": True, "queued": 1, "worktree": "/w",
              "packets": [{"n": "001", "gist": "first", "packet_path": "/p/1", "packet_url": "file:///p/1",
                           "report_path": "/p/r1", "report_url": "file:///p/r1", "diff_path": "/p/d1", "diff_url": "file:///p/d1",
                           "tldr": {"outcome": "Did it.", "status": "clean", "risk": "none", "unverified": "none"}},
                          {"n": "002", "gist": "second", "packet_path": "/p/2", "packet_url": "file:///p/2", "current": True}]}
        html = board_render.render({"leads": [{"session_id": "L", "project": "proj"}], "executors": [ex], "relay_bin": "/x/relay"})
        for needle in ("parser", "sonnet[1m]", "none/1m/A", "1.2M/34k", "heavy", "pinned", "1 queued", "Did it.",
                       "Status: clean", 'href="file:///p/d1"', "/x/relay send e1", "/x/relay verify e1", "in flight"):
            assert needle in html, needle
        closed = dict(ex, status="closed", auto_closed="landed", rendered_status="closed (auto)")
        html = board_render.render({"leads": [{"session_id": "L"}], "executors": [closed]})
        assert "closed-row" in html and "auto: landed" in html and "/x/relay" not in html


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
            assert ex["e1"]["packets"][0]["report_url"].startswith("file://")
            assert ex["orph"]["orphan"] is True and any("no longer armed" in w["text"] for w in d["warnings"])
            out = tmp_path / "b.html"
            relay.cmd_board(SimpleNamespace(json=False, out=str(out), open=False, lead=None))
        html = out.read_text()
        assert "proj" in html and "Done the thing." in html and "orphan" in html and "Unowned / orphaned" in html
        assert "relay-board-theme" in html
