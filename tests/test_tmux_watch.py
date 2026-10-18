"""
Seeing an executor under tmux (bin/relay + hooks): the screen verdict behind `relay check`'s
`screen:` line and `relay list`'s stuck:* status, `relay peek`, the pane.log transcript, and the
lead poller's instant wake (stop_lead_watch's `wait-for` tick + executor_escalation's signal).
Every tmux call is mocked — the real-tmux half lives in tests/test_e2e_tmux.py.
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from test_relay import load_relay_module  # noqa: E402
import conftest_hooks as H  # noqa: E402
from conftest_hooks import lg  # noqa: E402

sys.path.insert(0, str(TESTS.parent / "scripts"))
import tmux_backend  # noqa: E402


@pytest.fixture
def relay(tmp_path):
    return load_relay_module(tmp_path / ".relay-tasks")


# ── captured-screen fixtures ──────────────────────────────────────────────────────────────────────

BASH_PERMISSION = """\
⏺ I'll push the branch now.

⏺ Bash(git push origin wt/x)
  ⎿  Running…

╭──────────────────────────────────────────────────────────────────────────────╮
│ Bash command                                                                 │
│                                                                              │
│   git push origin wt/x                                                       │
│   Push the feature branch                                                    │
│                                                                              │
│ Do you want to proceed?                                                      │
│ ❯ 1. Yes                                                                     │
│   2. Yes, and don't ask again for git push commands in /Users/v/wt           │
│   3. No, and tell Claude what to do differently (esc)                        │
╰──────────────────────────────────────────────────────────────────────────────╯
"""

EDIT_PERMISSION = """\
 Edit file
 bin/relay
 ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  12 -    old line
  12 +    new line
 Do you want to make this edit to relay?
 ❯ 1. Yes
   2. Yes, allow all edits during this session (shift+tab)
   3. No, and tell Claude what to do differently (esc)

 Esc to cancel · Tab to add additional instructions
"""

SELECTION = """\
☐ Approach

Which layout should the split use?

❯ 1. Side by side
     Panes split horizontally
  2. Stacked
     Panes split vertically
  3. Type something.

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

SHELL_AFTER_EXIT = """\
⏺ Report written to /Users/v/.relay-tasks/e/packets/001-report.md.

Resume this session with:
claude --resume 0f2a9c4e-0000-4000-8000-000000000000
vamsi@box:~/development/wt$
"""

WORKING = """\
⏺ Running the targeted tests.

✻ Pondering… (42s · ↑ 1.3k tokens · esc to interrupt)

────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

UNSENT = """\
⏺ Done. Idle, awaiting the lead's review.

╭──────────────────────────────────────────────────────────────────────────────╮
│ > Task — GOAL: fix the thing. Read and follow the work packet at             │
│ /Users/v/.relay-tasks/e/packets/002-packet.md — it contains your task,       │
│ GATES, and REPORT FORMAT.                                                    │
╰──────────────────────────────────────────────────────────────────────────────╯
  ? for shortcuts
"""

# The pointer was SUBMITTED: it renders up in the scrollback, and the box below is empty.
IDLE_SUBMITTED = """\
> Read and follow the work packet at /Users/v/.relay-tasks/e/packets/002-packet.md — it contains
  your task.

⏺ Done. Idle, awaiting the lead's review.

────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
"""

# The model's own prose that happens to contain ONE prompt phrase — not a dialog.
PROSE = """\
⏺ Do you want to also rotate the logs? I left that out of scope.

────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────
"""

NEEDLES = ("Read and follow the work packet at", "002-packet.md")


@pytest.mark.parametrize("screen,command,verdict,excerpt_has", [
    (BASH_PERMISSION, "node", "prompt", "Do you want to proceed?"),
    (EDIT_PERMISSION, "claude", "prompt", "Do you want to make this edit to relay?"),
    (SELECTION, "2.0.14", "prompt", "Enter to select"),
    (SHELL_AFTER_EXIT, "bash", "crashed", "[bash] vamsi@box:~/development/wt$"),
    (SHELL_AFTER_EXIT, "-zsh", "crashed", "[zsh]"),
    (WORKING, "node", "working", "esc to interrupt"),
    (UNSENT, "node", "unsent", "Task — GOAL: fix the thing."),
    (IDLE_SUBMITTED, "node", "unknown", ""),
    (PROSE, "node", "unknown", ""),
    ("", None, "unknown", ""),
], ids=["bash-permission", "edit-permission", "selection", "shell-after-exit", "login-shell",
        "working", "unsent", "idle-submitted", "prose", "blank"])
def test_classify_screen_table(relay, screen, command, verdict, excerpt_has):
    got, excerpt = relay._classify_screen(screen, command, NEEDLES)
    assert got == verdict, (got, excerpt)
    assert excerpt_has in excerpt
    assert "\n" not in excerpt and len(excerpt) <= 100


def test_a_versioned_native_binary_is_not_a_crash(relay):
    """Native Claude Code runs as a binary named after its version — only a SHELL means exited."""
    assert relay._classify_screen(WORKING, "2.1.3", NEEDLES)[0] == "working"


def test_unsent_needs_the_current_packets_pointer(relay):
    s = {"current_packet": 7}
    assert relay._classify_screen(UNSENT, "node", ("007-packet.md",))[0] == "unknown"
    assert "007-packet.md" in relay._screen_needles(s)


# Claude Code 2.1.x after a rename: the input box's TOP rule carries the session name, which the
# old bin/relay-only rule pattern (`^\s*[╭╰]?─{8,}[╮╯]?\s*$`) did not match — no box found, so a
# typed-but-unsubmitted pointer read "unknown" and `unsent` detection was dead after every rename.
UNSENT_LABELLED = """\
⏺ Done. Idle, awaiting the lead's review.

