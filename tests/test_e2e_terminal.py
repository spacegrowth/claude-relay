"""
Layer 2 (automated, real Terminal.app required, not CI-able) end-to-end test for the Terminal
backend: exercises the actual AppleScript layer — spawn (window-id capture), send into the running
session by window id, report detection, window close → dead — against the stub `claude`
(tests/fake_claude). Mirrors test_e2e_iterm.py; the structural difference under test is window-id
addressing instead of title matching.

Run manually on a Mac (Terminal.app will be launched if not running):
    python3 tests/test_e2e_terminal.py
Not part of the pytest suite (not CI-able, opens a real Terminal window) — run standalone.
"""
import importlib.machinery
import importlib.util
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_relay_module(state_root):
    path = str(REPO_ROOT / "bin" / "relay")
    loader = importlib.machinery.SourceFileLoader("relay_cli", path)
    spec = importlib.util.spec_from_file_location("relay_cli", path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["relay_cli"] = mod
    loader.exec_module(mod)
    mod.STATE_ROOT = state_root
    mod.LEDGER = state_root / "sessions.jsonl"
    return mod


def poll_until(fn, timeout=10, interval=0.3):
    deadline = time.time() + timeout
    result = fn()
    while not result and time.time() < deadline:
        time.sleep(interval)
        result = fn()
    return result


def poll_until_status(check_fn, session_id, want_status, timeout=10, interval=0.3):
    deadline = time.time() + timeout
    result = check_fn(session_id)
    while result["status"] != want_status and time.time() < deadline:
        time.sleep(interval)
        result = check_fn(session_id)
    return result


def main():
    tmp = Path(tempfile.mkdtemp(prefix="relay-e2e-term-"))
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    fake_claude_dst = fakebin / "claude"
    shutil.copy(REPO_ROOT / "tests" / "fake_claude", fake_claude_dst)
    fake_claude_dst.chmod(0o755)

    state_root = tmp / ".relay-tasks"
    relay = load_relay_module(state_root)
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import iterm
    import terminal_app
    # Every claude launch in this run — including the resume-relaunch inside `relay send`'s
    # fallback — must hit the stub, never the real binary. Pinning CLAUDE_BIN covers them all
    # (build_claude_cmd reads it at call time).
    iterm.CLAUDE_BIN = str(fake_claude_dst)

    session_id = "e2e-term-" + str(int(time.time()))
    label = f"[executor] {session_id}"
    d = relay.packets_dir(session_id)
    d.mkdir(parents=True)
    report_path = str(d / "001-report.md")
    packet_path = d / "001-packet.md"
    packet_path.write_text(relay.build_packet("E2E Terminal-backend test packet, do nothing real.", report_path))
    pointer = relay.build_pointer_message(str(packet_path))
    handle_file = relay.iterm_id_path(session_id)
    relay.session_dir(session_id).mkdir(parents=True, exist_ok=True)

    print(f"[1/6] spawning '{session_id}' in a Terminal.app window with stub claude...")
    terminal_app.spawn(
        cwd=str(tmp), prompt=pointer, label=label,
        pidfile=str(relay.pid_path(session_id)),
        iterm_id_file=str(handle_file),
        session_uuid="e2e-term-fake-conversation",
    )
    pid = relay.read_pid(session_id, timeout=8)
    assert pid is not None, "FAIL: pidfile never appeared — spawn's shell command didn't run"
    handle = relay.read_iterm_id(session_id, timeout=8)
    assert handle and handle.startswith("twid:"), f"FAIL: window-id handle not captured (got {handle!r})"
    print(f"    OK pid={pid}, handle={handle}")

    relay.write_session(session_id, {
        "session_id": session_id, "worktree": str(tmp), "topic": "e2e", "scope": "e2e",
        "tab_label": label, "model": None, "pid": pid, "iterm_session": handle,
        "backend": "terminal", "claude_session": "e2e-term-fake-conversation",
        "status": "busy", "current_packet": 1,
        "busy_since": relay.now(), "superseded_by": None,
        "created": relay.now(), "updated": relay.now(),
    })

    print("[2/6] window addressable by captured id...")
    assert poll_until(lambda: terminal_app.is_alive(label, handle)), \
        f"FAIL: window not found by handle {handle}"
    print("    OK window addressable")

    print("[3/6] status before report (should stay busy)...")
    result = relay._check_one(session_id)
    assert result["status"] == "busy", f"FAIL: expected busy, got {result['status']}"
    print("    OK status=busy")

    print("[4/6] triggering the stub's report via its trigger FILE (Terminal can't inject stdin)...")
    Path(report_path + ".trigger").write_text("")
    result = poll_until_status(relay._check_one, session_id, "reported")
    assert result["status"] == "reported", f"FAIL: expected reported, got {result['status']}"
    print("    OK status=reported")

    print("[5/6] follow-up packet via PRODUCTION `relay send` — on Terminal this must take the")
    print("      resume-fallback: kill+close the old window, reopen the conversation in a fresh")
    print("      window with the packet delivered as the launch prompt...")
    from types import SimpleNamespace
    p2 = tmp / "packet2.md"
    p2.write_text("Second packet, same conversation.")
    relay.cmd_send(SimpleNamespace(session_id=session_id, packet=str(p2)))
    s = relay.read_session(session_id)
    assert s["current_packet"] == 2 and s["status"] == "busy", \
        f"FAIL: expected busy on packet 002, got {s['status']} on {s['current_packet']}"
    new_pid, new_handle = s["pid"], s["iterm_session"]
    assert new_pid and new_pid != pid, f"FAIL: expected a NEW process (old {pid}, new {new_pid})"
    assert new_handle and new_handle.startswith("twid:") and new_handle != handle, \
        f"FAIL: expected a NEW window handle (old {handle}, new {new_handle})"
    # The OLD process must be gone (no second live copy of the conversation). The old WINDOW may
    # linger: Terminal ignores scripted close on some macOS versions (documented best-effort) —
    # asserting window-gone would test Apple's quirk, not relay.
    assert not relay.pid_alive(pid), "FAIL: old process still alive — two copies of one conversation"
    print(f"    OK resumed in new window (pid {new_pid}, {new_handle}); old process dead")

    print("[6/6] kill the stub (window lingers) → status must read STALLED (crashed, tab open)...")
    import os as _os
    import signal as _sig
    try:
        _os.kill(new_pid, _sig.SIGTERM)
    except ProcessLookupError:
        pass
    assert poll_until(lambda: not relay.pid_alive(new_pid), timeout=5), "FAIL: stub did not die"
    result = poll_until_status(relay._check_one, session_id, "stalled")
    # With the window still open and no report for packet 002, the truthful state is `stalled`
    # ("process gone, tab still open — go look"), not dead. If Terminal DID honor the close (some
    # versions do), `dead` is equally correct.
    assert result["status"] in ("stalled", "dead"), \
        f"FAIL: expected stalled/dead after killing the stub, got {result['status']}"
    terminal_app.close(label, new_handle)  # best-effort tidy-up; no assertion (see above)
    print(f"    OK post-kill status={result['status']}")

    print("\nALL TERMINAL-BACKEND E2E CHECKS PASSED")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
