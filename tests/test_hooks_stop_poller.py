"""
Bug-hunt suite (part 2): hooks/stop_lead_watch.py — the notification tier chain, the background
report poller, and the per-turn heartbeat.

Split out of tests/test_hooks_stop.py purely for size; the oracle, the drivers and the fixtures are
that file's. Read its module docstring first — in particular the note that every run here sets
`RELAY_NO_NOTIFY=1` and shadows `osascript` with a stub on PATH, so no test can post a real
desktop banner or touch a real iTerm session.

Run: pytest tests/test_hooks_stop_poller.py -q
"""
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

STOP = "stop_lead_watch.py"

# All shared with tests/conftest_hooks.py so the two halves of this suite cannot drift apart.
DRIVERS, DRIVER_IDS = H.DRIVERS, H.DRIVER_IDS
WAKE, SILENT = H.WAKE, H.SILENT
stop_payload = H.stop_payload
armed = H.armed_lead_with_config


def load_stop_module():
    """hooks/stop_lead_watch.py as an importable module, for the two notification tiers that
    cannot be reached from stdin without touching the real machine: tier 1 writes an OSC escape to
    a live iTerm tty, tier 2 shells out to osascript. Both are documented, user-facing behaviour
    (README:435-445), so they are worth pinning — the alternative is leaving the whole fallback
    chain untested."""
    import importlib.util
    path = str(H.HOOKS_DIR / STOP)
    spec = importlib.util.spec_from_file_location("stop_watch_notify_tiers", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestStopHookNotificationTiers:
    """README:435-445 / _notify:44-58 — "two tiers, first one that applies wins"."""

    def test_tier_one_writes_the_osc_notification_to_the_leads_own_tty(self, monkeypatch):
        """"iTerm native (OSC 777, written straight to the lead's own tty) ... clicking it focuses
        the POSTING session natively". When it applies it RETURNS — no second banner behind it."""
        mod = load_stop_module()
        import iterm
        sent, ran = [], []
        monkeypatch.setattr(iterm, "tty_by_id", lambda sid: "/dev/ttys999")
        monkeypatch.setattr(iterm, "notify_via_tty",
                            lambda tty, title, body: sent.append((tty, title, body)))
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: ran.append(a))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True}, "exec-1 reported", project="proj",
                    executor="exec-1", lead_sid="lead-1", iterm_session="w0t1p0:UUID")
        assert sent and sent[0][0] == "/dev/ttys999"
        assert sent[0][1] == "relay · proj"
        assert ran == [], "tier 1 winning must not also fire osascript"

    def test_tier_one_is_skipped_when_notify_via_is_osascript(self, monkeypatch):
        """_notify:70-74 — "a lead who wants a clean banner title opts out of this tier". iTerm
        forces its own "Session …" prefix on the OSC tier, which no escape can override."""
        mod = load_stop_module()
        import iterm
        sent, ran = [], []
        monkeypatch.setattr(iterm, "tty_by_id", lambda sid: sent.append(sid) or "/dev/ttys999")
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: ran.append(a[0]) or SimpleNamespace(returncode=0))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True, "notify_via": "osascript"}, "msg",
                    project="proj", lead_sid="lead-1", iterm_session="w0t1p0:UUID")
        assert sent == [], "tty_by_id must not even be consulted when the tier is opted out of"
        assert ran and ran[0][0] == "osascript"

    def test_tier_one_is_skipped_for_legacy_terminal_notifier_config_value(self, monkeypatch):
        """A pre-drop config still holding notify_via='terminal-notifier' must keep opting out of
        tier 1 exactly like 'osascript' does — no error, no silent revert to the OSC tier."""
        mod = load_stop_module()
        import iterm
        sent, ran = [], []
        monkeypatch.setattr(iterm, "tty_by_id", lambda sid: sent.append(sid) or "/dev/ttys999")
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: ran.append(a[0]) or SimpleNamespace(returncode=0))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True, "notify_via": "terminal-notifier"}, "msg",
                    project="proj", lead_sid="lead-1", iterm_session="w0t1p0:UUID")
        assert sent == []
        assert ran and ran[0][0] == "osascript"

    def test_tier_one_falling_over_drops_through_to_tier_two(self, monkeypatch):
        """"fall through to tier 2 — tty_by_id shells out to osascript, WHICH CAN MISBEHAVE"
        (L82-83). A raising tty lookup must not swallow the notification entirely."""
        mod = load_stop_module()
        import iterm
        ran = []

        def boom(_sid):
            raise RuntimeError("osascript wedged")

        monkeypatch.setattr(iterm, "tty_by_id", boom)
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: ran.append(a[0]) or SimpleNamespace(returncode=0))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True}, "msg", project="proj", lead_sid="lead-1",
                    iterm_session="w0t1p0:UUID")
        assert ran and ran[0][0] == "osascript"

    def test_tier_one_with_no_resolvable_tty_drops_through(self, monkeypatch):
        """A lead whose iTerm session is gone (window closed, iTerm restarted) — tty_by_id returns
        None and the chain continues."""
        mod = load_stop_module()
        import iterm
        ran = []
        monkeypatch.setattr(iterm, "tty_by_id", lambda sid: None)
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: ran.append(a[0]) or SimpleNamespace(returncode=0))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True}, "msg", project="proj", lead_sid="lead-1",
                    iterm_session="w0t1p0:UUID")
        assert ran and ran[0][0] == "osascript"

    def test_tier_two_osascript_fallback_when_no_iterm_session(self, monkeypatch):
        """README:445 — "osascript fallback (tier 1 didn't apply): macOS's built-in `display
        notification`, same info, NOT clickable"."""
        mod = load_stop_module()
        ran = []
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: ran.append(a[0]) or SimpleNamespace(returncode=0))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True}, "exec-1 reported", project="proj",
                    executor="exec-1", lead_sid="lead-1")
        assert ran and ran[0][0] == "osascript"
        script = ran[0][2]
        assert "display notification" in script
        assert 'with title "relay · proj"' in script

    def test_the_osascript_fallback_escapes_quotes_and_backslashes(self, monkeypatch):
        """_notify:97-98's `q()` — an unescaped quote in a project or report brief would produce a
        malformed AppleScript, i.e. a silently lost notification."""
        mod = load_stop_module()
        ran = []
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: ran.append(a[0]) or SimpleNamespace(returncode=0))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True}, 'a "quoted" brief with a \\ backslash',
                    project='proj "x"', lead_sid="lead-1")
        script = ran[0][2]
        assert '\\"quoted\\"' in script
        assert "\\\\ backslash" in script

    def test_a_notifier_that_raises_never_reaches_the_caller(self, monkeypatch):
        """"failures swallowed throughout" (L58) — the wake must not be lost because a banner was."""
        mod = load_stop_module()

        def boom(*a, **k):
            raise OSError("no such binary")

        monkeypatch.setattr(mod.subprocess, "run", boom)
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True}, "msg", project="proj", lead_sid="lead-1")

    def test_the_default_subtitle_names_the_reporting_executor(self, monkeypatch):
        """_notify:68-69 — the subtitle is `"<executor> reported"`, so a banner with several leads
        running still says WHOSE executor finished."""
        mod = load_stop_module()
        ran = []
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: ran.append(a[0]) or SimpleNamespace(returncode=0))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True}, "msg", project="proj", executor="exec-7",
                    lead_sid="lead-1")
        assert "exec-7 reported" in ran[0][2]

    def test_a_notification_with_no_project_still_says_what_it_is(self, monkeypatch):
        mod = load_stop_module()
        ran = []
        monkeypatch.setattr(mod.subprocess, "run",
                            lambda *a, **k: ran.append(a[0]) or SimpleNamespace(returncode=0))
        monkeypatch.delenv("RELAY_NO_NOTIFY", raising=False)
        mod._notify({"notify_on_wake": True}, "msg")
        assert "relay — review needed" in ran[0][2]

    def test_notify_summary_falls_back_when_every_line_is_empty(self):
        """_notify_summary:201-207 — the banner must never be blank."""
        mod = load_stop_module()
        assert mod._notify_summary([]) == "new relay activity — review when ready"
        assert mod._notify_summary(["  ✅ ", " "]) == "new relay activity — review when ready"

    def test_report_brief_survives_an_unreadable_report(self, tmp_path):
        """_report_brief:174-185 — "Best-effort; empty on any error". A report path that is a
        DIRECTORY (or missing) must yield "", not an exception inside the wake path."""
        mod = load_stop_module()
        d = tmp_path / "001-report.md"
        d.mkdir()
        assert mod._report_brief(str(d)) == ""
        assert mod._report_brief(str(tmp_path / "nope.md")) == ""