──────────────────────────────────────────────────────────── [Exec] bye-file-214926 ─
❯ Task — GOAL: fix the thing. Read and follow the work packet at
  /Users/v/.relay-tasks/e/packets/002-packet.md — it contains your task, GATES, and REPORT FORMAT.
────────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
"""


def test_unsent_under_a_labelled_border(relay):
    verdict, excerpt = relay._classify_screen(UNSENT_LABELLED, "node", NEEDLES)
    assert verdict == "unsent", (verdict, excerpt)
    assert "Task — GOAL: fix the thing." in excerpt
    assert relay._SCREEN_RULE_RE is relay.trust_prompt._RULE_RE   # ONE pattern, not two


# ── 0.5.13: dialog phrases SHOWN on a screen are not a dialog (field, 2026-10-17) ─────────────────
# A busy ops executor's pane showed `grep` output of bin/relay's PROMPT_PATTERNS comment ("Do you
# want to create X?" (Write), "Do you want to allow …"). `prompt` was checked on phrases alone,
# before `working`, so the idle lead was woken twice with "stuck:prompt". The fixtures below are
# trimmed from screens captured live from Claude Code 2.1.302 on a private tmux server (paths
# shortened, the status line kept): a real dialog REPLACES the input box — it opens under one
# full-width rule and ends on its footer, with no input box and no spinner beneath it.

# The incident: busy (spinner) with the grep output, two+ PROMPT_PATTERNS phrases, above it.
BUSY_UNDER_PHRASES = """\
⏺ Bash(grep -n -A3 "PROMPT_PATTERNS = (" bin/relay)
  ⎿  966:PROMPT_PATTERNS = (
     967-    # Permission-dialog header: "Do you want to proceed?" (Bash), "Do you want to make this edit to
     968-    # X?" (Edit), "Do you want to create X?" (Write), "Do you want to allow …" (WebFetch/MCP).
     969-    re.compile(r"\\bDo you want to\\b"),
     970-    # The dialog's numbered first option: "❯ 1. Yes"
     971-    # its "No" option: "3. No, and tell Claude what to do differently (esc)"
     972-    # footer: "Esc to cancel · Tab to add additional instructions", "Enter to select · ↑/↓"

✻ Pondering… (42s · ↑ 1.3k tokens · esc to interrupt)

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

# Same, with the 2.1.302 spinner as captured live — it no longer says "esc to interrupt", so only
# the input box below the phrases tells them apart from a dialog.
BUSY_UNDER_PHRASES_2_1_302 = """\
⏺ LINE-A Do you want to proceed?
  LINE-B 1. Yes
  LINE-C 2. No, and tell Claude (esc)
  LINE-D Esc to cancel

⏺ Bash(python3 -c 'import time; time.sleep(40)')
  ⎿  Running… (30s)
     (ctrl+b ctrl+b (twice) to run in background)

· Mulling… (32s · ↓ 157 tokens)

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Haiku 5.5  .../development/claude-relay  git:main*  ctx:5%
  ⏸ manual mode on
"""

# Idle (no spinner): the same phrases in the scrollback above an empty input box.
IDLE_UNDER_PHRASES = """\
⏺ LINE-A Do you want to proceed?
  LINE-B 1. Yes
  LINE-C 2. No, and tell Claude (esc)
  LINE-D Esc to cancel · Enter to select

⏺ Done.

✻ Worked for 2s · done 1:59 AM

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Haiku 5.5  .../development/claude-relay  git:main*  ctx:5%
  ⏸ manual mode on
"""

# A real Bash permission dialog, 2.1.302 — and the same phrases quoted in the scrollback above it.
REAL_BASH_DIALOG_UNDER_PHRASES = """\
⏺ LINE-A Do you want to proceed?
  LINE-B 1. Yes
  LINE-C 2. No, and tell Claude (esc)
  LINE-D Esc to cancel

⏺ Bash(python3 -c 'import time; time.sleep(40)')
  ⎿  Waiting…
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Bash command
 Tip: auto mode handles these prompts for you — choose "switch to auto mode" below
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 │ python3 -c 'import time; time.sleep(40)'
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Wait 40 seconds
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for python3 commands in /Users/v/development/claude-relay
   3. Yes, and switch to auto mode · auto mode handles these prompts for you
   4. No
 Esc to cancel · Tab to amend
"""

REAL_WRITE_DIALOG = """\
❯ Use the Write tool to create the file /tmp/s/probe-written.txt containing the word hi.

⏺ Write(/tmp/s/probe-written.txt)
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Create file
 /tmp/s/probe-written.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 hi
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create probe-written.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this
      session (shift+tab)
   3. No
 Esc to cancel · Tab to amend
"""

# AskUserQuestion, 2.1.302: note the SECOND rule above "Chat about this" — one rule below the
# options must still read as live, so only TWO rules below a row mean an input box.
REAL_ASK_USER_QUESTION = """\
❯ Use the AskUserQuestion tool to ask me one question: which color do I prefer, red or blue.
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
 ☐ Color
Which color do you prefer, red or blue?
❯ 1. Blue
     Blue as your preferred color
  2. Red
     Red as your preferred color
  3. Type something.
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  4. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
"""

# /model, 2.1.302 (packet 003 item 3: informational): its footer carries ONE pattern only.
REAL_MODEL_MENU = """\
  ❯ 5.  Haiku 5.5 ✔            Fastest for quick answers
    6.  Haiku 4.5              Fastest for quick answers
     … +3 models

  ◐ Medium effort (default) ←/→ to adjust

  Enter to set as default · s to use this session only · Esc to cancel
"""


@pytest.mark.parametrize("screen,verdict,excerpt_has", [
    (BUSY_UNDER_PHRASES, "working", "esc to interrupt"),
    (BUSY_UNDER_PHRASES_2_1_302, "working", "Mulling…"),   # read `unknown` before the shared busy rule
    (IDLE_UNDER_PHRASES, "unknown", ""),
    (REAL_BASH_DIALOG_UNDER_PHRASES, "prompt", "Do you want to proceed?"),
    (REAL_WRITE_DIALOG, "prompt", "Do you want to create probe-written.txt?"),
    (REAL_ASK_USER_QUESTION, "prompt", "Enter to select"),
    (REAL_MODEL_MENU, "unknown", ""),
], ids=["busy-under-phrases", "busy-under-phrases-2.1.302-spinner", "idle-under-phrases",
        "real-bash-dialog-under-phrases", "real-write-dialog", "real-ask-user-question",
        "real-model-menu"])
def test_only_a_dialog_at_the_live_bottom_is_a_prompt(relay, screen, verdict, excerpt_has):
    got, excerpt = relay._classify_screen(screen, "2.1.302", NEEDLES)
    assert got == verdict, (got, excerpt)
    assert excerpt_has in excerpt


def test_the_excerpt_is_the_live_dialogs_question_not_a_quoted_one(relay):
    _, excerpt = relay._classify_screen(REAL_BASH_DIALOG_UNDER_PHRASES, "2.1.302", NEEDLES)
    assert "LINE-A" not in excerpt and excerpt == "Do you want to proceed?"


def test_stuck_executors_does_not_flag_a_busy_pane_showing_dialog_phrases(relay, screen, capsys):
    _tmux_session(relay, "exec-1", owner_lead="lead-1")
    for shown in (BUSY_UNDER_PHRASES, BUSY_UNDER_PHRASES_2_1_302, IDLE_UNDER_PHRASES):
        screen["screen"] = shown
        relay.cmd_stuck_executors(SimpleNamespace(lead="lead-1", json=True))
        row = json.loads(capsys.readouterr().out)[0]
        assert row["stuck"] is False and row["verdict"] != "prompt", row
    screen["screen"] = REAL_BASH_DIALOG_UNDER_PHRASES
    relay.cmd_stuck_executors(SimpleNamespace(lead="lead-1", json=True))
    assert json.loads(capsys.readouterr().out)[0]["verdict"] == "prompt"


# ── 0.5.13: a busy 2.1.302 pane reads `working` again (field, 2026-10-18) ───────────────────────
# Claude Code 2.1.296+ dropped "esc to interrupt" from its spinner row, so a busy pane read
# `unknown`. The fixtures below are trimmed from screens captured live from Claude Code 2.1.302
# (`claude --model haiku`, a private `tmux -L relay-probe-busy-*` server, 120x45, paths shortened):
# thinking, streaming, a running tool, the first second after launch, and idle before and after a
# turn. The spinner row is a glyph in column 0 (· ✢ ✳ ✶ ✻ ✽), a verb ending in "…" and an optional
# parenthetical; the done line ("✻ Worked for 1s · done 2:26 AM") has no "…" after its verb.

BUSY_2_1_302_THINKING = """\
❯ Write a 600-word essay about lighthouses. Plain prose, no tools.

✳ Befuddling… (12s · still thinking)

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Haiku 5.5  .../development/claude-relay  git:main*  sid:9e4b0a49
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

BUSY_2_1_302_STREAMING = """\
❯ Write a 600-word essay about lighthouses. Plain prose, no tools.

⏺ Lighthouses are among the oldest and most stubbornly useful structures people have built. Their job is
  simple to state and hard to do well: stand at the edge of land, show a light, and tell passing ships

✳ Befuddling… (13s · ↓ 2.1k tokens · thought for 12s)

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Haiku 5.5  .../development/claude-relay  git:main*  sid:9e4b0a49                                 /rc
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

# A running tool: the tool's own "⎿  Running… (5s)" row (indented, no glyph) and a Tip row between
# the spinner and the input box.
BUSY_2_1_302_TOOL = """\
✻ Brewed for 16s · done 2:25 AM

❯ Run exactly this Bash command and nothing else: sleep 20

⏺ Bash(sleep 20)
  ⎿  Running… (5s)
     (ctrl+b ctrl+b (twice) to run in background)

✢ Sprouting… (7s · ↓ 81 tokens)
  ⎿  Tip: Run /install-github-app to tag @claude right from your Github issues and PRs

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Haiku 5.5  .../development/claude-relay  git:main*  sid:9e4b0a49  ctx:5%                       /rc
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

# The first second after a launch with a prompt (what a spawned executor shows): the spinner with
# no parenthetical at all, and the footer's "/rc connecting…" (indented, no glyph).
BUSY_2_1_302_BARE = """\
 ▐▛███▛█   Claude Code v2.1.302
▝▜██████▀  Haiku 5.5 · Claude Max
 ▝▝   ▝▝   ~/development/claude-relay

❯ Run the Bash command sleep 12, then reply with exactly: done

✽ Spinning…

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents                     ◐ medium · /effort
                                                                                      /rc connecting…
"""

BUSY_2_1_302_HOOKS = BUSY_2_1_302_BARE.replace(
    "✽ Spinning…", "✢ Spinning… (running UserPromptSubmit hooks… 1/2 · 0s)")

IDLE_2_1_302_START = """\
 ▐▛███▛█   Claude Code v2.1.302
▝▜██████▀  Haiku 5.5 · Claude Max
 ▝▝   ▝▝   ~/development/claude-relay

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯ Try "create a util logging.py that..."
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Haiku 5.5  .../development/claude-relay  git:main*  sid:9e4b0a49                 ◐ medium · /effort
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents                                    /rc
"""

IDLE_2_1_302_AFTER_TURN = """\
✻ Cooked for 22s · done 2:26 AM

❯ Think hard about whether 1000003 is prime, then answer in one word.

⏺ Prime

✻ Worked for 1s · done 2:26 AM

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Haiku 5.5  .../development/claude-relay  git:main*  sid:9e4b0a49  ctx:5%
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

IDLE_2_1_302_AFTER_TOOL = """\
⏺ Bash(sleep 20)
  ⎿  Running… (No output)

⏺ The command sleep 20 ran and finished with no output.

✻ Cooked for 22s · done 2:26 AM

──────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  Haiku 5.5  .../development/claude-relay  git:main*  sid:9e4b0a49  ctx:5%                       /rc
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

BUSY_2_1_302 = {"thinking": BUSY_2_1_302_THINKING, "streaming": BUSY_2_1_302_STREAMING,
                "tool": BUSY_2_1_302_TOOL, "bare": BUSY_2_1_302_BARE, "hooks": BUSY_2_1_302_HOOKS,
                "busy-under-phrases": BUSY_UNDER_PHRASES_2_1_302}
IDLE_2_1_302 = {"start": IDLE_2_1_302_START, "after-turn": IDLE_2_1_302_AFTER_TURN,
                "after-tool": IDLE_2_1_302_AFTER_TOOL, "idle-under-phrases": IDLE_UNDER_PHRASES}


@pytest.mark.parametrize("name", list(BUSY_2_1_302))
def test_a_busy_2_1_302_pane_reads_working(relay, name):
    screen = BUSY_2_1_302[name]
    verdict, excerpt = relay._classify_screen(screen, "2.1.302", NEEDLES)
    assert verdict == "working", (verdict, excerpt)
    assert excerpt.endswith(")") or excerpt.endswith("…")   # the spinner row itself is the excerpt
    assert relay.trust_prompt.is_busy(screen) is True
    assert relay.trust_prompt.is_live(screen) is False


@pytest.mark.parametrize("name", list(IDLE_2_1_302))
def test_an_idle_2_1_302_pane_is_not_working_and_its_input_box_is_ready(relay, name):
    screen = IDLE_2_1_302[name]
    verdict, excerpt = relay._classify_screen(screen, "2.1.302", NEEDLES)
    assert verdict == "unknown", (verdict, excerpt)
    assert relay.trust_prompt.is_busy(screen) is False
    assert relay.trust_prompt.input_ready(screen) is True
    assert relay.trust_prompt.launch_state(screen) == "ready"


@pytest.mark.parametrize("row", [
    "✻ Thinking… (12s · esc to interrupt)",                       # older builds
    "✻ Pondering… (42s · ↑ 1.3k tokens · esc to interrupt)",
    "✽ Spinning…",                                                 # 2.1.302, as captured
    "✽ Spinning…" + " " * 40,                                      # tmux pads rows with spaces
    "✢ Spinning… (running UserPromptSubmit hooks… 1/2 · 0s)",
    "✳ Befuddling… (8s · thinking)",
    "· Sprouting… (13s · ↓ 81 tokens)",
    "✢ Thundering… (18m 58s · ↓ 41.8k tokens)",                    # 2.1.296 (test_backends)
    "✶ Reticulating splines… (3s)",                                # a two-word custom verb
])
def test_spinner_rows_old_and_new(relay, row):
    assert relay.trust_prompt.is_spinner_row(row) is True
    assert relay._screen_working is relay.trust_prompt.is_spinner_row   # ONE rule, not two


@pytest.mark.parametrize("row", [
    "✻ Worked for 12s",                                            # the done line
    "✻ Worked for 1s · done 2:26 AM",
    "✻ Brewed for 16s · done 2:25 AM",
    "✻ Cogitated for 15s · done 2:25…",                            # even if truncated with "…"
    "  ⎿  Running… (5s)",                                          # a tool's own progress row
    "                                       /rc connecting…",      # status-line chrome
    "     ✻ Thinking… (12s)",                                      # quoted in tool output
    "  ✶ Elucidating… (10s · ↓ 82 tokens)",                        # quoted in assistant prose
    "⏺ Spinning… is what the spinner says",                        # an assistant line
    "❯ ✽ Spinning…",                                               # a submitted user line
    "✻ 3 files…",                                                  # not a verb
    "",
])
def test_rows_that_are_not_the_spinner(relay, row):
    assert relay.trust_prompt.is_spinner_row(row) is False


def test_stuck_executors_reports_a_busy_2_1_302_pane_as_working(relay, screen, capsys):
    _tmux_session(relay, "exec-1", owner_lead="lead-1")
    for shown in BUSY_2_1_302.values():
        screen["screen"] = shown
        relay.cmd_stuck_executors(SimpleNamespace(lead="lead-1", json=True))
        row = json.loads(capsys.readouterr().out)[0]
        assert row["verdict"] == "working" and row["stuck"] is False, row


# ── _screen_verdict: tmux only ────────────────────────────────────────────────────────────────────

def _tmux_session(relay, sid="exec-1", status="busy", **extra):
    s = {"session_id": sid, "status": status, "current_packet": 2, "backend": "tmux",
         "iterm_session": "tmux:%3", "tab_label": "[Exec] e", "topic": "e", "scope": "e",
         "worktree": "/nonexistent/wt"}
    s.update(extra)
    relay.write_session(sid, s)
    return s


@pytest.fixture
def screen(monkeypatch):
    """Patch the tmux backend's two reads; returns a dict the test fills in."""
    st = {"screen": BASH_PERMISSION, "command": "node", "calls": []}

    def capture(handle, lines=40):
        st["calls"].append(("capture", handle, lines))
        return st["screen"]

    def current_command(handle):
        st["calls"].append(("command", handle))
        return st["command"]
    monkeypatch.setattr(tmux_backend, "capture", capture)
    monkeypatch.setattr(tmux_backend, "current_command", current_command)
    return st


def test_screen_verdict_reads_a_tmux_pane(relay, screen):
    s = _tmux_session(relay)
    assert relay.backend.by_name("tmux") is tmux_backend
    v = relay._screen_verdict(s)
    assert v == {"verdict": "prompt", "excerpt": "Do you want to proceed?"}
    assert ("capture", "tmux:%3", 40) in screen["calls"]


@pytest.mark.parametrize("extra", [{"backend": "iterm", "iterm_session": "w0t0p0:x"},
                                   {"backend": "terminal", "iterm_session": "twid:3"},
                                   {"iterm_session": None}])
def test_screen_verdict_is_none_off_tmux(relay, screen, extra):
    s = _tmux_session(relay, **extra)
    assert relay._screen_verdict(s) is None
    assert screen["calls"] == []


def test_screen_verdict_is_none_when_the_pane_is_gone(relay, screen):
    screen["screen"] = None
    assert relay._screen_verdict(_tmux_session(relay)) is None


# ── relay check / relay list surfaces ─────────────────────────────────────────────────────────────

def _quiet_sweeps(relay, monkeypatch):
    monkeypatch.setattr(relay, "deliver_queued", lambda *a, **k: None)
    monkeypatch.setattr(relay, "auto_close_sweep", lambda *a, **k: [])
    monkeypatch.setattr(relay, "refresh_live_board", lambda *a, **k: None)
    monkeypatch.setattr(relay, "_armed_lead_caller", lambda *a, **k: None)
    monkeypatch.setattr(relay, "_usage_for_session", lambda *a, **k: None)
    monkeypatch.setattr(relay, "_check_one", lambda sid, persist=True: dict(relay.read_session(sid)))


def test_check_prints_the_screen_line_for_a_tmux_session(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay)
    relay.cmd_check(SimpleNamespace(all=False, session_id="exec-1", json=False))
    out = capsys.readouterr().out
    assert "screen: prompt — Do you want to proceed?" in out


def test_check_json_carries_the_screen(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay)
    relay.cmd_check(SimpleNamespace(all=False, session_id="exec-1", json=True))
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["screen"] == {"verdict": "prompt", "excerpt": "Do you want to proceed?"}


def test_check_prints_no_screen_line_off_tmux(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay, backend="iterm", iterm_session="w0t0p0:x")
    relay.cmd_check(SimpleNamespace(all=False, session_id="exec-1", json=False))
    assert "screen:" not in capsys.readouterr().out
    assert screen["calls"] == []


def test_check_skips_a_closed_session(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay, status="closed")
    relay.cmd_check(SimpleNamespace(all=False, session_id="exec-1", json=False))
    assert "screen:" not in capsys.readouterr().out


def _list(relay, capsys):
    relay.cmd_list(SimpleNamespace(lead=None, all=False, json=False, closed=False, all_leads=False))
    return capsys.readouterr().out


@pytest.mark.parametrize("scr,cmd,want", [(BASH_PERMISSION, "node", "stuck:prompt"),
                                          (SHELL_AFTER_EXIT, "zsh", "stuck:crashed")],
                         ids=["prompt", "crashed"])
def test_list_shows_stuck_regardless_of_the_stall_threshold(relay, screen, monkeypatch, capsys,
                                                            scr, cmd, want):
    _quiet_sweeps(relay, monkeypatch)
    screen["screen"], screen["command"] = scr, cmd
    import time as _t
    _tmux_session(relay, busy_since_epoch=_t.time() - 60)   # busy 1 minute — far under 45
    assert want in _list(relay, capsys)


def test_list_keeps_busy_when_the_screen_is_working(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    screen["screen"] = WORKING
    import time as _t
    _tmux_session(relay, busy_since_epoch=_t.time() - 60)
    out = _list(relay, capsys)
    assert "stuck:" not in out and "busy 1m" in out


def test_list_never_reads_a_non_tmux_pane(relay, screen, monkeypatch, capsys):
    _quiet_sweeps(relay, monkeypatch)
    _tmux_session(relay, backend="iterm", iterm_session="w0t0p0:x")
    assert "stuck:" not in _list(relay, capsys)
    assert screen["calls"] == []


# ── relay peek ─────────────────────────────────────────────────────────────────────────────────────

def test_peek_prints_the_screen(relay, screen, capsys):
    _tmux_session(relay)
    screen["screen"] = WORKING + "\n\n\n"
    relay.cmd_peek(SimpleNamespace(session_id="exec-1", lines=12, log=False))
    out = capsys.readouterr().out
    assert "esc to interrupt" in out and not out.endswith("\n\n")
    assert ("capture", "tmux:%3", 12) in screen["calls"]


def test_peek_off_tmux_says_it_needs_tmux(relay, screen, capsys):
    _tmux_session(relay, backend="iterm", iterm_session="w0t0p0:x")
    with pytest.raises(SystemExit) as e:
        relay.cmd_peek(SimpleNamespace(session_id="exec-1", lines=40, log=False))
    assert "needs the tmux backend" in str(e.value) and "\n" not in str(e.value)
    assert screen["calls"] == []


def test_peek_log_tails_pane_log_without_escapes(relay, capsys):
    _tmux_session(relay)
    log = relay.pane_log_path("exec-1")
    log.write_bytes(b"".join(b"\x1b[1mrow %d\x1b[0m\r\n" % i for i in range(100)))
    relay.cmd_peek(SimpleNamespace(session_id="exec-1", lines=3, log=True))
    assert capsys.readouterr().out.splitlines() == ["row 97", "row 98", "row 99"]


def test_peek_log_missing(relay):
    _tmux_session(relay)
    with pytest.raises(SystemExit) as e:
        relay.cmd_peek(SimpleNamespace(session_id="exec-1", lines=3, log=True))
    assert "no pane log" in str(e.value)


# ── pane.log: start at spawn, rotate at spawn/send, config switch ─────────────────────────────────

class _PipeSpy:
    NAME = "tmux"

    def __init__(self):
        self.calls = []

    def pipe_pane(self, handle, path):
        self.calls.append(("pipe", handle, path))
        return True

    def unpipe_pane(self, handle):
        self.calls.append(("unpipe", handle))
        return True


def test_pane_log_is_piped(relay):
    _tmux_session(relay)
    bk = _PipeSpy()
    assert relay._pane_log_maintain(bk, "exec-1", "tmux:%3") is True
    assert bk.calls == [("pipe", "tmux:%3", str(relay.pane_log_path("exec-1")))]


def test_pane_log_rotates_past_the_cap(relay, monkeypatch):
    _tmux_session(relay)
    monkeypatch.setattr(relay, "PANE_LOG_MAX_BYTES", 10)
    log = relay.pane_log_path("exec-1")
    log.write_text("x" * 11)
    (log.parent / "pane.log.1").write_text("older")
    bk = _PipeSpy()
    relay._pane_log_maintain(bk, "exec-1", "tmux:%3")
    assert (log.parent / "pane.log.1").read_text() == "x" * 11 and not log.exists()
    assert bk.calls == [("unpipe", "tmux:%3"), ("pipe", "tmux:%3", str(log))]


def test_pane_log_under_the_cap_is_left_alone(relay, monkeypatch):
    _tmux_session(relay)
    monkeypatch.setattr(relay, "PANE_LOG_MAX_BYTES", 10)
    log = relay.pane_log_path("exec-1")
    log.write_text("x" * 10)
    bk = _PipeSpy()
    relay._pane_log_maintain(bk, "exec-1", "tmux:%3")
    assert log.read_text() == "x" * 10 and bk.calls[0][0] == "pipe"


def test_pane_log_config_off(relay, tmp_path):
    _tmux_session(relay)
    cfg = tmp_path / ".relay-tasks" / "lead" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"tmux_pane_log": False}))
    bk = _PipeSpy()
    assert relay._pane_log_maintain(bk, "exec-1", "tmux:%3") is False
    assert bk.calls == []


