"""
Bug-hunt tests for lib/lead_guard.py — part 3 of 3: concurrency and addressing.

The poll lock (one background report-watcher per lead), the executor-side escalation state, and
transport-v2 peer addressing. Parts 1 and 2 are tests/test_lib_lead_guard_bughunt.py and
tests/test_lib_lead_guard_policy_bughunt.py; the fixtures below mirror part 1's.

Oracle: `_poll_lock_status`'s docstring (the ONE staleness definition), wake-watch design §9 as
quoted in `escalation_decision`, and the #28-phase-1 comment block above `peer_registry_entries`.

Run: pytest tests/test_lib_lead_guard_locks_bughunt.py -v
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
import lead_guard as lg  # noqa: E402


DEAD_PID = 999_999            # far above the default pid_max; never alive on macOS/Linux CI


@pytest.fixture
def sr(tmp_path):
    """An isolated state root. Never ~/.relay-tasks."""
    root = tmp_path / ".relay-tasks"
    root.mkdir()
    return root


@pytest.fixture
def a_file(tmp_path):
    """A path that is a FILE — using it as a state_root makes every mkdir/write below it fail."""
    p = tmp_path / "not-a-dir"
    p.write_text("x")
    return p


def armed(sr, sid="lead-1", **fields):
    lg.write_marker(sr, sid, project="proj", cwd="/tmp", color=[1, 2, 3], **fields)
    return sid


# ── the poll lock (one background watcher per lead) ────────────────────────────────────────────
class TestPollLock:
    def test_absent_live_and_reclaimed(self, sr):
        armed(sr, "L1")
        assert lg.poll_lock_state(sr, "L1") == "absent"
        assert lg.acquire_poll_lock(sr, "L1") is True
        assert lg.poll_lock_state(sr, "L1") == "live"
        assert lg.acquire_poll_lock(sr, "L1") is False      # a live poller already holds it
        lg.heartbeat_poll_lock(sr, "L1")
        lg.release_poll_lock(sr, "L1")
        assert lg.poll_lock_state(sr, "L1") == "absent"

    @pytest.mark.parametrize("content,why", [
        ("", "empty file"),
        ("garbage", "not JSON and not an int"),
        (json.dumps({"pid": DEAD_PID, "pid_started": "x", "ts": time.time()}), "dead pid"),
        (json.dumps({"pid": os.getpid(), "pid_started": "definitely-not-now",
                     "ts": time.time()}), "pid recycled"),
        (json.dumps({"pid": os.getpid(), "ts": time.time() - 10_000}), "heartbeat stopped"),
        (json.dumps({"pid": os.getpid()}), "no heartbeat ts at all"),
        (json.dumps({"pid": os.getpid(), "ts": "not-a-float"}), "unparsable ts"),
        (str(DEAD_PID), "legacy bare int, dead pid"),
    ])
    def test_every_documented_staleness_shape_is_reclaimable(self, sr, content, why):
        """_poll_lock_status' docstring enumerates these; 'Never raises — any bad input is treated
        as "stale" so it's reclaimable rather than a permanent block.'"""
        armed(sr, "L1")
        lg._lock_path(sr, "L1").write_text(content)
        assert lg.poll_lock_state(sr, "L1") == "stale", why
        assert lg.acquire_poll_lock(sr, "L1") is True, why

    def test_a_fresh_legacy_bare_int_lock_still_reads_live(self, sr):
        """'A legacy (pre-heartbeat) bare-int lock … is judged stale purely by file mtime against
        _LEGACY_LOCK_TTL.'"""
        armed(sr, "L1")
        lg._lock_path(sr, "L1").write_text(str(os.getpid()))
        assert lg.poll_lock_state(sr, "L1") == "live"

    def test_a_stale_legacy_lock_by_mtime_is_reclaimable(self, sr):
        armed(sr, "L1")
        p = lg._lock_path(sr, "L1")
        p.write_text(str(os.getpid()))
        old = time.time() - lg._LEGACY_LOCK_TTL - 60
        os.utime(p, (old, old))
        assert lg.poll_lock_state(sr, "L1") == "stale"

    def test_a_lock_that_is_a_directory_is_stale_not_an_exception(self, sr):
        armed(sr, "L1")
        lg._lock_path(sr, "L1").mkdir()
        assert lg.poll_lock_state(sr, "L1") == "stale"
        assert lg._poll_lock_status(lg._lock_path(sr, "L1"), 5) == "stale"

    def test_acquire_never_raises_when_the_lock_cannot_be_written(self, a_file):
        assert lg.acquire_poll_lock(a_file, "L1") is False

    def test_the_read_only_view_never_raises_on_a_hostile_state_root(self):
        """poll_lock_state: 'Never breaks or touches the lock … Never raises.'"""
        assert lg.poll_lock_state(None, "L1") == "stale"

    def test_heartbeat_and_release_never_touch_someone_elses_lock(self, sr):
        """_heartbeat_lock: 'ONLY rewrites when the lock's pid is THIS process; never stomps
        another holder's lock.' _release_lock: 'ONLY releases when the lock is still ours.'"""
        armed(sr, "L1")
        p = lg._lock_path(sr, "L1")
        theirs = json.dumps({"pid": DEAD_PID, "ts": time.time()})
        p.write_text(theirs)
        lg.heartbeat_poll_lock(sr, "L1")
        lg.release_poll_lock(sr, "L1")
        assert p.read_text() == theirs
        p.write_text(str(DEAD_PID))                 # legacy int owned by someone else
        lg.release_poll_lock(sr, "L1")
        assert p.exists()

    def test_heartbeat_and_release_are_no_ops_with_no_lock_or_garbage(self, sr):
        armed(sr, "L1")
        lg.heartbeat_poll_lock(sr, "L1")            # absent → early return
        lg.release_poll_lock(sr, "L1")
        lg._lock_path(sr, "L1").write_text("garbage")
        lg.heartbeat_poll_lock(sr, "L1")            # unparsable → don't touch it
        lg.release_poll_lock(sr, "L1")
        assert lg._lock_path(sr, "L1").read_text() == "garbage"
        lg._lock_path(sr, "L1").unlink()
        lg._lock_path(sr, "L1").mkdir()
        lg.release_poll_lock(sr, "L1")              # a directory → swallowed

    def test_pid_start_time_is_none_for_a_nonsense_pid(self):
        """_pid_start_time: 'or None on any failure. SINGLE SOURCE OF TRUTH for pid-reuse
        detection.'"""
        assert lg._pid_start_time("not-a-pid") is None
        assert lg._pid_start_time(DEAD_PID) is None
        assert lg._pid_start_time(os.getpid())
        assert lg._pid_alive(os.getpid()) is True
        assert lg._pid_alive(DEAD_PID) is False
        assert lg._pid_alive("nonsense") is False


