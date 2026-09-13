"""
Tests for `relay plan` — the lead-owned ordered work queue (backlog row 55, v0).

plan.json lives in the lead's dir; items are stored, status is DERIVED at print time from the ledger
(`packet_sent` source → auto-bind, `landed` → done) and the executor's report file. v0 never sends.

Helpers and the backend-stubbing fixtures (`relay`, `terms`, the autouse `_pristine_backends` /
`_no_desktop_notifications` guards) are reused from tests/test_cli_lead_succession.py, the same way
tests/test_lib_hooks_lead_found.py reuses test_relay's loader.

Run: pytest tests/test_cli_plan.py -q
"""
import json
import os

import pytest

from test_cli_lead_succession import (  # noqa: F401 — fixtures are used by name
    _no_desktop_notifications, _pristine_backends, arm_lead, ledger_events, make_session, relay,
    run_main, terms, write_packet,
)

LEAD = "lead-plan"


@pytest.fixture
def lead(relay, tmp_path, monkeypatch):
    """An armed lead whose cwd is tmp_path, so relative plan packets resolve there."""
    relay.lead_guard.write_marker(relay.STATE_ROOT, LEAD, project="webapp", cwd=str(tmp_path),
                                  tab_label="[Lead] webapp")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", LEAD)
    return LEAD


def pkt(tmp_path, name):
    d = tmp_path / "_staging"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("GOAL — x.\n")
    return p


def plan_items(relay):
    return json.loads(relay.plan_path(LEAD).read_text())["items"]


def rows(relay):
    return {r["n"]: r for r in relay.plan_rows(LEAD)}


def sent(relay, sid, n, source):
    relay.append_ledger("packet_sent", session_id=sid, packet=n, source=str(source))


class TestAddRmDone:
    def test_add_appends_and_writes_the_v0_shape(self, relay, lead, tmp_path, capsys):
        pkt(tmp_path, "a-packet.md")
        run_main(relay, "plan", "add", "_staging/a-packet.md", "--model", "sonnet", "--note", "row 57")
        d = json.loads(relay.plan_path(LEAD).read_text())
        assert d["version"] == 1
        (it,) = d["items"]
        assert it["n"] == 1 and it["packet"] == "_staging/a-packet.md"   # stored as given
        assert it["target"] == "fresh" and it["model"] == "sonnet" and it["note"] == "row 57"
        assert it["executor"] is None and it["packet_n"] is None and it["done"] is None and it["added"]
        assert ledger_events(relay, "plan_add")[0]["session_id"] == LEAD

    def test_add_refuses_a_missing_packet(self, relay, lead, tmp_path):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "plan", "add", "_staging/nope-packet.md")
        assert "does not exist" in str(e.value)
        assert not relay.plan_path(LEAD).exists()

    def test_before_inserts_and_renumbers(self, relay, lead, tmp_path):
        for name in ("a-packet.md", "b-packet.md", "c-packet.md"):
            pkt(tmp_path, name)
        run_main(relay, "plan", "add", "_staging/a-packet.md")
        run_main(relay, "plan", "add", "_staging/b-packet.md")
        run_main(relay, "plan", "add", "_staging/c-packet.md", "--before", "2")
        assert [(i["n"], os.path.basename(i["packet"])) for i in plan_items(relay)] == [
            (1, "a-packet.md"), (2, "c-packet.md"), (3, "b-packet.md")]

    def test_rm_removes_and_renumbers(self, relay, lead, tmp_path):
        for name in ("a-packet.md", "b-packet.md", "c-packet.md"):
            pkt(tmp_path, name)
            run_main(relay, "plan", "add", f"_staging/{name}")
        run_main(relay, "plan", "rm", "1")
        assert [(i["n"], os.path.basename(i["packet"])) for i in plan_items(relay)] == [
            (1, "b-packet.md"), (2, "c-packet.md")]
        assert ledger_events(relay, "plan_rm")[0]["n"] == 1
        with pytest.raises(SystemExit) as e:
            run_main(relay, "plan", "rm", "9")
        assert "no item #9" in str(e.value)

    def test_done_marks_by_hand(self, relay, lead, tmp_path):
        pkt(tmp_path, "a-packet.md")
        run_main(relay, "plan", "add", "_staging/a-packet.md")
        run_main(relay, "plan", "done", "1", "--note", "landed outside relay")
        it = plan_items(relay)[0]
        assert it["done"] and it["note"] == "landed outside relay"
        assert rows(relay)[1]["status"] == "done"
        assert ledger_events(relay, "plan_done")

    def test_bind_ties_an_item_to_an_executor(self, relay, lead, tmp_path):
        pkt(tmp_path, "a-packet.md")
        make_session(relay, "e1", owner_lead=LEAD, status="busy", current_packet=3)
        run_main(relay, "plan", "add", "_staging/a-packet.md")
        run_main(relay, "plan", "bind", "1", "e1")
        it = plan_items(relay)[0]
        assert it["executor"] == "e1" and it["packet_n"] == 3
        run_main(relay, "plan", "bind", "1", "e1", "--packet", "2")
        assert plan_items(relay)[0]["packet_n"] == 2
        assert ledger_events(relay, "plan_bind")[-1]["executor"] == "e1"

    def test_a_non_lead_session_is_refused(self, relay, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "not-a-lead")
        with pytest.raises(SystemExit) as e:
            run_main(relay, "plan")
        assert "not in lead mode" in str(e.value)
        with pytest.raises(SystemExit) as e:
            run_main(relay, "plan", "add", "x", "--session", "also-not")
        assert "'also-not' is not in lead mode" in str(e.value)

    def test_session_flag_works_before_or_after_the_verb(self, relay, tmp_path, monkeypatch):
        relay.lead_guard.write_marker(relay.STATE_ROOT, LEAD, project="w", cwd=str(tmp_path))
        pkt(tmp_path, "a-packet.md")
        run_main(relay, "plan", "add", "_staging/a-packet.md", "--session", LEAD)
        run_main(relay, "plan", "--session", LEAD, "add", "_staging/a-packet.md")
        assert len(plan_items(relay)) == 2