def test_pane_log_is_tmux_only(relay):
    bk = _PipeSpy()
    bk.NAME = "iterm"
    assert relay._pane_log_maintain(bk, "exec-1", "w0t0p0:x") is False
    assert relay._pane_log_maintain(_PipeSpy(), "exec-1", None) is False


def test_tmux_pane_log_default_is_on():
    assert lg.LEAD_DEFAULTS["tmux_pane_log"] is True


# ── instant wake: the lead poller's tick, and the executor's signal ───────────────────────────────

STOP, ESCALATE = "stop_lead_watch.py", "executor_escalation.py"


def _poller_case(tmp_path, monkeypatch, backend):
    """An armed lead (marker backend `backend`) with one busy executor. The fake wait/sleep land
    the report on their FIRST call, so the poller wakes on the next check either way."""
    H.arm_lead(tmp_path, "lead-1", project="proj", backend=backend)
    H.write_config(tmp_path, poll_seconds=20, poll_interval=3)
    H.make_executor(tmp_path, report=None, status="busy")
    report = H.state_root(tmp_path) / "exec-1" / "packets" / "001-report.md"
    seen = {"wait": [], "sleep": []}

    def fake_wait(channel, timeout):
        seen["wait"].append((channel, timeout))
        report.write_text("landed via wait-for\n")
        return "signalled"

    import time as _time
    real_sleep = _time.sleep

    def fake_sleep(sec):
        seen["sleep"].append(sec)
        if sec == 3:
            report.write_text("landed via sleep\n")
        else:
            real_sleep(sec)
    monkeypatch.setattr(tmux_backend, "wait", fake_wait)
    monkeypatch.setattr(_time, "sleep", fake_sleep)
    return seen