# =================================================================================================
# App 1 slow path — the background poller
# =================================================================================================

class TestStopHookBackgroundPoller:
    """stop_lead_watch:6-10 — "when the lead stops with a busy executor still in flight, it watches
    the report paths and exits 2 the moment one lands. (A one-shot check at stop time would miss a
    later report and never fire again, since an idle session emits no further Stop events.)" """

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_report_landing_mid_poll_wakes_the_lead_either_way(self, drv, tmp_path):
        """The same case as below, run through BOTH drivers so the poller loop itself is measured
        as well as proven in a real process."""
        armed(tmp_path, poll_seconds=25, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")
        report = H.state_root(tmp_path) / "exec-1" / "packets" / "001-report.md"

        def land():
            time.sleep(2.5)
            report.write_text("landed mid-poll\n")

        t = threading.Thread(target=land, daemon=True)
        t.start()
        run = drv(STOP, stop_payload(tmp_path), tmp_path)
        t.join(timeout=5)
        assert run.returncode == WAKE
        assert "landed mid-poll" in run.stderr

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_async_announce_is_recorded_as_async(self, drv, tmp_path):
        """_announce_and_wake's `kind="async"` (L340-343) — "THIS exit-2 is the droppable one (a
        stale poller firing while the lead is mid-turn), so its claim is only ever honoured against
        transcript evidence that the wake really landed — never trusted on faith." The claim the
        poller writes must therefore say `async`, not `sync`."""
        root = armed(tmp_path, poll_seconds=25, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")
        report = root / "exec-1" / "packets" / "001-report.md"

        def land():
            time.sleep(2.5)
            report.write_text("late\n")

        t = threading.Thread(target=land, daemon=True)
        t.start()
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == WAKE
        t.join(timeout=5)
        assert lg.load_announce_claim(root, "lead-1")["kind"] == "async"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_an_unprovable_async_claim_is_not_treated_as_delivered(self, drv, tmp_path):
        """relay_announce_delivered:1003-1011 — "An unprovable "async" claim reads as NOT
        delivered, which merely costs a retry (capped) — the safe side of the incident." (A `sync`
        claim with no transcript IS trusted; this is the asymmetry that matters.)"""
        root = armed(tmp_path, poll_seconds=25, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")
        report = root / "exec-1" / "packets" / "001-report.md"

        def land():
            time.sleep(2.5)
            report.write_text("late\n")

        t = threading.Thread(target=land, daemon=True)
        t.start()
        drv(STOP, stop_payload(tmp_path), tmp_path)
        t.join(timeout=5)
        assert lg.load_pending(root, "lead-1")["exec-1:1"]["announces"] == 1
        drv(STOP, stop_payload(tmp_path, stop_hook_active=True), tmp_path)
        assert lg.load_surfaced(root, "lead-1") == set(), \
            "an async claim with nothing to prove it must NOT promote to surfaced"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_poller_stops_when_the_lead_steps_down(self, drv, tmp_path):
        """stop_lead_watch:335-336, through both drivers — a poller that outlived its lead would
        wake a session that is no longer armed."""
        armed(tmp_path, poll_seconds=25, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")
        root = H.state_root(tmp_path)

        def step_down():
            time.sleep(2)
            lg.clear_lead(root, "lead-1")

        t = threading.Thread(target=step_down, daemon=True)
        t.start()
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT
        t.join(timeout=5)

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_poller_stops_when_the_last_executor_leaves_flight(self, drv, tmp_path):
        """stop_lead_watch:344-345, through both drivers."""
        armed(tmp_path, poll_seconds=25, poll_interval=1)
        d = H.make_executor(tmp_path, report=None, status="busy")

        def close_it():
            time.sleep(2)
            s = json.loads((d / "session.json").read_text())
            s["status"] = "closed"
            (d / "session.json").write_text(json.dumps(s))

        t = threading.Thread(target=close_it, daemon=True)
        t.start()
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT
        t.join(timeout=5)

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_held_lock_makes_a_second_stop_return_at_once(self, drv, tmp_path):
        """stop_lead_watch:328-329 — "a poller is already watching". Driven without a second
        process by planting a LIVE lock held by this test's own (definitely alive) pid."""
        root = armed(tmp_path, poll_seconds=25, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")
        lock = lg._lock_path(root, "lead-1")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid(), "ts": time.time(),
                                    "pid_started": lg._pid_start_time(os.getpid())}))
        assert lg.poll_lock_state(root, "lead-1", 1) == "live"
        started = time.time()
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT
        assert time.time() - started < 5
        assert lg.poll_lock_state(root, "lead-1", 1) == "live", \
            "a Stop that did not acquire the lock must not release someone else's"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_stale_lock_is_reclaimed_rather_than_blocking_forever(self, drv, tmp_path):
        """_poll_lock_status:1274-1296 — a lock whose holder is dead is "stale ... reclaimable
        rather than a permanent block". Otherwise one crashed poller would disable the wake for
        the life of the lead."""
        root = armed(tmp_path, poll_seconds=2, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")
        lock = lg._lock_path(root, "lead-1")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": 999999, "ts": time.time(), "pid_started": "x"}))
        assert lg.poll_lock_state(root, "lead-1", 1) == "stale"
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT
        assert lg.poll_lock_state(root, "lead-1", 1) == "absent", "the reclaimed lock was released"

    def test_a_report_landing_mid_poll_wakes_the_lead(self, tmp_path):
        armed(tmp_path, poll_seconds=25, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")   # in flight, nothing written yet
        report = H.state_root(tmp_path) / "exec-1" / "packets" / "001-report.md"

        def land():
            time.sleep(2.5)
            report.write_text("landed while the lead was idle\n")

        t = threading.Thread(target=land, daemon=True)
        t.start()
        started = time.time()
        run = H.run_hook(STOP, stop_payload(tmp_path), tmp_path, timeout=40)
        t.join(timeout=5)
        assert run.returncode == WAKE
        assert "landed while the lead was idle" in run.stderr
        assert time.time() - started < 20, "the poller must fire on the report, not on its deadline"

    def test_the_poller_is_not_armed_with_nothing_in_flight(self, tmp_path):
        """stop_lead_watch:325 — with no busy/stalled executor of OURS there is nothing to wait
        on, so the hook returns immediately instead of sitting for poll_seconds."""
        armed(tmp_path, poll_seconds=30, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="reported")
        started = time.time()
        assert H.run_hook(STOP, stop_payload(tmp_path), tmp_path, timeout=20).returncode == SILENT
        assert time.time() - started < 10

    def test_another_leads_busy_executor_does_not_arm_the_poller(self, tmp_path):
        """has_inflight_executors:1154-1187 — "a lead never idles waiting on another lead's
        executor OR an unowned (bare/legacy) one"."""
        armed(tmp_path, poll_seconds=30, poll_interval=1)
        H.make_executor(tmp_path, "exec-other", report=None, status="busy", owner_lead="lead-2")
        H.make_executor(tmp_path, "exec-bare", report=None, status="busy", owner_lead=None)
        started = time.time()
        assert H.run_hook(STOP, stop_payload(tmp_path), tmp_path, timeout=20).returncode == SILENT
        assert time.time() - started < 10

    def test_a_stalled_executor_still_arms_the_poller(self, tmp_path):
        """has_inflight_executors — "`stalled` counts as in-flight (wake-watch design §6): a
        long-but-alive executor is the MOST likely to report while the lead idles"."""
        armed(tmp_path, poll_seconds=25, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="stalled")
        report = H.state_root(tmp_path) / "exec-1" / "packets" / "001-report.md"

        def land():
            time.sleep(2.5)
            report.write_text("the stalled one finished after all\n")

        t = threading.Thread(target=land, daemon=True)
        t.start()
        run = H.run_hook(STOP, stop_payload(tmp_path), tmp_path, timeout=40)
        t.join(timeout=5)
        assert run.returncode == WAKE
        assert "the stalled one finished" in run.stderr

    def test_the_poller_stops_when_nothing_of_ours_is_left_in_flight(self, tmp_path):
        """stop_lead_watch:344-345 — "nothing left of OURS in flight → stop waiting", well before
        the deadline."""
        armed(tmp_path, poll_seconds=30, poll_interval=1)
        d = H.make_executor(tmp_path, report=None, status="busy")

        def close_it():
            time.sleep(2)
            s = json.loads((d / "session.json").read_text())
            s["status"] = "closed"
            (d / "session.json").write_text(json.dumps(s))

        t = threading.Thread(target=close_it, daemon=True)
        t.start()
        started = time.time()
        assert H.run_hook(STOP, stop_payload(tmp_path), tmp_path, timeout=40).returncode == SILENT
        t.join(timeout=5)
        assert time.time() - started < 20

    def test_the_poller_exits_when_the_lead_steps_down_mid_wait(self, tmp_path):
        """stop_lead_watch:335-336 — "lead stepped down / session ended while we waited". A poller
        that outlived its lead would wake a session that is no longer armed."""
        armed(tmp_path, poll_seconds=30, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")
        root = H.state_root(tmp_path)

        def step_down():
            time.sleep(2)
            lg.clear_lead(root, "lead-1")

        t = threading.Thread(target=step_down, daemon=True)
        t.start()
        started = time.time()
        assert H.run_hook(STOP, stop_payload(tmp_path), tmp_path, timeout=40).returncode == SILENT
        t.join(timeout=5)
        assert time.time() - started < 20

    def test_the_poller_times_out_silently(self, tmp_path):
        """"timed out; a later lead turn will re-arm" (L346) — a deadline is exit 0, not a wake."""
        armed(tmp_path, poll_seconds=2, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")
        run = H.run_hook(STOP, stop_payload(tmp_path), tmp_path, timeout=40)
        assert run.returncode == SILENT and run.stderr == ""

    def test_only_one_poller_per_lead(self, tmp_path):
        """"One poller per lead (lock); a later Stop while it runs just exits 0" (L323-324). The
        second Stop must return AT ONCE, not queue behind the first."""
        armed(tmp_path, poll_seconds=8, poll_interval=1)
        H.make_executor(tmp_path, report=None, status="busy")
        bin_dir, log = H.stub_bin(tmp_path)
        first = subprocess.Popen(
            [sys.executable, str(H.HOOKS_DIR / STOP)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=H._hook_env(tmp_path, log, bin_dir))
        try:
            first.stdin.write(json.dumps(stop_payload(tmp_path)))
            first.stdin.close()
            deadline = time.time() + 8
            while time.time() < deadline:            # wait until the lock is genuinely held
                if lg.poll_lock_state(H.state_root(tmp_path), "lead-1", 1) == "live":
                    break
                time.sleep(0.2)
            assert lg.poll_lock_state(H.state_root(tmp_path), "lead-1", 1) == "live"
            started = time.time()
            second = H.run_hook(STOP, stop_payload(tmp_path), tmp_path, timeout=20)
            assert second.returncode == SILENT
            assert time.time() - started < 5, "the second Stop must not queue behind the poller"
        finally:
            first.wait(timeout=30)
        assert first.returncode == SILENT
        assert lg.poll_lock_state(H.state_root(tmp_path), "lead-1", 1) == "absent", \
            "the poller must release its lock on the way out (stop_lead_watch:347-348)"


# =================================================================================================
# Heartbeat / version re-stamp
# =================================================================================================

class TestStopHookHeartbeat:
    """stop_lead_watch:221-227 — "every lead turn refreshes last_active (and re-stamps
    plugin_version/stop_hook_timeout from THIS hook's own plugin root) so `relay list` reflects
    real liveness and the current version"."""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_a_silent_stop_still_refreshes_last_active(self, drv, tmp_path):
        root = armed(tmp_path)
        lg.update_marker(root, "lead-1", last_active="2020-01-01T00:00:00")
        assert drv(STOP, stop_payload(tmp_path), tmp_path).returncode == SILENT
        assert lg.read_marker(root, "lead-1")["last_active"] != "2020-01-01T00:00:00"

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_version_stamp_is_refreshed_from_the_live_plugin_root(self, drv, tmp_path):
        """"This kills the stale-VER-until-re-arm gap" (touch_lead:576-590): a lead armed under an
        older version picks up the live one at its next turn, without re-arming."""
        root = armed(tmp_path)
        lg.update_marker(root, "lead-1", plugin_version="0.0.1-ancient", stop_hook_timeout=1)
        drv(STOP, stop_payload(tmp_path), tmp_path)
        m = lg.read_marker(root, "lead-1")
        live = json.loads((H.REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
        manifest = json.loads((H.REPO_ROOT / "hooks" / "hooks.json").read_text())
        assert m["plugin_version"] == live
        assert m["stop_hook_timeout"] == manifest["hooks"]["Stop"][0]["hooks"][0]["timeout"]

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_the_heartbeat_preserves_every_other_marker_field(self, drv, tmp_path):
        """touch_lead is a read-modify-write, not a rewrite — project/cwd/colour must survive."""
        root = H.arm_lead(tmp_path, project="proj", cwd="/somewhere", iterm_session="w0t0p0",
                          color=(1, 2, 3))
        H.write_config(tmp_path)
        before = lg.read_marker(root, "lead-1")
        drv(STOP, stop_payload(tmp_path), tmp_path)
        after = lg.read_marker(root, "lead-1")
        for k in ("project", "cwd", "iterm_session", "color", "session_id"):
            assert after.get(k) == before.get(k), k
