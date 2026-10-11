"""`relay list --json` names each tmux executor's own tmux session (`tmux_session`), so Cue can
`tmux attach -t <name>` to exactly that executor — the companion of tmux_backend's own-session
placement (tests/test_backends.py covers the spawn/close argv; tests/test_e2e_tmux.py a real
private server).

Every backend here is a stub — term_backend and relay.iterm are pinned, and the tmux module's one
lookup is replaced — so no test reaches a real terminal or tmux server."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_relay import load_relay_module  # noqa: E402


@pytest.fixture
def relay(tmp_path, monkeypatch):
    mod = load_relay_module(tmp_path / ".relay-tasks")
    stub = SimpleNamespace(NAME="tmux", is_alive=lambda label, handle=None, pid=None: True,
                           exists_by_id=lambda handle: True)
    monkeypatch.setattr(mod, "iterm", stub)
    monkeypatch.setattr(mod, "term_backend", lambda s: stub)
    monkeypatch.setattr(mod, "pid_alive", lambda pid: True)
    tmux = mod.backend.by_name("tmux")
    asked = []

    def name_of(handle):
        asked.append(handle)
        return {"tmux:%7": "relay-e1", "tmux:%11": "lead-sess"}.get(handle)
    monkeypatch.setattr(tmux, "session_name_by_id", name_of)
    monkeypatch.setattr(tmux, "_tmux", lambda *a, **k: pytest.fail("real tmux reached"))
    mod._asked = asked
    return mod


def _exec(relay, sid, handle, backend="tmux"):
    relay.write_session(sid, {"session_id": sid, "current_packet": 1, "status": "busy",
                              "topic": "t", "worktree": "/w", "scope": "", "model": "opus",
                              "busy_since": relay.now(), "updated": relay.now(),
                              "owner_lead": None, "owner_project": None, "backend": backend,
                              "iterm_session": handle, "pid": 4242, "tab_label": f"[Exec] {sid}"})


def _rows(relay, capsys):
    relay.cmd_list(SimpleNamespace(json=True, lead=None, all=True, closed=False))
    return {e["session_id"]: e for e in json.loads(capsys.readouterr().out)["executors"]}


def test_list_json_carries_each_tmux_executors_session_name(relay, capsys):
    _exec(relay, "e1", "tmux:%7")            # own detached session
    _exec(relay, "e2", "tmux:%11")           # split pane: lives in the lead's session
    _exec(relay, "gone", "tmux:%99")         # pane no longer resolves
    rows = _rows(relay, capsys)
    assert rows["e1"]["tmux_session"] == "relay-e1"
    assert rows["e2"]["tmux_session"] == "lead-sess"
    assert rows["gone"]["tmux_session"] is None
    # existing keys unchanged: the handle is still where it was
    assert rows["e1"]["iterm_session"] == "tmux:%7"


def test_list_json_non_tmux_executor_has_no_tmux_session_key(relay, capsys):
    _exec(relay, "it", "w0t0p0:11111111-2222-3333-4444-555555555555", backend="iterm")
    _exec(relay, "ta", "twid:4", backend="terminal")
    rows = _rows(relay, capsys)
    assert "tmux_session" not in rows["it"] and "tmux_session" not in rows["ta"]
    assert relay._asked == []                # no tmux lookup for a non-tmux handle
