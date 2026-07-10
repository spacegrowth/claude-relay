"""
Layer 2 (automated, real iTerm required, not CI-able) end-to-end test: exercises the actual
AppleScript layer -- spawn, post-launch /rename, send into an existing tab, dead detection --
against a stub `claude` (tests/fake_claude) so no tokens are spent and no real model session is
involved. This is the regression gate for the riskiest part of the system: title-based addressing.

Run manually on a Mac with iTerm running:
    python3 tests/test_e2e_iterm.py
Not part of the pytest suite (not CI-able, opens real iTerm tabs) -- run standalone.
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
    """Poll `fn()` until truthy or timeout. Process/render startup latency is inherently
    variable, so a fixed sleep-then-check is a flaky test -- poll instead."""
    deadline = time.time() + timeout
    result = fn()
    while not result and time.time() < deadline:
        time.sleep(interval)
        result = fn()
    return result


def poll_until_status(check_fn, session_id, want_status, timeout=10, interval=0.3):
    """Like poll_until, but for relay._check_one -- returns the last session dict once its
    status matches `want_status`, or the last-seen dict on timeout (for a useful failure message)."""
    deadline = time.time() + timeout
    result = check_fn(session_id)
    while result["status"] != want_status and time.time() < deadline:
        time.sleep(interval)
        result = check_fn(session_id)
    return result


def main():
    tmp = Path(tempfile.mkdtemp(prefix="relay-e2e-"))
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    fake_claude_src = REPO_ROOT / "tests" / "fake_claude"
    fake_claude_dst = fakebin / "claude"
    shutil.copy(fake_claude_src, fake_claude_dst)
    fake_claude_dst.chmod(0o755)

    state_root = tmp / ".relay-tasks"
    relay = load_relay_module(state_root)
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import iterm

    worktree = str(tmp)
    session_id = "e2e-test-" + str(int(time.time()))
    label = f"relay-{session_id}"

    d = relay.packets_dir(session_id)
    d.mkdir(parents=True)
    report_path = str(d / "001-report.md")
    packet_path = d / "001-packet.md"
    packet_path.write_text(relay.build_packet("This is an E2E test packet, do nothing real.", report_path))
    pointer = relay.build_pointer_message(str(packet_path))

    print(f"[1/6] spawning session '{session_id}' with stub claude...")
    iterm.spawn(
        cwd=worktree, prompt=pointer, label=label,
        pidfile=str(relay.pid_path(session_id)),
        env_prefix=f'export PATH="{fakebin}:$PATH" && ',
    )
    pid = relay.read_pid(session_id, timeout=8)
    assert pid is not None, "FAIL: pidfile never appeared -- spawn's shell command didn't run"
    print(f"    OK pid={pid} captured")

    relay.write_session(session_id, {
        "session_id": session_id, "worktree": worktree, "topic": "e2e", "scope": "e2e",
        "tab_label": label, "model": None, "pid": pid, "status": "busy",
        "current_packet": 1, "busy_since": relay.now(), "superseded_by": None,
        "created": relay.now(), "updated": relay.now(),
    })

    print("[2/6] waiting for /rename to land, checking tab is addressable by label...")
    alive = poll_until(lambda: iterm.is_alive(label))
    assert alive, f"FAIL: tab not found under label '{label}' -- /rename didn't stick or title match is wrong"
    print("    OK tab is addressable by label")

    print("[3/6] checking status before report (should stay busy)...")
    result = relay._check_one(session_id)
    assert result["status"] == "busy", f"FAIL: expected busy, got {result['status']}"
    print("    OK status=busy")

    print("[4/6] telling stub to write its report...")
    ok = iterm.send(label, "__FAKE_CLAUDE_REPORT__")
    assert ok, "FAIL: send() could not find the tab to write the report-trigger into"
    result = poll_until_status(relay._check_one, session_id, "reported")
    assert result["status"] == "reported", f"FAIL: expected reported, got {result['status']}"
    print("    OK status=reported, report.md written")

    print("[5/6] sending a second packet into the SAME session (reuse, not fresh spawn)...")
    n = relay.next_packet_number(session_id)
    report_path_2 = str(d / f"{n:03d}-report.md")
    packet_path_2 = d / f"{n:03d}-packet.md"
    packet_path_2.write_text(relay.build_packet("Second packet, same session.", report_path_2))
    ok = iterm.send(label, relay.build_pointer_message(str(packet_path_2)))
    assert ok, "FAIL: could not send second packet into existing tab"
    # Advance the session record the way cmd_send does — current_packet=2 has NO report yet, which
    # is what lets step 6 observe `dead` (a packet WITH a report stays `reported` even after the
    # tab closes; that reported-wins rule is deliberate and unit-tested — don't "fix" it here).
    s = relay.read_session(session_id)
    s["current_packet"] = n
    s["status"] = "busy"
    s["busy_since"] = relay.now()
    relay.write_session(session_id, s)
    print(f"    OK packet {n:03d} sent into existing tab (same label, no new process)")

    print("[6/6] cleaning up: closing the test tab...")
    # Close via AppleScript: find the tab by label and close it.
    close_action = "          tell t to close\n"
    match_block = iterm._match_session_block(label, close_action)
    close_script = (
        'tell application "iTerm"\n'
        + match_block
        + "end tell"
    )
    iterm.run_osascript(close_script)
    result = poll_until_status(relay._check_one, session_id, "dead")
    print(f"    status={result['status']}")
    assert result["status"] == "dead", f"FAIL: expected dead after closing tab, got {result['status']}"
    print("    OK dead-tab detection works")

    print("\nALL E2E CHECKS PASSED")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
