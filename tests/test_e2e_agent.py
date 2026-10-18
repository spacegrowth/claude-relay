"""
Layer 2 (live, real `claude` + API, not CI-able): proves the executor AGENT against the real CLI,
using the exact flags relay builds (lead_guard.executor_agent_flags → iterm.build_claude_cmd):
  1. the Agent tool is gone from the top-level tool list (no sub-spawning)
  2. the role prompt is applied (the session knows its GATES) and the harness prompt is kept
  3. `git commit` is DENIED even under --dangerously-skip-permissions (CLI --disallowedTools)
  4. the agent is re-applied on `--resume` when relay re-passes the flags (inline agents are not
     restored by resume on their own)
  5. the interactive dialogs (AskUserQuestion, EnterPlanMode, ExitPlanMode) are gone from an
     INTERACTIVE session's tool list. `claude -p` never lists them at all (observed 2026-10-15), so
     this one runs the real REPL on a PRIVATE tmux server (`tmux -L relay-e2e-agent-<pid>`, killed
     after; never the human's server or a tab) with and without the flags — the control run must
     list them, or the check proves nothing. Skipped when tmux is not installed.
Run: python3 tests/test_e2e_agent.py   (or pytest with RELAY_E2E_AGENT=1). A few small haiku calls.
"""
import json, os, shlex, shutil, subprocess, sys, tempfile, time, uuid
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
    # 5
    dialogs = ("AskUserQuestion", "EnterPlanMode", "ExitPlanMode")
    assert not set(dialogs) & set(init["tools"])          # -p: absent either way (see docstring)
    if shutil.which("tmux"):
        with_flags = interactive_tools(flags, str(REPO_ROOT))
        control = interactive_tools([], str(REPO_ROOT))
        out["dialogs"] = (sorted(set(dialogs) & with_flags), f"control lists {sorted(set(dialogs) & control)}",
                          "interactive REPL on a private tmux server")
        assert set(dialogs) <= control, f"control run lists no dialog tools — check is vacuous: {sorted(control)}"
        assert not set(dialogs) & with_flags, f"dialog tools still offered to the executor: {sorted(with_flags)}"
    else:
        out["dialogs"] = ([], "SKIPPED — tmux not installed", "")
    return out


def interactive_tools(agent_flags, cwd):
    """The tool names an INTERACTIVE claude (haiku) says it has, read off a private tmux server's
    pane. `cwd` must already be a trusted folder (the repo) so no trust dialog blocks the REPL."""
    sock = f"relay-e2e-agent-{os.getpid()}"
    prompt = ("Without calling any tool, reply with exactly one line: the word ASK= followed by the exact "
              "names of EVERY tool in your tool list, comma-separated with no spaces, then the word PLAN=END")
    cmd = iterm.build_claude_cmd(prompt, model=MODEL, agent_flags=agent_flags)
    shell = f"cd {shlex.quote(cwd)} && export RELAY_HEADLESS=1 && exec {cmd} --strict-mcp-config"
    T = ["tmux", "-L", sock]
    subprocess.run(T + ["new-session", "-d", "-s", "p", "-x", "220", "-y", "60", shell], check=True)
    try:
        flat = ""
        for _ in range(90):
            time.sleep(2)
            pane = subprocess.run(T + ["capture-pane", "-p", "-t", "p", "-S", "-200"],
                                  capture_output=True, text=True).stdout
            flat = " ".join(ln.strip() for ln in pane.splitlines())
            i = flat.find("\u23fa ASK=")                     # "⏺ ASK=": the reply, not the prompt echo
            if i >= 0 and "PLAN=END" in flat[i:]:
                names = flat[i + len("\u23fa ASK="):flat.index("PLAN=END", i)].replace(" ", "")
                return {n for n in names.split(",") if n}
        raise AssertionError(f"no reply from the interactive session; pane tail: {flat[-400:]}")
    finally:
        subprocess.run(T + ["kill-server"], capture_output=True)


if __name__ == "__main__":
    os.environ["RELAY_E2E_AGENT"] = "1"
    for k, (a, res, cmd) in test_executor_agent_live().items():
        print(f"\n== {k}\n   $ {cmd[:200]}…\n   -> {a if k != 'role' else ('tools=' + str(len(a)) + ' (Agent absent)')}\n   -> {res[:200]}")
    print("\nALL LIVE AGENT CHECKS PASSED")
