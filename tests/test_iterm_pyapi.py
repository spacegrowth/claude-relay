"""
Unit tests for scripts/iterm_pyapi.py: the pure placement-index arithmetic (no live iTerm2
connection needed) and the availability-gate behavior of try_create_adjacent_tab (import blocked
→ None, no lead_handle → None, any exception during connect/locate/create → None, never raises) —
plus the same two halves for try_reorder_tabs (`relay tidy`'s only route to a real tab bar).

Every iterm2 object here is a mock: no test in this file ever connects to iTerm2, and none may.

Run: pytest tests/test_iterm_pyapi.py -v
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import iterm_pyapi as pyapi  # noqa: E402


class TestIndexOfLeadTab:
    def test_finds_matching_tab(self):
        tabs = [["a", "b"], ["c"], ["d", "e"]]
        assert pyapi._index_of_lead_tab(tabs, "c") == 1

    def test_finds_tab_with_multiple_sessions(self):
        tabs = [["a", "b"], ["c"], ["d", "e"]]
        assert pyapi._index_of_lead_tab(tabs, "e") == 2

    def test_no_match_returns_none(self):
        tabs = [["a", "b"], ["c"]]
        assert pyapi._index_of_lead_tab(tabs, "zzz") is None

    def test_empty_tabs_returns_none(self):
        assert pyapi._index_of_lead_tab([], "a") is None


class TestLocateLeadWindowAndTab:
    def test_finds_window_and_tab(self):
        windows = [
            [["a"], ["b"]],           # window 0: 2 tabs
            [["c"], ["d", "e"]],      # window 1: 2 tabs, second has 2 sessions
        ]
        assert pyapi._locate_lead_window_and_tab(windows, "e") == (1, 1)

    def test_finds_in_first_window(self):
        windows = [[["a"], ["b"]], [["c"]]]
        assert pyapi._locate_lead_window_and_tab(windows, "b") == (0, 1)

    def test_no_match_returns_none_none(self):
        windows = [[["a"], ["b"]]]
        assert pyapi._locate_lead_window_and_tab(windows, "zzz") == (None, None)

    def test_no_windows_returns_none_none(self):
        assert pyapi._locate_lead_window_and_tab([], "a") == (None, None)


class TestTryCreateAdjacentTab:
    def test_no_lead_handle_returns_none(self):
        assert pyapi.try_create_adjacent_tab(None) is None
        assert pyapi.try_create_adjacent_tab("") is None

    def test_import_blocked_returns_none(self, monkeypatch):
        # Simulate the `iterm2` package genuinely not being installed: setting sys.modules[name]
        # to None makes any subsequent `import iterm2` raise ImportError (documented Python
        # behavior), without needing to actually uninstall the real package for this test.
        monkeypatch.setitem(sys.modules, "iterm2", None)
        assert pyapi.try_create_adjacent_tab("w1t2p0:SOME-UUID") is None

    def test_connect_exception_returns_none_never_raises(self, monkeypatch):
        # Simulate the package being present but the connection failing (API disabled in iTerm's
        # settings, iTerm not running, etc.) — must degrade silently, not raise.
        fake_iterm2 = mock.MagicMock()

        async def boom():
            raise ConnectionRefusedError("no API listener")

        fake_iterm2.Connection.async_create = boom
        monkeypatch.setitem(sys.modules, "iterm2", fake_iterm2)
        assert pyapi.try_create_adjacent_tab("w1t2p0:SOME-UUID") is None

    def test_lead_not_found_returns_none(self, monkeypatch):
        # Package present, connects fine, but no window/tab contains the lead's session id.
        fake_iterm2 = mock.MagicMock()

        async def fake_connect():
            return mock.MagicMock()

        async def fake_get_app(connection):
            app = mock.MagicMock()
            app.windows = []  # no windows at all → definitely not found
            return app

        fake_iterm2.Connection.async_create = fake_connect
        fake_iterm2.async_get_app = fake_get_app
        monkeypatch.setitem(sys.modules, "iterm2", fake_iterm2)
        assert pyapi.try_create_adjacent_tab("w1t2p0:SOME-UUID") is None

    def test_success_returns_new_session_id(self, monkeypatch):
        # Package present, connects fine, lead session found in window 0 tab 0 → a tab is created
        # at index 1 and its session id is returned.
        fake_iterm2 = mock.MagicMock()

        lead_session = mock.MagicMock(session_id="LEAD-UUID")
        lead_tab = mock.MagicMock(sessions=[lead_session])
        new_session = mock.MagicMock(session_id="NEW-TAB-SESSION-ID")
        new_tab = mock.MagicMock(current_session=new_session)

        window = mock.MagicMock()
        window.tabs = [lead_tab]

        async def async_create_tab(index):
            assert index == 1   # lead's tab was at index 0 → adjacent means index 1
            return new_tab

        window.async_create_tab = async_create_tab

        async def fake_connect():
            return mock.MagicMock()

        async def fake_get_app(connection):
            app = mock.MagicMock()
            app.windows = [window]
            return app

        fake_iterm2.Connection.async_create = fake_connect
        fake_iterm2.async_get_app = fake_get_app
        monkeypatch.setitem(sys.modules, "iterm2", fake_iterm2)
        result = pyapi.try_create_adjacent_tab("w1t2p0:LEAD-UUID")
        assert result == "NEW-TAB-SESSION-ID"


class TestWindowTabOrder:
    """`_window_tab_order`: "the tabs holding a desired id come first, in `desired`'s order, with
    every other tab after them in its existing relative order"."""

    def test_desired_tabs_move_to_the_front_in_order(self):
        tabs = [["x"], ["b"], ["y"], ["a"]]
        order, matched = pyapi._window_tab_order(tabs, ["a", "b"])
        assert order == [3, 1, 0, 2]        # a, b, then x and y in their existing order
        assert matched == ["a", "b"]

    def test_unknown_tabs_keep_their_existing_relative_order(self):
        tabs = [["p"], ["q"], ["a"], ["r"]]
        order, _ = pyapi._window_tab_order(tabs, ["a"])
        assert order == [2, 0, 1, 3]

    def test_ids_this_window_does_not_hold_are_skipped(self):
        tabs = [["a"], ["z"]]
        order, matched = pyapi._window_tab_order(tabs, ["elsewhere", "a", "nowhere"])
        assert matched == ["a"]
        assert order == [0, 1]

    def test_a_tab_named_twice_is_placed_once(self):
        """A split pane: one tab, two sessions, both in `desired`."""
        tabs = [["x"], ["lead", "exec"]]
        order, matched = pyapi._window_tab_order(tabs, ["lead", "exec"])
        assert order == [1, 0]
        assert matched == ["lead"]

    def test_an_already_ordered_window_is_unchanged(self):
        tabs = [["a"], ["b"], ["c"]]
        order, _ = pyapi._window_tab_order(tabs, ["a", "b"])
        assert order == [0, 1, 2]

    def test_no_tabs_is_empty(self):
        assert pyapi._window_tab_order([], ["a"]) == ([], [])


