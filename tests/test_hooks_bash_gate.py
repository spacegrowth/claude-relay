"""
hooks/pretool_bash_gate.py — the Bash WRITE vector of the routing gate (backlog row 59), driven
through both hook drivers with a tmp HOME and a real tmp git repo as the session cwd.

The verb-log half of the hook (task d1) is covered by tests/test_hooks_pretool.py.
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

BASH = "pretool_bash_gate.py"
DRIVERS, DRIVER_IDS = H.DRIVERS, H.DRIVER_IDS


def body(n):
    return "\n".join("line %d" % i for i in range(n))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "bin").mkdir(parents=True)
    (r / "bin" / "relay").write_text("#!/bin/sh\n")
    (r / "lib").mkdir()
    (r / "lib" / "tracked.py").write_text("x\n")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    return r


def payload(command, cwd, sid="lead-1"):
    return {"session_id": sid, "tool_name": "Bash", "cwd": str(cwd),
            "tool_input": {"command": command}}


def denied(run):
    if not run.stdout.strip():
        return None
    out = json.loads(run.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def bash_records(home):
    return [r for r in H.ledger(home)
            if r["event"] == "would_have_blocked" and r.get("vector") == "bash"]


BIG_HEREDOC = "cat > bin/relay <<EOF\n%s\nEOF" % body(60)


class TestModes:

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_default_is_log(self, drv, tmp_path, repo):
        H.arm_lead(tmp_path)
        run = drv(BASH, payload(BIG_HEREDOC, repo), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""
        recs = bash_records(tmp_path)
        assert len(recs) == 1
        r = recs[0]
        assert r["file_path"].endswith("bin/relay") and r["lines"] == 60
        assert r["new_file"] is False and r["session_id"] == "lead-1"
        assert r["command"] == BIG_HEREDOC and r["rule"] == "heredoc"
        # one record per command: the verb log does not duplicate it
        assert len([x for x in H.ledger(tmp_path) if x["event"] == "would_have_blocked"]) == 1

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_deny_mode_denies_with_edit_gate_shape(self, drv, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        run = drv(BASH, payload(BIG_HEREDOC, repo), tmp_path)
        assert run.returncode == 0
        reason = denied(run)
        assert reason and "bin/relay" in reason and "60 lines" in reason
        assert "/relay:route retain" in reason and "/relay:spawn" in reason
        blocked = [r for r in H.ledger(tmp_path) if r["event"] == "blocked"]
        assert len(blocked) == 1 and blocked[0]["lines"] == 60
        assert set(blocked[0]) == {"ts", "event", "session_id", "file_path", "lines", "new_file",
                                   "vector"}
        assert blocked[0]["vector"] == "bash"
        assert bash_records(tmp_path) == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_off_mode_skips(self, drv, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="off")
        run = drv(BASH, payload(BIG_HEREDOC, repo), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""
        assert bash_records(tmp_path) == []
        # the independent verb log still runs
        assert [r["rule"] for r in H.ledger(tmp_path)] == ["heredoc"]

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_unknown_mode_value_falls_back_to_log(self, drv, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="DENY!!")
        run = drv(BASH, payload(BIG_HEREDOC, repo), tmp_path)
        assert run.stdout.strip() == "" and len(bash_records(tmp_path)) == 1

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_log_mode_survives_verb_log_kill_switch(self, drv, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_gate_logging=False)
        drv(BASH, payload(BIG_HEREDOC, repo), tmp_path)
        assert len(bash_records(tmp_path)) == 1


class TestRule:

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("cmd,target", [
        (BIG_HEREDOC, "bin/relay"),
        ("cat <<EOF | tee lib/tracked.py\n%s\nEOF" % body(45), "lib/tracked.py"),
        ("echo x > lib/new.py", "lib/new.py"),
        ("sed -i s/x/y/ lib/new2.py", "lib/new2.py"),
        ("cp lib/tracked.py lib/copy.py", "lib/copy.py"),
        ("mv lib/tracked.py lib/renamed.py", "lib/renamed.py"),
        ("python3 - <<PY\nopen('lib/gen.py', 'w').write('x')\nPY", "lib/gen.py"),
        ("python3 -c \"open('lib/gen2.py','w')\"", "lib/gen2.py"),
    ])
    def test_each_write_shape_is_denied(self, drv, cmd, target, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        reason = denied(drv(BASH, payload(cmd, repo), tmp_path))
        assert reason and target in reason, cmd

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    @pytest.mark.parametrize("cmd", [
        "cat > bin/relay <<EOF\n%s\nEOF" % body(39),         # under threshold
        "sed -i s/x/y/ lib/tracked.py",                     # unknown count, existing file
        "echo x >> lib/tracked.py",                         # unknown count, existing file
        "cat > lib/001-packet.md <<EOF\n%s\nEOF" % body(99),  # packet exemption
        "mkdir -p _staging && echo x > _staging/n.md",      # _staging exemption
        "echo x > /tmp/elsewhere-new.txt",                  # outside cwd
        "echo x > untracked/new.txt",                       # new file under an untracked dir
        "echo x > $TARGET",                                 # unresolvable
        "ls -la && git status",                             # no write at all
    ])
    def test_allowed(self, drv, cmd, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        run = drv(BASH, payload(cmd, repo), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == "", cmd
        assert [r for r in H.ledger(tmp_path) if r["event"] == "blocked"] == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_relay_tasks_exempt(self, drv, tmp_path, repo):
        """A write into ~/.relay-tasks is exempt even when that dir sits inside the cwd."""
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        (tmp_path / "x.txt").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "x.txt"], check=True)
        cmd = "cat > .relay-tasks/notes-new.md <<EOF\n%s\nEOF" % body(80)
        run = drv(BASH, payload(cmd, tmp_path), tmp_path)
        assert run.stdout.strip() == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_threshold_config_is_honoured(self, drv, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny", edit_line_threshold=5)
        cmd = "cat > bin/relay <<EOF\n%s\nEOF" % body(6)
        assert denied(drv(BASH, payload(cmd, repo), tmp_path))


class TestZeroImpactAndFailOpen:

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_grace_window_allows(self, drv, tmp_path, repo):
        root = H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        lg.set_grace(root, "lead-1", 120)
        run = drv(BASH, payload(BIG_HEREDOC, repo), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""
        assert [r for r in H.ledger(tmp_path) if r["event"] == "blocked"] == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_expired_grace_denies_again(self, drv, tmp_path, repo):
        root = H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        lg.set_grace(root, "lead-1", -1)
        assert denied(drv(BASH, payload(BIG_HEREDOC, repo), tmp_path))

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_non_lead_session_allows(self, drv, tmp_path, repo):
        H.write_config(tmp_path, bash_write_gate="deny")
        run = drv(BASH, payload(BIG_HEREDOC, repo, sid="stranger"), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""
        assert H.ledger(tmp_path) == []

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_executor_session_allows(self, drv, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        H.make_executor(tmp_path, sid="exec-1", report=None)
        run = drv(BASH, payload(BIG_HEREDOC, repo, sid="exec-1"), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_unparseable_command_allows(self, drv, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        for cmd in ("cat > 'unterminated <<EOF\n%s" % body(80), "\x00>\x00", "> > > >"):
            run = drv(BASH, payload(cmd, repo), tmp_path)
            assert run.returncode == 0 and run.stdout.strip() == "", cmd

    def test_parser_exception_allows(self, tmp_path, repo, monkeypatch):
        import bash_writes

        def boom(*a, **k):
            raise RuntimeError("parser exploded")
        monkeypatch.setattr(bash_writes, "gated_targets", boom)
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        run = H.run_hook_inproc(BASH, payload(BIG_HEREDOC, repo), tmp_path)
        assert run.returncode == 0 and run.stdout.strip() == ""

    @pytest.mark.parametrize("drv", DRIVERS, ids=DRIVER_IDS)
    def test_missing_cwd_falls_back_without_error(self, drv, tmp_path, repo):
        H.arm_lead(tmp_path)
        H.write_config(tmp_path, bash_write_gate="deny")
        p = payload(BIG_HEREDOC, repo)
        del p["cwd"]
        assert drv(BASH, p, tmp_path).returncode == 0
