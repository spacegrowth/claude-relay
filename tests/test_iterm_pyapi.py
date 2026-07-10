"""
Unit tests for scripts/iterm_pyapi.py: the pure placement-index arithmetic (no live iTerm2
connection needed) and the availability-gate behavior of try_create_adjacent_tab (import blocked
→ None, no lead_handle → None, any exception during connect/locate/create → None, never raises).

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
