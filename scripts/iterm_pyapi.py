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
REORDER_TIMEOUT = 5.0  # seconds — bounds the ENTIRE connect+walk+reorder operation. Larger than
                       # DEFAULT_TIMEOUT because it walks EVERY window and may issue one
                       # async_set_tabs per window, where tab creation is a single call.


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


def _window_tab_order(tabs_session_ids, desired, follows=None):
    """Pure, no-iterm2-needed: ONE window's new tab order. `tabs_session_ids` is that window's tabs,
    each a list of that tab's session ids (the same shape `_index_of_lead_tab` takes); `desired` is
    the wanted order of session ids, which may name tabs in other windows or none at all.

    Returns `(new_order, matched)`: `new_order` is the window's tab INDEXES rearranged so the tabs
    holding a desired id come first, in `desired`'s order, with every other tab after them in its
    existing relative order; `matched` is the desired ids this window actually holds, in that same
    order. A desired id this window doesn't hold is simply skipped — it belongs to another window
    (or to no tab at all), and `try_reorder_tabs` reports the ones no window claimed. A tab holding
    two desired ids (a split pane) is placed once, at its FIRST mention.

    `follows` (backlog row 89) maps an id to the id it must sit AFTER — an executor to its lead. An
    id whose anchor this window did not match (the lead's tab is in another window, or its recorded
    handle resolves to no tab at all) is NOT moved: it stays among the untouched tabs. Without this,
    a group whose lead could not be found lost its head and its executors alone were hoisted to the
    FRONT of the window — exactly the "spawned executor lands first" incident."""
    follows = follows or {}
    front, matched = [], []
    for uuid in desired:
        anchor = follows.get(uuid)
        if anchor is not None and anchor not in matched:
            continue
        i = _index_of_lead_tab(tabs_session_ids, uuid)
        if i is None or i in front:
            continue
        front.append(i)
        matched.append(uuid)
    rest = [i for i in range(len(tabs_session_ids)) if i not in front]
    return front + rest, matched


def _moved_count(order):
    """Pure: how many tabs a window order actually changes the position of — the number `relay
    tidy` reports, 0 for a window that is already in the wanted order."""
    return sum(1 for pos, i in enumerate(order) if pos != i)


def _norm_tty(tty):
    """'/dev/ttys002' and 'ttys002' are the same device; compare on the bare name."""
    tty = str(tty or "").strip()
    return tty[len("/dev/"):] if tty.startswith("/dev/") else tty


async def _reorder_tabs(desired, timeout, tty_hints=None, follows=None, dry_run=False):
    import iterm2  # lazy: only ever imported here, inside the try/except caller below

    connection = await iterm2.Connection.async_create()
    app = await iterm2.async_get_app(connection)
    windows = list(app.windows)
    tabs_by_window = [list(w.tabs) for w in windows]
    shapes = [[[s.session_id for s in t.sessions] for t in tabs] for tabs in tabs_by_window]
    live = {sid for shape in shapes for tab in shape for sid in tab}

    # Backlog row 89: a recorded handle goes STALE when iTerm restarts (every restored tab gets a
    # new session UUID) while the claude inside keeps running. The caller hands a tty per id (the
    # tty of the claude process behind that lead/executor); an id no live tab holds is re-pointed
    # at the live session on that tty. Asked only when some id is actually unresolved.
    tty_hints = {k: _norm_tty(v) for k, v in (tty_hints or {}).items() if v}
    alias = {}
    unresolved = [u for u in desired if u not in live and tty_hints.get(u)]
    if unresolved:
        by_tty = {}
        for tabs in tabs_by_window:
            for t in tabs:
                for s in t.sessions:
                    tty = _norm_tty(await s.async_get_variable("tty"))
                    if tty:
                        by_tty.setdefault(tty, s.session_id)
        used = set(desired) & live
        for u in unresolved:
            cand = by_tty.get(tty_hints[u])
            if cand and cand not in used:
                alias[u] = cand
                used.add(cand)
    ids = [alias.get(u, u) for u in desired]
    anchors = {alias.get(k, k): alias.get(v, v) for k, v in (follows or {}).items()}

    matched, left, tabs_moved, windows_moved = [], 0, 0, 0
    for window, tabs, shape in zip(windows, tabs_by_window, shapes):
        order, here = _window_tab_order(shape, ids, anchors)
        matched.extend(here)
        held = {sid for tab in shape for sid in tab}
        left += sum(1 for u in ids if u in held and u not in here and u in anchors)
        n = _moved_count(order)
        if not here or n == 0:
            continue  # nothing of ours in this window, or it is already in the wanted order
        tabs_moved += n
        windows_moved += 1
        if dry_run:
            continue
        # ONLY ever this window's own tabs. async_set_tabs documents that "the provided tabs may
        # belong to any window. They will be moved if needed" — handing it a tab from elsewhere
        # would YANK that tab into this window, which is not what tidying a tab bar means.
        await window.async_set_tabs([tabs[i] for i in order])
    missing = sum(1 for u in ids if u not in live)
    return {"matched": matched, "tabs": tabs_moved, "windows": windows_moved, "missing": missing,
            "left": left, "resolved": len(alias)}


