"""
Shared harness for the hook bug-hunt suites (tests/test_hooks_*.py).

NOT a pytest `conftest.py` — it is imported explicitly by each test module (the name is
deliberately different so pytest does not auto-load it as a plugin). It gives every hook suite the
same two ways to drive a hook, plus the state-builders they all need.

Two drivers, on purpose:

  `run_hook`        — a REAL subprocess, exactly as Claude Code launches a hook: the script path,
                      the JSON payload on stdin, a tmp `HOME` so `os.path.expanduser("~")` resolves
                      to an isolated `~/.relay-tasks`, `RELAY_NO_NOTIFY=1`, and a stub bin dir
                      FIRST on PATH shadowing `terminal-notifier`/`osascript`/`open` so a
                      notification can never reach the real desktop. This is the fixture the
                      contract actually describes ("read the hook payload from stdin ... exit 0"),
                      and it is what the fail-open cases must be proven through — only a real
                      process can show that an unparseable payload exits 0 rather than tracebacks.

  `run_hook_inproc` — the SAME script, loaded as a fresh module in this process (unique module name
                      each call, so its module-level `STATE_ROOT = ~/.relay-tasks` is recomputed
                      from the patched `HOME` rather than reused). `sys.stdin`/`sys.argv`/`os.environ`
                      are patched, `SystemExit` is caught, stdout/stderr are captured. Nothing is
                      stubbed that the subprocess driver does not also stub, so the two agree; this
                      one exists because a subprocess is invisible to `coverage`.

Both return the same `HookRun(returncode, stdout, stderr)` triple, so a test can be written once
and pointed at either.

Nothing here may touch the real `~/.relay-tasks`, iTerm, or a live `claude`.
"""
import importlib.util
import io
import itertools
import json
import os
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"

sys.path.insert(0, str(REPO_ROOT / "lib"))
import lead_guard as lg  # noqa: E402

HookRun = namedtuple("HookRun", "returncode stdout stderr")

_counter = itertools.count()

# Every suite parametrises its behaviour tests over BOTH drivers: the subprocess one IS the
# contract (Claude Code launches a process), the in-process one is the same script with `coverage`
# able to see it. Running both is what keeps the cheap driver honest.
DRIVERS = [None, None]      # filled in below, once both functions are defined
DRIVER_IDS = ["subprocess", "inproc"]

# Stdin bodies that must all produce "exit 0, do nothing" — the HARD RULE every hook's docstring
# states. Shared so a shape added here is immediately exercised against all six hooks.
MALFORMED_STDIN = {
    "empty": "",
    "whitespace": "   \n",
    "not-json": "not json at all {",
    "truncated-json": '{"session_id": "lead-1", "tool_nam',
    "json-array": "[1, 2, 3]",
    "json-null": "null",
    "json-string": '"just a string"',
    "json-number": "42",
    "empty-object": "{}",
    "nul-bytes": "\x00\x00\x00",
}

# Every hook the manifest (or a spawn-time --settings file) can launch.
ALL_HOOKS = (
    "pretool_route_guard.py",
    "pretool_bash_gate.py",
    "stop_lead_watch.py",
    "sessionstart_lead_rearm.py",
    "sessionend_lead_cleanup.py",
    "executor_escalation.py",
)


# ---- stub bin dir ------------------------------------------------------------------------------

_STUB = """#!/bin/sh
printf '%s\\n' "$0 $*" >> "$RELAY_TEST_STUB_LOG"
exit 0
"""


def stub_bin(tmp_path):
    """A bin dir that shadows every external notifier this suite could otherwise fire for real.

    `lead_guard.find_terminal_notifier` probes PATH first, so a stub named `terminal-notifier`
    here wins over a real Homebrew install; `osascript` and `open` are shadowed the same way. Each
    invocation appends its argv to `$RELAY_TEST_STUB_LOG`, so a test can assert a notification was
    or was not attempted instead of just hoping. Returns (bin_dir, log_path)."""
    tmp_path = Path(tmp_path)
    d = tmp_path / "stubbin"
    d.mkdir(exist_ok=True)
    log = tmp_path / "stub-calls.log"
    for name in ("terminal-notifier", "osascript", "open", "tmux"):
        p = d / name
        p.write_text(_STUB)
        p.chmod(0o755)
    return d, log


def stub_calls(log):
    try:
        return [ln for ln in Path(log).read_text().splitlines() if ln.strip()]
    except Exception:
        return []


