"""
iterm_pyapi — optional, best-effort placement helper using iTerm2's PYTHON API (not AppleScript).

Why this exists: packet 002 proved AppleScript's `move tab`/`set index of tab` are no-ops on this
machine — a spawned executor tab can land in the lead's WINDOW, but never truly ADJACENT to the
lead's own tab (always appended at the end of the tab bar instead). iTerm2's Python API has no such
limitation: `Window.async_create_tab(index=...)` genuinely inserts at the given index — confirmed
live (see the packet 007 report) by creating a tab at `lead_index + 1` and re-reading the window's
tab order back.

This module does PLACEMENT ONLY. Once a tab exists at the right index, `iterm.py`'s existing
AppleScript machinery (write text / /rename / pid capture / tab_color) targets that tab's session
by id exactly like it already does for the lead-window (non-adjacent) case — confirmed live that
Python API session ids and AppleScript's `id of session` are the SAME UUID space, so the handoff is
a simple string. This keeps the Python API's blast radius to "which index does the new tab land
at" and nothing else — no async_send_text, no API-side pid/rename handling to keep in sync with the
AppleScript path.

Zero new HARD dependency: the `iterm2` package is imported lazily, inside `try_create_adjacent_tab`
only, wrapped in `try/except`. If it's not installed, the API is disabled in iTerm's settings, no
window/tab/session actually contains the lead's session, or literally anything else goes wrong,
`try_create_adjacent_tab` returns None and the caller (`iterm.py`) falls back to its existing
AppleScript-only same-window placement — a spawn must never hang or fail over this being cosmetic.
"""
import asyncio

DEFAULT_TIMEOUT = 2.0  # seconds — bounds the ENTIRE connect+locate+create-tab operation


def _index_of_lead_tab(tabs_session_ids, uuid):
    """Pure, no-iterm2-needed: `tabs_session_ids` is one window's tabs, each a list of that tab's
    session ids. Returns the index of the first tab containing `uuid`, or None. Factored out so
    the placement arithmetic is unit-testable without a live iTerm2 connection."""
    for i, session_ids in enumerate(tabs_session_ids):
        if uuid in session_ids:
            return i
    return None


def _locate_lead_window_and_tab(windows_tabs_session_ids, uuid):
    """Pure: `windows_tabs_session_ids` is a list of windows, each itself a list-of-tabs-of-
    session-ids (same shape `_index_of_lead_tab` takes, one level up). Returns (window_index,
    tab_index) of the first window/tab containing `uuid`, or (None, None)."""
    for wi, tabs_session_ids in enumerate(windows_tabs_session_ids):
        ti = _index_of_lead_tab(tabs_session_ids, uuid)
        if ti is not None:
            return wi, ti
    return None, None


async def _create_adjacent_tab(uuid, timeout):
    import iterm2  # lazy: only ever imported here, inside the try/except caller below

    connection = await iterm2.Connection.async_create()
    app = await iterm2.async_get_app(connection)
    # Build the plain-data shape _locate_lead_window_and_tab expects, from the real API objects.
    shape = [[[s.session_id for s in t.sessions] for t in w.tabs] for w in app.windows]
    wi, ti = _locate_lead_window_and_tab(shape, uuid)
    if wi is None:
        return None
    window = app.windows[wi]
    new_tab = await window.async_create_tab(index=ti + 1)
    return new_tab.current_session.session_id


def try_create_adjacent_tab(lead_handle, timeout=DEFAULT_TIMEOUT):
    """Best-effort: create a new iTerm tab immediately after the lead's own tab (true index
    adjacency, not just same-window), using iTerm2's Python API. Returns the new tab's session id
    (a UUID string, same format/space as AppleScript's `id of session`) on success, or None on
    ANY failure — package not installed, Python API not enabled in iTerm's settings, connect
    timeout, lead session not found, or any other error. Never raises. Bounded to `timeout`
    seconds total (connect + locate + create-tab), so a spawn can never hang on this being
    unreachable."""
    if not lead_handle:
        return None
    uuid = lead_handle.split(":")[-1]
    try:
        return asyncio.run(asyncio.wait_for(_create_adjacent_tab(uuid, timeout), timeout=timeout))
    except Exception:
        return None