def _fake_app(monkeypatch, windows):
    """An `iterm2` module whose async_get_app returns `windows`. Nothing here talks to iTerm."""
    fake_iterm2 = mock.MagicMock()

    async def fake_connect():
        return mock.MagicMock()

    async def fake_get_app(connection):
        app = mock.MagicMock()
        app.windows = windows
        return app

    fake_iterm2.Connection.async_create = fake_connect
    fake_iterm2.async_get_app = fake_get_app
    monkeypatch.setitem(sys.modules, "iterm2", fake_iterm2)
    return fake_iterm2


def _window(*tab_session_ids):
    """A mock iTerm window whose tabs hold the given session ids, recording every async_set_tabs
    call on `window.set_calls` as a list of the session-id lists it was handed."""
    tabs = []
    for ids in tab_session_ids:
        tabs.append(mock.MagicMock(sessions=[mock.MagicMock(session_id=i) for i in ids]))
    window = mock.MagicMock()
    window.tabs = tabs
    window.set_calls = []

    async def async_set_tabs(new_tabs):
        window.set_calls.append([[s.session_id for s in t.sessions] for t in new_tabs])

    window.async_set_tabs = async_set_tabs
    return window


class TestTryReorderTabs:
    def test_no_ids_is_refused_without_connecting(self):
        assert pyapi.try_reorder_tabs([]) == (False, "nothing to order")
        assert pyapi.try_reorder_tabs(None)[0] is False

    def test_import_blocked_returns_false_and_a_reason(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "iterm2", None)
        ok, reason = pyapi.try_reorder_tabs(["w0t0p0:A"])
        assert ok is False and "unavailable" in reason

    def test_a_refused_connection_never_raises(self, monkeypatch):
        fake_iterm2 = mock.MagicMock()

        async def boom():
            raise ConnectionRefusedError("no API listener")

        fake_iterm2.Connection.async_create = boom
        monkeypatch.setitem(sys.modules, "iterm2", fake_iterm2)
        ok, reason = pyapi.try_reorder_tabs(["w0t0p0:A"])
        assert ok is False and "ConnectionRefusedError" in reason

    def test_no_matching_tab_returns_false(self, monkeypatch):
        window = _window(["OTHER"])
        _fake_app(monkeypatch, [window])
        ok, reason = pyapi.try_reorder_tabs(["w0t0p0:A"])
        assert ok is False and "no iTerm tab found" in reason
        assert window.set_calls == []

    def test_handles_and_bare_uuids_both_resolve(self, monkeypatch):
        window = _window(["X"], ["LEAD"], ["EXEC"])
        _fake_app(monkeypatch, [window])
        ok, _ = pyapi.try_reorder_tabs(["w0t1p0:LEAD", "EXEC"])
        assert ok is True
        assert window.set_calls == [[["LEAD"], ["EXEC"], ["X"]]]

    def test_each_window_is_ordered_with_only_its_own_tabs(self, monkeypatch):
        """async_set_tabs MOVES a tab that belongs to another window — so a window may only ever be
        handed its own tabs, or tidying would drag tabs across windows."""
        w1 = _window(["A-EXEC"], ["A-LEAD"])
        w2 = _window(["B-EXEC"], ["OTHER"], ["B-LEAD"])
        _fake_app(monkeypatch, [w1, w2])
        ok, _ = pyapi.try_reorder_tabs(["A-LEAD", "A-EXEC", "B-LEAD", "B-EXEC"])
        assert ok is True
        assert w1.set_calls == [[["A-LEAD"], ["A-EXEC"]]]
        assert w2.set_calls == [[["B-LEAD"], ["B-EXEC"], ["OTHER"]]]

    def test_a_window_already_in_order_is_left_untouched(self, monkeypatch):
        window = _window(["LEAD"], ["EXEC"], ["X"])
        _fake_app(monkeypatch, [window])
        ok, reason = pyapi.try_reorder_tabs(["LEAD", "EXEC"])
        assert ok is True and reason == "moved 0 tab(s) in 0 window(s)"   # row 89: a real count
        assert window.set_calls == []

    def test_ids_with_no_tab_are_reported_but_do_not_fail_the_tidy(self, monkeypatch):
        window = _window(["X"], ["LEAD"])
        _fake_app(monkeypatch, [window])
        ok, reason = pyapi.try_reorder_tabs(["LEAD", "GONE"])
        assert ok is True
        assert "1 session id(s) had no tab" in reason
        assert window.set_calls == [[["LEAD"], ["X"]]]


