"""Session-wide test safety net.

RELAY_NO_NOTIFY=1 is set for EVERY test by default so no test run ever pops a REAL desktop banner
(iTerm's OSC tier, or the osascript fallback) as a side effect of exercising code that calls
`lead_guard.notify_banner`. Needed as of the packet-002 fix
that made `tombstone_lead`/`migrate_lead` call `notify_banner` unconditionally on every successful
call (lead-found notify gap: a hijack or silent tombstone used to happen with nothing on screen) —
dozens of pre-existing tests across this suite exercise those two functions directly with zero
notify awareness (some even pass a plausible-looking `iterm_session` string), and without this
fixture every one of those runs would shell out to the real notifier.

RELAY_NO_TIDY=1 is set for every test for the same reason, one notch more serious: `relay tidy`
(and the automatic tidy after spawn/handoff/resume/restart) reorders REAL iTerm tabs through
iTerm's Python API, and the tab bar it would reorder is the human's own. `iterm.reorder_tabs`
checks this variable first and returns (False, "disabled by RELAY_NO_TIDY") — so even a test that
reaches the automatic tidy through an unstubbed backend can never move a real tab. A test asserting
ON the tidy stubs `reorder_tabs` at the backend seam (see tests/test_cli_tidy.py) rather than
unsetting this.

A test that specifically wants to exercise the REAL notify chain deletes this env var itself
(`monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)` — see `TestNotifyFallback` and
`TestDesktopNudgeUsesTheSharedNotifyChain` in tests/test_lead_guard.py and tests/test_relay.py) AND
mocks every OS-facing call it might reach (subprocess.run, iterm.tty_by_id, iterm.notify_via_tty),
so it still never touches the real OS even with the kill-switch off.
"""
import pytest


@pytest.fixture(autouse=True)
def _relay_no_notify_by_default(monkeypatch):
    monkeypatch.setenv("RELAY_NO_NOTIFY", "1")
    monkeypatch.setenv("RELAY_NO_TIDY", "1")