def _hook_env(home, log, bin_dir, extra=None, no_notify=True):
    env = {
        "HOME": str(home),
        "PATH": "%s:%s" % (bin_dir, os.environ.get("PATH", "")),
        "RELAY_TEST_STUB_LOG": str(log),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        # A hook inherits a minimal shell; keep just enough for python3/git to work.
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    if no_notify:
        env["RELAY_NO_NOTIFY"] = "1"
    if extra:
        env.update(extra)
    return env


# ---- driver 1: real subprocess (the contract's own shape) ---------------------------------------

def run_hook(hook, payload, home, *, argv=(), env_extra=None, timeout=60, raw=None,
             no_notify=True, stub=None):
    """Run `hooks/<hook>` as Claude Code does: a separate process, JSON on stdin, tmp HOME.

    `raw` sends a byte-exact stdin body instead of `json.dumps(payload)` (for the malformed-payload
    cases). `argv` becomes the hook's extra argv, which is how the executor escalation hook learns
    its relay name. Returns HookRun."""
    home = Path(home)
    bin_dir, log = stub if stub else stub_bin(home)
    body = raw if raw is not None else json.dumps(payload)
    p = subprocess.run(
        [sys.executable, str(HOOKS_DIR / hook), *[str(a) for a in argv]],
        input=body, capture_output=True, text=True, timeout=timeout,
        env=_hook_env(home, log, bin_dir, env_extra, no_notify))
    return HookRun(p.returncode, p.stdout, p.stderr)


# ---- driver 2: same script, in-process (so `coverage` can see it) --------------------------------

def run_hook_inproc(hook, payload, home, *, argv=(), env_extra=None, raw=None, no_notify=True,
                    stub=None):
    """Load `hooks/<hook>` as a FRESH module and run its `main()` under the same conditions the
    subprocess driver sets up. A unique module name per call forces re-execution, so the script's
    module-level `STATE_ROOT = os.path.join(os.path.expanduser("~"), ".relay-tasks")` is recomputed
    against the patched HOME — nothing about the hook's own state resolution is bypassed."""
    home = Path(home)
    bin_dir, log = stub if stub else stub_bin(home)
    body = raw if raw is not None else json.dumps(payload)

    # Built BEFORE os.environ is cleared — _hook_env reads the real PATH to build the hook's one
    # (a cleared PATH would leave `#!/usr/bin/env python3` scripts like bin/relay unrunnable).
    new_env = _hook_env(home, log, bin_dir, env_extra, no_notify)
    old_env = dict(os.environ)
    old_argv, old_stdin = sys.argv, sys.stdin
    old_out, old_err = sys.stdout, sys.stderr
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    try:
        os.environ.clear()
        os.environ.update(new_env)
        sys.argv = [str(HOOKS_DIR / hook), *[str(a) for a in argv]]
        sys.stdin = io.StringIO(body)
        sys.stdout, sys.stderr = out, err
        name = "hooktest_%s_%d" % (hook.replace(".py", ""), next(_counter))
        spec = importlib.util.spec_from_file_location(name, str(HOOKS_DIR / hook))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            mod.main()
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv, sys.stdin = old_argv, old_stdin
        sys.stdout, sys.stderr = old_out, old_err
        os.environ.clear()
        os.environ.update(old_env)
    return HookRun(rc, out.getvalue(), err.getvalue())


DRIVERS[:] = [run_hook, run_hook_inproc]


# ---- Stop-hook shared vocabulary (tests/test_hooks_stop.py + test_hooks_stop_poller.py) ----------

WAKE = 2      # exit 2 → wake the idle lead
SILENT = 0    # exit 0 → stay idle


def stop_payload(home, sid="lead-1", **kw):
    return dict({"session_id": sid, "cwd": str(home)}, **kw)


def armed_lead_with_config(home, sid="lead-1", **cfg):
    """An armed lead plus a config that keeps the background poller from holding a test open for
    its 1800s default and keeps the auto-close sweep (a real `bin/relay` subprocess) out of the
    way. Individual tests re-enable whatever they are actually testing."""
    root = arm_lead(home, sid, project="proj")
    write_config(home, **cfg)
    return root


# ---- state builders -----------------------------------------------------------------------------

def state_root(home):
    return Path(home) / ".relay-tasks"


def arm_lead(home, sid="lead-1", **kw):
    """Write a lead marker under the tmp HOME's state root — the one thing that turns every hook
    from a fast-exit no-op into an active one."""
    root = state_root(home)
    lg.write_marker(root, sid, **kw)
    return root


def write_config(home, **cfg):
    """`~/.relay-tasks/lead/config.json`. Tests default `auto_close` off (the Stop hook otherwise
    shells out to `bin/relay _auto-close-sweep` on every run) and shrink the poll window so the
    background watcher can't hold a test open for its 1800s default."""
    root = state_root(home)
    (root / "lead").mkdir(parents=True, exist_ok=True)
    base = {"auto_close": False, "poll_seconds": 1, "poll_interval": 1}
    base.update(cfg)
    (root / "lead" / "config.json").write_text(json.dumps(base))
    return base


def make_executor(home, sid="exec-1", packet=1, owner_lead="lead-1", report="done, staged.\n",
                  status="busy", **extra):
    """A relay executor's state dir, in the shape `lead_guard.executor_reports` reads."""
    root = state_root(home)
    d = root / sid
    (d / "packets").mkdir(parents=True, exist_ok=True)
    s = {"session_id": sid, "current_packet": packet, "status": status, "owner_lead": owner_lead}
    s.update(extra)
    (d / "session.json").write_text(json.dumps(s))
    if report is not None:
        (d / "packets" / ("%03d-report.md" % packet)).write_text(report)
    return d


def ledger(home):
    """Every record in the shared `~/.relay-tasks/sessions.jsonl`, in order."""
    p = state_root(home) / "sessions.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def ledger_events(home):
    return [r.get("event") for r in ledger(home)]


def is_deny(run):
    """True if this HookRun is a PreToolUse deny decision, in the shape the contract names:
    `permissionDecision: "deny"` on stdout, and exit 0 regardless."""
    if not run.stdout.strip():
        return False
    try:
        d = json.loads(run.stdout)
    except Exception:
        return False
    return d.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def deny_reason(run):
    return json.loads(run.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def git_repo(path):
    """A tiny real git repo (the Stop hook's App 2 reads `git rev-parse`/`git rev-list` for real)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)

    def g(*a):
        subprocess.run(["git", "-C", str(path), *a], capture_output=True, timeout=30)

    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    return path


def git_commit(path, message, filename="f.txt", body=None):
    p = Path(path) / filename
    p.write_text(body if body is not None else message)
    subprocess.run(["git", "-C", str(path), "add", "-A"], capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", message],
                   capture_output=True, timeout=30)