class TestStatus:
    def test_the_four_states(self, relay, lead, tmp_path):
        for name in ("q-packet.md", "f-packet.md", "r-packet.md", "d-packet.md"):
            pkt(tmp_path, name)
            run_main(relay, "plan", "add", f"_staging/{name}")
        make_session(relay, "e-busy", owner_lead=LEAD, status="busy")
        make_session(relay, "e-rep", owner_lead=LEAD, status="reported")
        make_session(relay, "e-land", owner_lead=LEAD, status="reported")
        sent(relay, "e-busy", 1, tmp_path / "_staging/f-packet.md")
        sent(relay, "e-rep", 1, tmp_path / "_staging/r-packet.md")
        sent(relay, "e-land", 1, tmp_path / "_staging/d-packet.md")
        relay.append_ledger("landed", session_id="e-land", packet=1, trigger="t", sha="abc")
        r = rows(relay)
        assert [r[i]["status"] for i in (1, 2, 3, 4)] == ["queued", "in-flight", "reported", "done"]
        # auto-bind was written back
        items = plan_items(relay)
        assert (items[1]["executor"], items[1]["packet_n"]) == ("e-busy", 1)
        assert items[0]["executor"] is None

    def test_autobind_ignores_other_leads_and_earlier_sends(self, relay, lead, tmp_path):
        pkt(tmp_path, "a-packet.md")
        make_session(relay, "e-old", owner_lead=LEAD, status="busy")
        # BEFORE the add (ledger ts is second-resolution; a same-second send deliberately binds, so
        # the "earlier" send is written with an unmistakably older stamp)
        with relay.LEDGER.open("a") as f:
            f.write(json.dumps({"ts": "2020-01-01T00:00:00", "event": "packet_sent", "session_id": "e-old",
                                "packet": 1, "source": str(tmp_path / "_staging/a-packet.md")}) + "\n")
        run_main(relay, "plan", "add", "_staging/a-packet.md")
        make_session(relay, "e-foreign", owner_lead="someone-else", status="busy")
        sent(relay, "e-foreign", 1, tmp_path / "_staging/a-packet.md")
        assert rows(relay)[1]["status"] == "queued"

    def test_table_and_footer(self, relay, lead, tmp_path, capsys):
        pkt(tmp_path, "a-packet.md")
        run_main(relay, "plan", "add", "_staging/a-packet.md", "--note", "row 57")
        capsys.readouterr()
        run_main(relay, "plan")
        out = capsys.readouterr().out
        for h in ("#", "STATUS", "PACKET", "TARGET", "MODEL", "NOTE"):
            assert h in out
        assert "a-packet.md" in out and "row 57" in out
        assert "1 queued · 0 in flight · 0 reported · 0 done" in out

    def test_json_carries_the_full_path(self, relay, lead, tmp_path, capsys):
        p = pkt(tmp_path, "a-packet.md")
        run_main(relay, "plan", "add", "_staging/a-packet.md")
        capsys.readouterr()
        run_main(relay, "plan", "status", "--json")
        d = json.loads(capsys.readouterr().out)
        assert d["items"][0]["packet_path"] == str(p.resolve())
        assert d["items"][0]["status"] == "queued"