# ── backlog row 89 ──────────────────────────────────────────────────────────────────────────────

def _tty_window(*tabs):
    """Like `_window`, but each tab is (session_id, tty) and sessions answer
    `async_get_variable("tty")` the way the real iterm2 Session does."""
    window = _window(*[[sid] for sid, _ in tabs])
    for t, (_, tty) in zip(window.tabs, tabs):
        async def get_var(name, _tty=tty):
            return _tty if name == "tty" else None
        t.sessions[0].async_get_variable = get_var
    return window


class TestRow89Placement:
    def test_moved_count_is_positions_changed(self):
        assert pyapi._moved_count([0, 1, 2]) == 0
        assert pyapi._moved_count([2, 0, 1]) == 3
        assert pyapi._moved_count([1, 0, 2]) == 2

    def test_an_executor_whose_lead_is_not_in_the_window_is_not_hoisted(self):
        """The incident: the lead's id matched nothing, the lone executor went to index 0."""
        tabs = [["L1"], ["L2"], ["EXEC"]]
        order, matched = pyapi._window_tab_order(tabs, ["STALE-LEAD", "EXEC"],
                                                 {"EXEC": "STALE-LEAD"})
        assert order == [0, 1, 2] and matched == []
        # …and pre-fix (no anchors) exactly that hoist:
        assert pyapi._window_tab_order(tabs, ["STALE-LEAD", "EXEC"])[0] == [2, 0, 1]

    def test_an_anchored_executor_follows_its_found_lead(self):
        tabs = [["EXEC"], ["X"], ["LEAD"]]
        order, matched = pyapi._window_tab_order(tabs, ["LEAD", "EXEC"], {"EXEC": "LEAD"})
        assert order == [2, 0, 1] and matched == ["LEAD", "EXEC"]

    def test_reason_counts_moved_tabs_and_windows(self, monkeypatch):
        w = _window(["EXEC"], ["X"], ["LEAD"])
        _fake_app(monkeypatch, [w])
        ok, reason = pyapi.try_reorder_tabs(["LEAD", "EXEC"], follows={"EXEC": "LEAD"})
        assert ok and reason == "moved 3 tab(s) in 1 window(s)"
        assert w.set_calls == [[["LEAD"], ["EXEC"], ["X"]]]

    def test_dry_run_counts_and_moves_nothing(self, monkeypatch):
        w = _window(["EXEC"], ["X"], ["LEAD"])
        _fake_app(monkeypatch, [w])
        ok, reason = pyapi.try_reorder_tabs(["LEAD", "EXEC"], dry_run=True)
        assert ok and reason == "would move 3 tab(s) in 1 window(s)"
        assert w.set_calls == []

    def test_a_stale_handle_is_resolved_by_tty(self, monkeypatch):
        """iTerm restarted: the lead's recorded UUID is gone, its claude still runs on ttys002."""
        w = _tty_window(("EXEC", "/dev/ttys000"), ("OTHER", "/dev/ttys001"),
                        ("LIVE-LEAD", "/dev/ttys002"))
        _fake_app(monkeypatch, [w])
        ok, reason = pyapi.try_reorder_tabs(
            ["w1t7p0:STALE-LEAD", "w1t2p0:EXEC"], tty_hints={"w1t7p0:STALE-LEAD": "ttys002"},
            follows={"w1t2p0:EXEC": "w1t7p0:STALE-LEAD"})
        assert ok and reason == "moved 3 tab(s) in 1 window(s); 1 stale handle(s) resolved by tty"
        assert w.set_calls == [[["LIVE-LEAD"], ["EXEC"], ["OTHER"]]]

    def test_a_tty_already_claimed_by_a_live_id_is_not_reused(self, monkeypatch):
        w = _tty_window(("LEAD", "/dev/ttys002"), ("EXEC", "/dev/ttys003"))
        _fake_app(monkeypatch, [w])
        ok, reason = pyapi.try_reorder_tabs(["LEAD", "GHOST", "EXEC"],
                                            tty_hints={"GHOST": "/dev/ttys002"})
        assert ok and "1 session id(s) had no tab" in reason and "resolved" not in reason

    def test_unanchored_executors_are_reported_as_left_in_place(self, monkeypatch):
        w = _window(["X"], ["EXEC"])
        _fake_app(monkeypatch, [w])
        ok, reason = pyapi.try_reorder_tabs(["X", "GONE", "EXEC"], follows={"EXEC": "GONE"})
        assert ok and w.set_calls == []
        assert reason == ("moved 0 tab(s) in 0 window(s); 1 session id(s) had no tab; "
                          "1 executor tab(s) left in place (lead tab not in their window)")
