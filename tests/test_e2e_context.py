"""
Layer 2 (live, real `claude` + API, not CI-able): proves relay's "PROVE the window" primitive
against the real CLI — the `contextWindow` field in a `result` event's `modelUsage`, not the
`[1m]` suffix string, and WITHOUT asserting bare `sonnet`/`opus` must be 200k (verified live on
this machine 2026-09-05: both already report 1_000_000 — an account's baseline the [1m] suffix
does not control). Uses relay's own command builder (iterm.build_claude_cmd), same as every
other live e2e file. Asserts:
  1. `sonnet[1m]` -> modelUsage contextWindow 1_000_000, and init["model"] ends with "[1m]"
  2. `opus[1m]` -> modelUsage contextWindow 1_000_000 (skipped if executor_model_ceiling < opus)
  3. `haiku` -> modelUsage contextWindow 200_000 (a tier with no 1M window at all)
  4. bare `sonnet`/`opus` -> contextWindow in {200_000, 1_000_000}, and never MORE than their
     own [1m] form — relay LEARNS the number, it doesn't assert one.
Writes nothing: this test must not touch tier_windows.json — only `relay doctor` writes it.
Run: python3 tests/test_e2e_context.py   (or pytest with RELAY_E2E_CONTEXT=1). Up to five cheap
("Reply: ok") calls.
"""
import json, os, subprocess, sys
from pathlib import Path
import pytest
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts")); sys.path.insert(0, str(REPO_ROOT / "lib"))
import iterm  # noqa: E402
import lead_guard  # noqa: E402
pytestmark = pytest.mark.skipif(not os.environ.get("RELAY_E2E_CONTEXT"),
                                reason="live test: set RELAY_E2E_CONTEXT=1 (real claude + API calls)")

TIER_WINDOWS_PATH = Path.home() / ".relay-tasks" / "tier_windows.json"


def _opus_allowed():
    cfg = lead_guard.load_config(Path.home() / ".relay-tasks")
    return not lead_guard.model_exceeds_ceiling("opus", cfg.get("executor_model_ceiling", "opus"))


def _snapshot_tier_windows():
    return TIER_WINDOWS_PATH.read_bytes() if TIER_WINDOWS_PATH.exists() else None


def run(model):
    cmd = iterm.build_claude_cmd("Reply: ok", model=model)
    cmd += " -p --output-format stream-json --verbose"
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"claude failed: {r.stderr[-600:]}\n{cmd}"
    init, result_event = None, None
    for line in r.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "system" and d.get("subtype") == "init":
            init = d
        if d.get("type") == "result":
            result_event = d
    assert init, f"no init event\n{cmd}"
    assert result_event, f"no result event\n{cmd}"
    mu = result_event.get("modelUsage") or {}
    assert mu, f"no modelUsage in result event: {result_event}"
    key = next(iter(mu))
    assert key == init.get("model"), f"modelUsage key {key!r} != init model {init.get('model')!r}"
    window = mu[key].get("contextWindow")
    assert window is not None, f"no contextWindow in modelUsage entry: {mu[key]}"
    return init, window, cmd


def test_context_window_proof_live():
    before = _snapshot_tier_windows()
    out = {}

    init, window, cmd = run("sonnet[1m]")
    out["sonnet[1m]"] = (init["model"], window, cmd)
    assert init["model"].endswith("[1m]"), f"init model doesn't carry [1m]: {init['model']}"
    assert window == 1_000_000, f"sonnet[1m] contextWindow was {window}, expected 1_000_000"

    init, window, cmd = run("haiku")
    out["haiku"] = (init["model"], window, cmd)
    assert window == 200_000, f"haiku contextWindow was {window}, expected 200_000"

    init, window, cmd = run("sonnet")
    out["sonnet"] = (init["model"], window, cmd)
    assert window in (200_000, 1_000_000), f"sonnet contextWindow was {window}, expected 200_000 or 1_000_000"
    assert window <= out["sonnet[1m]"][1], "bare sonnet reported MORE than its [1m] form — impossible"

    if _opus_allowed():
        init, window, cmd = run("opus[1m]")
        out["opus[1m]"] = (init["model"], window, cmd)
        assert window == 1_000_000, f"opus[1m] contextWindow was {window}, expected 1_000_000"

        init, window, cmd = run("opus")
        out["opus"] = (init["model"], window, cmd)
        assert window in (200_000, 1_000_000), f"opus contextWindow was {window}, expected 200_000 or 1_000_000"
        assert window <= out["opus[1m]"][1], "bare opus reported MORE than its [1m] form — impossible"

    assert _snapshot_tier_windows() == before, "this e2e test must not write tier_windows.json — only `relay doctor` does"
    return out


if __name__ == "__main__":
    os.environ["RELAY_E2E_CONTEXT"] = "1"
    results = test_context_window_proof_live()
    print(f"\n{'alias':<12} {'resolved model':<28} {'contextWindow':>13}")
    for alias, (model, window, _cmd) in results.items():
        print(f"{alias:<12} {model:<28} {window:>13,}")
    print("\nALL LIVE CONTEXT-WINDOW CHECKS PASSED")