class TestNext:
    def test_fresh_target_prints_a_spawn_command(self, relay, lead, tmp_path, capsys):
        p = pkt(tmp_path, "heavy-banner-packet.md")
        run_main(relay, "plan", "add", "_staging/heavy-banner-packet.md", "--model", "sonnet")
        capsys.readouterr()
        run_main(relay, "plan", "next")
        out = capsys.readouterr().out.strip()
        assert out == (f"relay spawn {tmp_path} heavy-banner {p.resolve()} --model sonnet "
                       f"--lead $CLAUDE_CODE_SESSION_ID")

    def test_executor_target_prints_a_send_command(self, relay, lead, tmp_path, capsys):
        p = pkt(tmp_path, "b-packet.md")
        make_session(relay, "e1", owner_lead=LEAD, status="reported")
        run_main(relay, "plan", "add", "_staging/b-packet.md", "--target", "e1")
        capsys.readouterr()
        run_main(relay, "plan", "next")
        assert capsys.readouterr().out.strip() == f"relay send e1 {p.resolve()}"

    def test_skips_non_queued_and_exits_1_when_empty(self, relay, lead, tmp_path, capsys):
        with pytest.raises(SystemExit) as e:
            run_main(relay, "plan", "next")
        assert e.value.code == 1
        pkt(tmp_path, "a-packet.md")
        run_main(relay, "plan", "add", "_staging/a-packet.md")
        run_main(relay, "plan", "done", "1")
        capsys.readouterr()
        with pytest.raises(SystemExit) as e:
            run_main(relay, "plan", "next")
        assert e.value.code == 1
        assert capsys.readouterr().out == ""


class TestLedgerSourceAndList:
    def test_a_real_spawn_stamps_source_and_autobinds(self, relay, terms, lead, tmp_path):
        p = write_packet(tmp_path, name="s-packet.md")
        run_main(relay, "plan", "add", p)
        run_main(relay, "spawn", str(tmp_path), "t", p, "--name", "e1", "--lead", LEAD)
        ev = ledger_events(relay, "packet_sent")[-1]
        assert ev["source"] == str(tmp_path.joinpath("s-packet.md").resolve())
        assert ledger_events(relay, "spawned")[-1]["source"] == ev["source"]
        r = rows(relay)[1]
        assert r["executor"] == "e1" and r["packet_n"] == 1 and r["status"] == "in-flight"

    def test_list_plan_prints_the_table_and_json_carries_plan(self, relay, terms, lead, tmp_path,
                                                             capsys):
        pkt(tmp_path, "a-packet.md")
        run_main(relay, "plan", "add", "_staging/a-packet.md")
        capsys.readouterr()
        run_main(relay, "list", "--plan")
        out = capsys.readouterr().out
        assert "PLAN" in out and "a-packet.md" in out and out.index("EXECUTORS") < out.index("PLAN")
        run_main(relay, "list", "--json")
        d = json.loads(capsys.readouterr().out)
        assert d["plan"][0]["packet"] == "_staging/a-packet.md"

    def test_list_plan_is_a_noop_without_a_lead(self, relay, terms, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        run_main(relay, "list", "--plan")
        assert "PLAN" not in capsys.readouterr().out
        run_main(relay, "list", "--json")
        assert "plan" not in json.loads(capsys.readouterr().out)


class TestHandoff:
    def test_handoff_copies_plan_json_to_the_successor(self, relay, terms, lead, tmp_path):
        pkt(tmp_path, "a-packet.md")
        run_main(relay, "plan", "add", "_staging/a-packet.md", "--note", "carry me")
        before = relay.plan_path(LEAD).read_text()
        doc = tmp_path / "handoff.md"
        doc.write_text("# Handoff\n\nIn flight: nothing.\n")
        run_main(relay, "handoff", str(doc))
        succ = next(m["session_id"] for m in relay.lead_guard.list_leads(relay.STATE_ROOT))
        assert succ != LEAD
        assert relay.plan_path(succ).read_text() == before
