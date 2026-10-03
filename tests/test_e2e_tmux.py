"""
Real-tmux end-to-end test for the tmux backend (scripts/tmux_backend.py), against the stub `claude`
(tests/fake_claude). Unlike test_e2e_terminal.py / test_e2e_iterm.py it runs under pytest: the ONE
real thing it drives is a PRIVATE tmux server named `relay-test-<pid>` (RELAY_TMUX_SOCKET), started
detached and killed in the fixture finalizer. It never touches the human's own tmux server, iTerm,
or Terminal.app. Skipped when `tmux` is not on PATH.

NOT asserted here: `tmux_backend._lead_alive` / `pids_on_tty` against the stub. The stub's process
name is `python` (it is a `#!/usr/bin/env python3` script), not `claude`, so the ps-by-tty match
those use would not find it — that is a property of the stub, not of the backend (review finding 5).
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "lib"))
import tmux_backend  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


def _poll(fn, timeout=10, interval=0.2):
    deadline = time.time() + timeout
    result = fn()
    while not result and time.time() < deadline:
        time.sleep(interval)
        result = fn()
    return result


@pytest.fixture
def tmux_server(monkeypatch):
    """A private detached tmux server. conftest's autouse fixture has already stripped the ambient
    TMUX*/RELAY_TMUX_SOCKET, so setting the socket here (after it) is what scopes every
    tmux_backend call to this server."""
    name = f"relay-test-{os.getpid()}"
    monkeypatch.setenv("RELAY_TMUX_SOCKET", name)
    try:
        subprocess.run(["tmux", "-L", name, "new-session", "-d", "-s", "relay-e2e",
                        "-x", "200", "-y", "50"], check=True, capture_output=True, timeout=10)
        yield name
    finally:
        subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True, timeout=10)


def _capture(pane):
    r = tmux_backend._tmux(["capture-pane", "-p", "-t", pane])
    return r.stdout or ""


def _opt(pane, scope_flag, option):
    r = tmux_backend._tmux(["show-options", scope_flag, "-v", "-t", pane, option])
    return (r.stdout or "").strip()


def test_tmux_backend_against_real_tmux(tmux_server, tmp_path, monkeypatch):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake = fakebin / "claude"
    shutil.copy(REPO_ROOT / "tests" / "fake_claude", fake)
    fake.chmod(0o755)

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    report_path = session_dir / "001-report.md"
    packet_path = session_dir / "001-packet.md"
    packet_path.write_text(f"E2E tmux test packet.\nwrite your full report to {report_path}\n")
    pointer = f"Read and follow the work packet at {packet_path} — it contains your task."
    pidfile = session_dir / "pid"
    handle_file = session_dir / "iterm_id"

    # --- spawn --------------------------------------------------------------------------------
    res = tmux_backend.spawn(
        cwd=str(tmp_path), prompt=pointer, label="[Exec] e2e-tmux", pidfile=str(pidfile),
        iterm_id_file=str(handle_file), session_uuid="e2e-tmux-fake-conversation",
        env_prefix=f"export PATH={fakebin}:$PATH && ", tab_color=(1, 2, 3), rename_delay=0.5)
    assert res["ok"] is True and res["reason"] == "ok", res
    handle = res["session_id"]
    assert handle.startswith("tmux:%"), handle
    pane = handle[len("tmux:"):]
    try:
        assert handle_file.read_text() == handle
        assert _poll(pidfile.exists), "pidfile never appeared — the bootstrap did not run"
        pid = int(pidfile.read_text().strip())
        assert pid > 0

        # The stub received the prompt: it echoes the report path it extracted from the pointer.
        assert _poll(lambda: f"report_path={report_path}" in _capture(pane)), _capture(pane)

        # --- liveness / tty -------------------------------------------------------------------
        assert tmux_backend.exists_by_id(handle) is True
        assert tmux_backend.is_alive("[Exec] e2e-tmux", handle) is True
        tty = tmux_backend.tty_by_id(handle)
        want_tty = tmux_backend._tmux(["display-message", "-p", "-t", pane, "#{pane_tty}"]).stdout.strip()
        assert tty == want_tty and tty.startswith("/dev/")

        # --- spawn-time naming / color --------------------------------------------------------
        assert _opt(pane, "-w", "window-status-style") == "bg=#010203"
        assert _opt(pane, "-w", "automatic-rename") == "off"
        wname = tmux_backend._tmux(["display-message", "-p", "-t", pane, "#{window_name}"]).stdout.strip()
        assert wname == "[Exec] e2e-tmux"

        # --- send: text lands, and a SEPARATE Enter is sent ------------------------------------
        calls = []
        real_tmux = tmux_backend._tmux

        def spy(args, timeout=tmux_backend.DEFAULT_TIMEOUT):
            calls.append(list(args))
            return real_tmux(args, timeout)

        with mock.patch.object(tmux_backend, "_tmux", spy):
            assert tmux_backend.send("[Exec] e2e-tmux", "hello from e2e", handle) is True
        sends = [c for c in calls if c[0] == "send-keys"]
        assert len(sends) == 2, sends
        assert sends[0][-1] == "hello from e2e" and "-l" in sends[0]
        assert sends[1][-1] == "Enter" and "-l" not in sends[1]
        assert _poll(lambda: "[fake_claude] got: hello from e2e" in _capture(pane)), _capture(pane)

        # --- rename_by_id retitles the window and keeps automatic-rename off ---------------------
        assert tmux_backend.rename_by_id(handle, "[Exec] renamed") is True
        assert _poll(lambda: tmux_backend._tmux(
            ["display-message", "-p", "-t", pane, "#{window_name}"]).stdout.strip() == "[Exec] renamed")
        assert _opt(pane, "-w", "automatic-rename") == "off"
        assert _poll(lambda: "renamed tab to '[Exec] renamed'" in _capture(pane)), _capture(pane)

        # --- paint_tab ----------------------------------------------------------------------------
        assert tmux_backend.paint_tab(handle, (255, 0, 16)) is True
        assert _opt(pane, "-w", "window-status-style") == "bg=#ff0010"

        # --- foreign handles: False/None everywhere, and NO tmux command is run -------------------
        foreign = ["twid:1", "w0t0p0:0F2A9C4E-0000-4000-8000-000000000000", "", None]
        with mock.patch.object(tmux_backend, "_tmux") as spy_tmux:
            for h in foreign:
                assert tmux_backend.exists_by_id(h) is False
                assert tmux_backend.is_alive("x", h) is False
                assert tmux_backend.send("x", "nope", h) is False
                assert tmux_backend.press_enter("x", h) is False
                assert tmux_backend.rename_by_id(h, "n") is False
                assert tmux_backend.paint_tab(h, (1, 2, 3)) is False
                assert tmux_backend.focus("x", h) is False
                assert tmux_backend.close_by_id(h) is False
                assert tmux_backend.close("x", h) is False
                assert tmux_backend.notify(h, "t", "b") is False
                assert tmux_backend.tty_by_id(h) is None
                assert tmux_backend.title_by_id(h) is None
            assert spy_tmux.call_count == 0, spy_tmux.call_args_list

        # --- close removes the pane ----------------------------------------------------------------
        # (the stub is a child of the pane's shell; kill-pane takes the pty down with it)
        assert tmux_backend.close_by_id(handle) is True
        assert _poll(lambda: tmux_backend.exists_by_id(handle) is False)
        assert tmux_backend.is_alive("[Exec] e2e-tmux", handle) is False
        assert tmux_backend.send("x", "gone", handle) is False
    finally:
        tmux_backend._tmux(["kill-pane", "-t", pane])