def try_reorder_tabs(desired, timeout=REORDER_TIMEOUT, tty_hints=None, follows=None,
                     dry_run=False):
    """Best-effort: move the tabs holding `desired`'s iTerm session ids to the FRONT of their OWN
    window, in that order, leaving every other tab in that window after them in its existing order
    (exactly what `iterm2.Window.async_set_tabs` does with a partial list). Ids may be given in
    either form — a `w#t#p#:UUID` handle or a bare UUID (the same goes for `tty_hints`/`follows`
    keys and values).

    `tty_hints` {id: tty} re-points an id no live tab holds at the tab on that tty (a handle made
    stale by an iTerm restart); `follows` {id: anchor id} leaves an id where it is when its anchor
    was not found in the same window (see `_window_tab_order`); `dry_run` computes everything and
    moves nothing.

    Returns `(ok, reason)` and NEVER raises. False + the reason when the `iterm2` package is
    missing, the Python API is disabled in iTerm's settings, the connection times out, or NO tab
    anywhere holds any of the given ids. True when at least one window claimed an id — `reason` is
    then `moved N tab(s) in M window(s)` (`would move …` on a dry run), N being the tabs whose
    POSITION actually changes (0 when already tidy), followed by how many ids no window claimed.
    Bounded to `timeout` seconds total, so a caller can never hang on this being unreachable.

    Deliberately NOT `iterm2.run_until_complete`: that helper calls `sys.exit(1)` when the
    connection is refused, which would abort the whole relay command over a cosmetic tab move.
    Same bounded `asyncio.run(...)` shape `try_create_adjacent_tab` uses."""
    def bare(x):
        return str(x).split(":")[-1]
    ids = [bare(x) for x in (desired or []) if x]
    if not ids:
        return False, "nothing to order"
    hints = {bare(k): v for k, v in (tty_hints or {}).items() if k}
    anchors = {bare(k): bare(v) for k, v in (follows or {}).items() if k and v}
    try:
        r = asyncio.run(asyncio.wait_for(
            _reorder_tabs(ids, timeout, hints, anchors, dry_run), timeout=timeout))
    except Exception as e:
        return False, f"iterm2 python api unavailable ({type(e).__name__}: {e})"
    if not r["matched"]:
        return False, "no iTerm tab found for any of the ordered session ids"
    reason = f"{'would move' if dry_run else 'moved'} {r['tabs']} tab(s) in {r['windows']} window(s)"
    if r["missing"]:
        reason += f"; {r['missing']} session id(s) had no tab"
    if r["left"]:
        reason += f"; {r['left']} executor tab(s) left in place (lead tab not in their window)"
    if r["resolved"]:
        reason += f"; {r['resolved']} stale handle(s) resolved by tty"
    return True, reason