def test_the_poller_waits_on_tmux_wait_for_under_tmux(tmp_path, monkeypatch):
    seen = _poller_case(tmp_path, monkeypatch, "tmux")
    run = H.run_hook_inproc(STOP, H.stop_payload(tmp_path), tmp_path)
    assert run.returncode == H.WAKE and "landed via wait-for" in run.stderr
    assert seen["wait"] == [("relay-wake-lead-1", 3)]
    assert 3 not in seen["sleep"]


@pytest.mark.parametrize("backend", ["iterm", "terminal", None])
def test_the_poller_sleeps_off_tmux(tmp_path, monkeypatch, backend):
    seen = _poller_case(tmp_path, monkeypatch, backend)
    run = H.run_hook_inproc(STOP, H.stop_payload(tmp_path), tmp_path)
    assert run.returncode == H.WAKE and "landed via sleep" in run.stderr
    assert seen["wait"] == [] and 3 in seen["sleep"]   # interval sleep ran (Linux adds a 1 ms one first)


def test_the_poller_falls_back_to_sleep_when_wait_for_errors(tmp_path, monkeypatch):
    seen = _poller_case(tmp_path, monkeypatch, "tmux")
    monkeypatch.setattr(tmux_backend, "wait",
                        lambda ch, t: seen["wait"].append((ch, t)) or "error")
    run = H.run_hook_inproc(STOP, H.stop_payload(tmp_path), tmp_path)
    assert run.returncode == H.WAKE and "landed via sleep" in run.stderr
    assert seen["wait"] == [("relay-wake-lead-1", 3)] and 3 in seen["sleep"]