# ── the executor-side escalation state (wake-watch design §9) ──────────────────────────────────
class TestEscalation:
    def test_the_decision_tree_covers_every_documented_branch(self, sr):
        """escalation_decision's docstring enumerates resolved / unowned / owner-missing / send."""
        assert lg.escalation_decision(sr, "e1", 1, None) == "unowned"
        assert lg.escalation_decision(sr, "e1", 1, "") == "unowned"
        assert lg.escalation_decision(sr, "e1", 1, "ghost-lead") == "owner-missing"
        armed(sr, "L1")
        assert lg.escalation_decision(sr, "e1", 1, "L1") == "send"
        lg.mark_surfaced(sr, "L1", ["e1:1"])
        assert lg.escalation_decision(sr, "e1", 1, "L1") == "resolved"
        assert lg.escalation_decision(sr, "e1", 2, "L1") == "send"

    def test_any_bad_input_surfaces_to_a_human_rather_than_doing_nothing(self, sr, monkeypatch):
        """'any bad input degrades to "owner-missing" (the safe direction is surfacing to a
        human, never silent inaction).'"""
        def boom(*a, **k):
            raise RuntimeError("disk gone")
        monkeypatch.setattr(lg, "read_marker", boom)
        assert lg.escalation_decision(sr, "e1", 1, "L1") == "owner-missing"

    def test_the_escalation_ledger_round_trips_and_degrades_quietly(self, sr, a_file):
        """load_escalation/save_escalation: '{} if missing/unreadable. Never raises.'"""
        assert lg.load_escalation(sr, "e1") == {}
        lg.save_escalation(sr, "e1", {"1": {"status": "notified"}})
        assert lg.load_escalation(sr, "e1") == {"1": {"status": "notified"}}
        lg._escalation_path(sr, "e1").unlink()
        lg._escalation_path(sr, "e1").mkdir()
        assert lg.load_escalation(sr, "e1") == {}
        lg.save_escalation(a_file, "e1", {})         # unwritable → swallowed

    def test_the_escalation_ledger_is_separate_from_the_leads_surfaced_set(self, sr):
        """'A SEPARATE ledger … by design: the executor's own "I pushed/notified" bookkeeping
        must never be written into the lead's own surfacing ledger.'"""
        armed(sr, "L1")
        lg.save_escalation(sr, "e1", {"1": {"status": "sent"}})
        assert lg.load_surfaced(sr, "L1") == set()
        assert lg.escalation_decision(sr, "e1", 1, "L1") == "send"


# ── peer addressing (#28 phase 1) ──────────────────────────────────────────────────────────────
class TestPeerAddressing:
    def test_a_live_registry_entry_resolves_to_its_socket(self, tmp_path, monkeypatch):
        """peer_address: 'The path is the ADDRESS' — newest live entry wins."""
        reg = tmp_path / "sessions"
        reg.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        (reg / "1.json").write_text(json.dumps(
            {"sessionId": "abc", "pid": os.getpid(), "messagingSocketPath": "/tmp/s.sock",
             "updatedAt": 2}))
        (reg / "2.json").write_text(json.dumps(
            {"sessionId": "abc", "pid": DEAD_PID, "messagingSocketPath": "/tmp/dead.sock",
             "updatedAt": 3}))
        (reg / "junk.json").write_text("{ truncated")
        (reg / "noid.json").write_text(json.dumps({"pid": 1}))
        assert lg.peer_address("abc") == "uds:/tmp/s.sock"

    def test_an_unresolvable_peer_is_none_not_an_exception(self, tmp_path, monkeypatch):
        """'Never throws — an unresolvable address must degrade to the old wake path, never break
        a packet build.'"""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
        assert lg.peer_address("abc") is None
        assert lg.peer_address(None) is None
        assert lg.peer_registry_entries() == []

    def test_a_registry_that_cannot_be_listed_is_empty_not_fatal(self, monkeypatch):
        def boom():
            raise RuntimeError("no home")
        monkeypatch.setattr(lg, "_sessions_registry_dir", boom)
        assert lg.peer_registry_entries() == []
