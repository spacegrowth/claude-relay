"""
Regression test for a lead-found gap surfaced while fixing BUG-lib-8/BUG-hooks-1 (packet: "Fix the
lib/ and hooks/ bugs from the bug hunt").

D2 (the decision already made — see ~/.relay-tasks/_staging/fix-cli-bugs-packet.md's "Decisions
already made"): "`is_lead` on an unreadable marker returns False (fail open), AND `relay list`'s
LEADS table shows that lead as a distinct broken row (LIVE column `broken`, plus a red footnote:
'<sid>: lead marker unreadable — re-run /relay:mode to re-arm'). Never silently vanish."

The `is_lead`/`read_marker`/`list_leads` half of D2 is covered by
tests/test_lib_lead_guard_bughunt.py and tests/test_lead_guard.py. This file covers the OTHER
half — `relay list`'s own RENDERING of the broken row — in tests/test_relay.py's own style (same
`load_relay_module` loader, same `relay`/capsys fixtures), kept in its own file per the packet's
instruction rather than growing test_relay.py further.

Run: pytest tests/test_lib_hooks_lead_found.py -q
"""
import json as jsonlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_relay import load_relay_module  # noqa: E402 — reuse the same bin/relay module loader


@pytest.fixture
def relay(tmp_path):
    return load_relay_module(tmp_path / ".relay-tasks")


def _args(json=False, lead=None, all=False, closed=False):
    return SimpleNamespace(json=json, lead=lead, all=all, closed=closed)


def _write_unreadable_marker(relay, sid, content):
    d = relay.lead_guard.lead_dir(relay.STATE_ROOT, sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "marker.json").write_text(content)


class TestListSurfacesABrokenLeadMarker:
    """`relay list`'s LEADS table must show an unreadable marker as a distinct broken row instead
    of it silently vanishing — the exact regression BUG-lib-8/BUG-hooks-1 named."""

    def test_broken_row_renders_broken_in_the_live_column(self, relay, capsys):
        _write_unreadable_marker(relay, "corrupt-lead", "{ not json")
        relay.cmd_list(_args())
        out = capsys.readouterr().out
        assert "corrupt-lead" in out
        assert "broken" in out

    def test_valid_json_wrong_shape_is_also_a_broken_row(self, relay, capsys):
        """Not just a JSON parse error — a marker that parses fine but isn't a (non-empty) dict
        (a bare array/null) is "any error" too, same contract `is_lead`/`read_marker` now share."""
        _write_unreadable_marker(relay, "corrupt-lead", "null")
        relay.cmd_list(_args())
        out = capsys.readouterr().out
        assert "corrupt-lead" in out and "broken" in out

    def test_broken_row_prints_the_named_footnote(self, relay, capsys):
        _write_unreadable_marker(relay, "corrupt-lead", "[1, 2]")
        relay.cmd_list(_args())
        out = capsys.readouterr().out
        assert ("corrupt-lead: lead marker unreadable — re-run /relay:mode to re-arm") in out

    def test_a_real_lead_alongside_a_broken_one_still_renders_fine(self, relay, capsys):
        """A single broken marker must never blank or crash the rest of the LEADS table."""
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp")
            _write_unreadable_marker(relay, "corrupt-lead", "{ not json")
            relay.cmd_list(_args())
        out = capsys.readouterr().out
        assert "webapp" in out
        assert "corrupt-lead" in out and "broken" in out

    def test_json_output_includes_the_broken_row_verbatim(self, relay, capsys):
        _write_unreadable_marker(relay, "corrupt-lead", "{ not json")
        relay.cmd_list(_args(json=True))
        data = jsonlib.loads(capsys.readouterr().out)
        assert {"session_id": "corrupt-lead", "broken": True} in data["leads"]

    def test_a_broken_lead_never_crashes_json_output_either(self, relay, capsys):
        with mock.patch.object(relay.iterm, "is_alive", return_value=True):
            relay.lead_guard.write_marker(relay.STATE_ROOT, "lead-1", project="webapp")
            _write_unreadable_marker(relay, "corrupt-lead", "{ not json")
            relay.cmd_list(_args(json=True))
        data = jsonlib.loads(capsys.readouterr().out)
        sids = {m["session_id"] for m in data["leads"]}
        assert sids == {"lead-1", "corrupt-lead"}
