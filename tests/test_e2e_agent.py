"""
Layer 2 (live, real `claude` + API, not CI-able): proves the executor AGENT against the real CLI,
using the exact flags relay builds (lead_guard.executor_agent_flags → iterm.build_claude_cmd):
  1. the Agent tool is gone from the top-level tool list (no sub-spawning)
  2. the role prompt is applied (the session knows its GATES) and the harness prompt is kept
  3. `git commit` is DENIED even under --dangerously-skip-permissions (CLI --disallowedTools)
  4. the agent is re-applied on `--resume` when relay re-passes the flags (inline agents are not
     restored by resume on their own)
Run: python3 tests/test_e2e_agent.py   (or pytest with RELAY_E2E_AGENT=1). A few small haiku calls.
"""
import json, os, subprocess, sys, tempfile, uuid
from pathlib import Path
import pytest
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts")); sys.path.insert(0, str(REPO_ROOT / "lib"))
import iterm, lead_guard  # noqa: E402
pytestmark = pytest.mark.skipif(not os.environ.get("RELAY_E2E_AGENT"), reason="live test: set RELAY_E2E_AGENT=1")
MODEL = os.environ.get("RELAY_E2E_AGENT_MODEL", "haiku")


def run(prompt, agent_flags, cwd, skip_perms=False, session_uuid=None, resume_id=None):
    cmd = iterm.build_claude_cmd(prompt, model=None if resume_id else MODEL, skip_perms=skip_perms,
                                 session_uuid=session_uuid, resume_id=resume_id, agent_flags=agent_flags)
    cmd += " -p --output-format stream-json --verbose"
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=240, cwd=cwd)
    assert r.returncode == 0, f"claude failed: {r.stderr[-600:]}\n{cmd}"
    init, result = None, ""
    for line in r.stdout.splitlines():
        try: d = json.loads(line)
        except ValueError: continue
        if d.get("type") == "system" and d.get("subtype") == "init": init = d
        if d.get("type") == "result": result = d.get("result") or ""
    return init, result, cmd


def test_executor_agent_live(tmp_path=None):
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp(prefix="relay-e2e-agent-"))
    repo = tmp / "repo"; repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    flags = lead_guard.executor_agent_flags(REPO_ROOT)
    assert flags, "plugin has no agents/executor.md"
    out = {}
    # 1 + 2
    init, res, cmd = run("Reply with exactly two lines. Line 1: GATES=<YES if your instructions contain a GATES section "
                         "telling you to stage and never commit, else NO>. Line 2: HARNESS=<YES if your system prompt also "
                         "contains Claude Code's own instructions about tools such as Bash, else NO>", flags, str(repo))
    out["role"] = (init and sorted(init["tools"]), res, cmd)
    assert "Agent" not in init["tools"], f"Agent tool still present: {init['tools']}"
    assert "Bash" in init["tools"] and "Edit" in init["tools"] and "Read" in init["tools"]
    assert "GATES=YES" in res and "HARNESS=YES" in res, res
    # 3
    init, res, cmd = run("Run exactly this shell command and report in one line whether it succeeded or was blocked: "
                         "git commit --allow-empty -m probe", flags, str(repo), skip_perms=True)
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout.strip()
    out["commit"] = (log, res, cmd)
    assert log == "", f"git commit was NOT denied under skip-perms: {log}\n{res}"
    # 4
    sid = str(uuid.uuid4())
    run("Reply: ok", flags, str(repo), session_uuid=sid)
    init, res, cmd = run("Reply with exactly one line: GATES=<YES if your instructions contain a GATES section about staging "
                         "and never committing, else NO>", flags, str(repo), resume_id=sid)
    out["resume+flags"] = (sorted(init["tools"]), res, cmd)
    assert "Agent" not in init["tools"] and "GATES=YES" in res, res
    return out


if __name__ == "__main__":
    os.environ["RELAY_E2E_AGENT"] = "1"
    for k, (a, res, cmd) in test_executor_agent_live().items():
        print(f"\n== {k}\n   $ {cmd[:200]}…\n   -> {a if k != 'role' else ('tools=' + str(len(a)) + ' (Agent absent)')}\n   -> {res[:200]}")
    print("\nALL LIVE AGENT CHECKS PASSED")