@pytest.mark.parametrize("drv", H.DRIVERS, ids=H.DRIVER_IDS)
def test_a_tmux_executor_signals_its_lead_when_its_report_is_there(drv, tmp_path):
    H.arm_lead(tmp_path, "lead-1")
    H.make_executor(tmp_path, owner_lead="lead-1", backend="tmux")
    stub = H.stub_bin(tmp_path)
    drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",), stub=stub)
    calls = [c for c in H.stub_calls(stub[1]) if "/tmux " in c]
    assert any(c.endswith("tmux wait-for -S relay-wake-lead-1") for c in calls), calls


@pytest.mark.parametrize("drv", H.DRIVERS, ids=H.DRIVER_IDS)
@pytest.mark.parametrize("case", ["no-report", "iterm", "unowned"])
def test_no_signal_without_a_report_off_tmux_or_unowned(drv, tmp_path, case):
    H.arm_lead(tmp_path, "lead-1")
    H.make_executor(tmp_path, owner_lead=None if case == "unowned" else "lead-1",
                    backend="iterm" if case == "iterm" else "tmux",
                    report=None if case == "no-report" else "done.\n")
    stub = H.stub_bin(tmp_path)
    drv(ESCALATE, {"session_id": "x"}, tmp_path, argv=("exec-1",), stub=stub)
    assert not [c for c in H.stub_calls(stub[1]) if "wait-for" in c]


