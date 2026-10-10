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

The iTerm screen probes (`iterm.trust_prompt_live` → "no dialog", `iterm.screen_state` → "ready";
scripts/trust_prompt.py) are stubbed for every test for the same reason: send/check/list/label-retry
read an iTerm session's screen through osascript whenever a pinned conversation has no transcript
yet — which is most fixture sessions — so without the stubs ~100 tests would query the human's REAL
iTerm (and launch it if it isn't running). A test about a dialog or an unready screen pins the probe
itself (`mock.patch.object(relay.iterm, "screen_state", ...)`), and tests/test_backends.py exercises
the real functions through `REAL_ITERM_TRUST_PROMPT_LIVE` / `REAL_ITERM_SCREEN_STATE` with
`run_osascript` stubbed.

A test that specifically wants to exercise the REAL notify chain deletes this env var itself
(`monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)` — see `TestNotifyFallback` and
`TestDesktopNudgeUsesTheSharedNotifyChain` in tests/test_lead_guard.py and tests/test_relay.py) AND
mocks every OS-facing call it might reach (subprocess.run, iterm.tty_by_id, iterm.notify_via_tty),
so it still never touches the real OS even with the kill-switch off.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import iterm  # noqa: E402 — the same module object bin/relay imports as `iterm_backend`

REAL_ITERM_TRUST_PROMPT_LIVE = iterm.trust_prompt_live
REAL_ITERM_SCREEN_STATE = iterm.screen_state


@pytest.fixture(autouse=True)
def _relay_no_notify_by_default(monkeypatch):
    monkeypatch.setenv("RELAY_NO_NOTIFY", "1")
    monkeypatch.setenv("RELAY_NO_TIDY", "1")
    monkeypatch.setattr(iterm, "trust_prompt_live", lambda iterm_id: False)
    monkeypatch.setattr(iterm, "screen_state", lambda iterm_id: "ready")
    # Backend selection auto-picks tmux inside a tmux pane (scripts/backend.py). The suite must
    # behave the same whether or not it happens to run inside tmux, so the ambient tmux identity is
    # stripped here; a test about tmux sets these itself (or pins RELAY_TERMINAL=tmux).
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("RELAY_TMUX_SOCKET", raising=False)
    if __import__("os").environ.get("TERM_PROGRAM") == "tmux":
        monkeypatch.delenv("TERM_PROGRAM", raising=False)
