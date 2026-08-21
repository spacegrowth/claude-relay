"""
Layer 2 (live, real `claude` binary + API, not CI-able) end-to-end proof of the executor MCP
policy (lead_guard "executor MCP policy"): launches REAL headless claude sessions with the EXACT
command string relay builds (iterm.build_claude_cmd + lead_guard.mcp_cli_flags, run through the
shell so quoting is exercised too) and reads, from each process's `system/init` stream-json event,
which MCP servers it ACTUALLY loaded.

Proves, against the real CLI rather than mocks:
  1. "none"     → the process loads NO MCP servers (claude.ai connectors included)
  2. "inherit"  → the machine's normal set loads
  3. allowlist  → ONLY the named server loads; nothing else leaks in
  4. resume     → `claude --resume` does NOT remember MCP flags: resuming the "none" conversation
                  with no flags brings MCPs back, re-passing the flags keeps it at none — i.e.
                  bin/relay's _relaunch MUST re-apply mcp_flags (it does).

Run manually (costs a handful of small haiku calls; needs a logged-in claude):
    python3 tests/test_e2e_mcp.py
Or via pytest with RELAY_E2E_MCP=1. Not part of the default suite.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "lib"))
import iterm        # noqa: E402
import lead_guard   # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("RELAY_E2E_MCP"),
                                reason="live test: set RELAY_E2E_MCP=1 (real claude + API calls)")

PROBE = "Reply with exactly: ok"
MODEL = os.environ.get("RELAY_E2E_MCP_MODEL", "haiku")


def run_claude(mcp_flags, session_uuid=None, resume_id=None, cwd=None):
    """Run the exact relay-built command headless (stream-json) and return the set of MCP server
    names the PROCESS actually loaded — read from the `system/init` event's `mcp_servers`, not from
    the model's self-report (unreliable: MCP tools register after init / are deferred, and a small
    model answering 'which tools do you have' got it wrong in a first cut of this test)."""
    cmd = iterm.build_claude_cmd(PROBE, model=None if resume_id else MODEL, session_uuid=session_uuid,
                                 resume_id=resume_id, mcp_flags=mcp_flags)
    cmd += " -p --output-format stream-json --verbose"
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180, cwd=cwd)
    assert r.returncode == 0, f"claude failed ({r.returncode}): {r.stderr[-800:]}\ncmd: {cmd}"
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "system" and d.get("subtype") == "init":
            return sorted(s["name"] for s in d.get("mcp_servers") or []), cmd
    raise AssertionError(f"no system/init event in stream-json output\ncmd: {cmd}\n{r.stdout[:500]}")


def test_none_inherit_allowlist_and_resume(tmp_path=None):
    tmp = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp(prefix="relay-e2e-mcp-"))
    cwd = str(tmp)  # a neutral cwd: no project-level .mcp.json, so "inherit" = user-level + connectors
    results = {}

    # 1. none → the process loads NO MCP servers (connectors/plugin MCPs included)
    got, cmd = run_claude(lead_guard.mcp_cli_flags("none"), cwd=cwd)
    results["none"] = (got, cmd)
    assert got == [], f"'none' still loaded MCP servers: {got}\n{cmd}"

    # 2. inherit → whatever this machine normally loads (must be non-empty to make the test mean anything)
    inherited, cmd = run_claude(lead_guard.mcp_cli_flags("inherit"), cwd=cwd)
    results["inherit"] = (inherited, cmd)
    assert inherited, "'inherit' loaded no MCP servers — this machine has none configured, test is vacuous"

    # 3. allowlist → exactly the named file-configured server, nothing else (no connectors, no plugins)
    known = lead_guard.known_mcp_servers(cwd=cwd)
    if known:
        name = sorted(known)[0]
        flags = lead_guard.mcp_cli_flags([name], state_root=tmp / ".relay-tasks", exec_name="e2e", cwd=cwd)
        got, cmd = run_claude(flags, cwd=cwd)
        results[f"allowlist[{name}]"] = (got, cmd)
        assert got == [name], f"allowlist [{name}] loaded {got} — leak or miss\n{cmd}"
        assert set(got) < set(inherited), "allowlist must be a strict subset of inherit"
    else:
        results["allowlist"] = ("SKIPPED — no file-configured MCP servers on this machine", "")

    # 4. resume does NOT restore MCP flags (docs: sessions.md "pass them again when you resume") —
    #    so bin/relay's _relaunch re-passing mcp_flags is REQUIRED, not redundant.
    sid = str(uuid.uuid4())
    got, cmd = run_claude(lead_guard.mcp_cli_flags("none"), session_uuid=sid, cwd=cwd)
    results["resume/seed(none)"] = (got, cmd)
    assert got == []
    got, cmd = run_claude(lead_guard.mcp_cli_flags("none"), resume_id=sid, cwd=cwd)
    results["resume+none-flags"] = (got, cmd)
    assert got == [], f"resume with flags re-passed should stay at none: {got}"
    got, cmd = run_claude(lead_guard.mcp_cli_flags("inherit"), resume_id=sid, cwd=cwd)
    results["resume+no-flags"] = (got, cmd)
    assert got, ("resume WITHOUT flags kept MCPs off — the CLI now persists --mcp-config across "
                 "resume; relay's re-pass is then redundant (harmless) but update docs/comments")
    assert sorted(got) == sorted(inherited), f"resume without flags should equal inherit: {got} vs {inherited}"
    return results


if __name__ == "__main__":
    os.environ["RELAY_E2E_MCP"] = "1"
    res = test_none_inherit_allowlist_and_resume()
    for k, (got, cmd) in res.items():
        print(f"\n== {k}\n   $ {cmd}\n   -> mcp_servers loaded: {got}")
    print("\nALL LIVE MCP CHECKS PASSED")