# ── wiring: spawn and send both keep the pane piped (the helper itself no-ops off tmux) ───────────

def test_spawn_starts_the_pane_log(relay, tmp_path, monkeypatch):
    from unittest import mock
    packet = tmp_path / "p.md"
    packet.write_text("do the thing\n\n## Preconditions\n- none\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    seen = []
    monkeypatch.setattr(relay, "_pane_log_maintain", lambda bk, sid, h: seen.append((sid, h)))
    with mock.patch.object(relay.iterm, "spawn", side_effect=lambda **kw: None), \
         mock.patch.object(relay, "auto_trust"), \
         mock.patch.object(relay, "read_pid", return_value=123), \
         mock.patch.object(relay, "read_iterm_id", return_value="tmux:%9"), \
         mock.patch.object(relay, "_ensure_tab_label", return_value=True):
        relay.cmd_spawn(SimpleNamespace(packet=str(packet), topic="foo", name="pl", worktree=str(wt),
                                        model=None, model_override=None, skip_perms=None, pane=None,
                                        lead=None, scope=None))
    assert ("pl", "tmux:%9") in seen


def test_send_checks_the_pane_log_before_typing(relay, tmp_path, monkeypatch):
    from unittest import mock
    sid = "e1"
    relay.packets_dir(sid).mkdir(parents=True, exist_ok=True)
    relay.write_session(sid, {"session_id": sid, "worktree": "/w", "topic": "t", "scope": "t",
        "tab_label": "relay-" + sid, "model": None, "pid": os.getpid(),
        "iterm_session": "w0t0p0:OLD", "claude_session": "cs-x", "status": "busy",
        "current_packet": 1, "busy_since": relay.now(), "created": relay.now(),
        "updated": relay.now()})
    (relay.packets_dir(sid) / "001-packet.md").write_text("first packet")
    (relay.packets_dir(sid) / "001-report.md").write_text("done")
    packet = tmp_path / "next.md"
    packet.write_text("# Follow-up\n\n## Preconditions\n- none\n\nDo the next thing.")
    order = []
    monkeypatch.setattr(relay, "_pane_log_maintain", lambda bk, s, h: order.append(("log", s, h)))
    monkeypatch.setattr(relay, "_send_confirmed",
                        lambda *a, **k: order.append(("send",)) or (True, True))
    with mock.patch.object(relay.iterm, "is_alive", return_value=True):
        relay.cmd_send(SimpleNamespace(session_id=sid, packet=str(packet)))
    assert order[:2] == [("log", sid, "w0t0p0:OLD"), ("send",)]


# ── relay _stuck-executors: the lead poller's one screen read per tick ──────────────────────────

def test_stuck_executors_lists_only_this_leads_in_flight_tmux_executors(relay, screen, capsys):
    _tmux_session(relay, "exec-1", owner_lead="lead-1")
    _tmux_session(relay, "exec-2", owner_lead="lead-2")                       # another lead's
    _tmux_session(relay, "exec-3", owner_lead="lead-1", backend="iterm",
                  iterm_session="w0t0p0:x")                                   # not tmux
    _tmux_session(relay, "exec-4", owner_lead="lead-1", status="reported")    # not in flight
    relay.cmd_stuck_executors(SimpleNamespace(lead="lead-1", json=True))
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"session_id": "exec-1", "packet": 2, "verdict": "prompt", "stuck": True,
                     "excerpt": "Do you want to proceed?"}]
    assert [c for c in screen["calls"] if c[0] == "capture"] == [("capture", "tmux:%3", 40)]


def test_stuck_executors_reports_a_working_or_unreadable_pane_as_not_stuck(relay, screen, capsys):
    _tmux_session(relay, "exec-1", owner_lead="lead-1")
    screen["screen"] = WORKING
    relay.cmd_stuck_executors(SimpleNamespace(lead="lead-1", json=True))
    assert json.loads(capsys.readouterr().out)[0]["stuck"] is False
    screen["screen"] = None                                  # pane gone → "couldn't look"
    relay.cmd_stuck_executors(SimpleNamespace(lead="lead-1", json=True))
    row = json.loads(capsys.readouterr().out)[0]
    assert row["verdict"] is None and row["stuck"] is False


def test_stuck_executors_is_a_real_hidden_subcommand(tmp_path):
    import subprocess
    r = subprocess.run([str(TESTS.parent / "bin" / "relay"), "_stuck-executors", "--lead", "lead-1",
                        "--json"], capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, HOME=str(tmp_path)))
    assert r.returncode == 0 and json.loads(r.stdout) == []
