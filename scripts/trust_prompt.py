"""
Claude Code's folder-trust dialog, as seen on an executor's screen — the ONE place that knows what
it looks like, shared by every backend's typing path and by bin/relay's check/list/send.

Why it matters: a claude launched in a folder it has never trusted opens on

    Quick safety check: Is this a project you created or one you trust? …
    ❯ 1. Yes, I trust this folder
      2. No, exit
    Enter to confirm · Esc to cancel

(older builds: "Do you trust the files in this folder?" / "1. Yes, proceed" / "2. No, exit"). The
dialog is a single-keystroke selector: a typed digit picks an option and Enter confirms one. Relay's
spawn types `/rename [Exec] <topic>-HHMMSS` + Enter into the new tab a beat after launch — every
session id ends in digits — so that line, landing on this dialog, answered "No, exit": claude quit,
the pane dropped to a shell and the executor read as stalled. Relay must never answer this dialog —
accepting trust is the owner's security decision — so every path that types into an executor
whose readiness it can't otherwise vouch for checks here first (launch_state) and types only on
POSITIVE evidence that claude's input box is live ("ready"). The dialog itself ("trust") is
recognised so the lead can be told "waiting for folder trust"; anything else ("starting": not
drawn yet, or a dialog worded in a way HEADERS doesn't know) gets no keystroke either.

Pure text functions only (no backend imports), so iterm.py, tmux_backend.py and bin/relay can all
import it without a cycle.
"""
import re

# The dialog's question — any one of these, in the bottom rows, starts a match.
HEADERS = (
    "Quick safety check",
    "Is this a project you created or one you trust",
    "Do you trust the files in this folder",
)
# Its options / footer — at least one must follow the header, so a header phrase quoted in a
# conversation (a packet ABOUT this dialog, say) with no live selector under it doesn't match.
OPTIONS = (
    "No, exit",
    "Yes, I trust this folder",
    "Yes, proceed",
    "Enter to confirm",
)
# Claude Code's input-box border (a plain rule or a rounded box edge). Two of them BELOW the
# header mean the REPL's input box is drawn under it: the phrase is scrollback, not a live dialog.
# The TOP edge carries the session's name once it has one (observed live, Claude Code 2.1.296:
# "────…──── [Exec] relay-spawn-list-fix-131415 ─"), so text may sit between the dashes as long
# as the row starts with a run of them and ends on one.
_RULE_RE = re.compile(r"^\s*[╭╰]?─{8,}(?:.*─)?[╮╯]?\s*$")
TAIL_ROWS = 30

# THE busy rule — the spinner row Claude Code shows for the whole of a turn, so claude is running
# and no dialog is blocking it. ONE definition, used here (is_live, input_ready) and by bin/relay's
# screen classifier (`working`, and the live-dialog test). Two forms:
#   - older builds: the row ends "(12s · esc to interrupt)" — matched by that phrase, as before;
#   - Claude Code 2.1.296+ (captured live on 2.1.302) dropped that phrase: the row is a spinner
#     glyph in column 0, a space, then a one-to-three-word verb ending in "…", then optionally a
#     parenthetical — "✽ Spinning…", "✶ Elucidating… (10s · ↓ 82 tokens)", "✳ Befuddling… (8s ·
#     thinking)", "✢ Spinning… (running UserPromptSubmit hooks… 1/2 · 0s)", "· Sprouting… (13s · ↓
#     81 tokens)". The glyph cycles through · ✢ ✳ ✶ ✻ ✽.
# What it must NOT match, all seen on the same screens: the done line "✻ Worked for 12s" / "✻ Brewed
# for 16s · done 2:25 AM" (no "…" right after the verb), a tool's "  ⎿  Running… (5s)" and the
# footer's "/rc connecting…" (indented, no glyph), and any of these phrases quoted in tool output or
# prose (indented under "⎿"/"⏺", never in column 0).
_SPINNER_GLYPHS = "·✢✳✶✻✽"
_SPINNER_RE = re.compile(r"^[" + _SPINNER_GLYPHS + r"]\s+[A-Za-z][\w'-]*(?: [A-Za-z][\w'-]*){0,2}…"
                         r"(?=\s|$)")
_LEGACY_SPINNER_RE = re.compile(r"\besc to interrupt\b", re.IGNORECASE)


def is_spinner_row(line):
    """True when this one screen row is Claude Code's mid-turn spinner, in either form (see
    _SPINNER_RE). The single busy-detection rule every screen reader in relay shares."""
    line = line or ""
    return bool(_SPINNER_RE.match(line) or _LEGACY_SPINNER_RE.search(line))


def is_busy(screen, tail_rows=TAIL_ROWS):
    """True when the spinner (is_spinner_row) is in the bottom `tail_rows` non-blank rows."""
    raw = [ln for ln in (screen or "").splitlines() if ln.strip()][-tail_rows:]
    return any(is_spinner_row(ln) for ln in raw)
# What bin/relay shows when an executor is parked on the dialog (check's screen line, list's note).
WAITING_TEXT = "waiting for folder trust: answer Claude Code's trust prompt in the executor's tab"


def _norm(line):
    """One row with box sides and runs of whitespace collapsed — so a phrase Ink hard-wrapped
    across rows still matches once rows are joined with a single space."""
    return re.sub(r"\s+", " ", (line or "").strip().strip("│").strip())


def is_live(screen, tail_rows=TAIL_ROWS):
    """True when the folder-trust dialog is the live thing at the bottom of `screen`: a HEADER in
    the bottom `tail_rows` non-blank rows, an OPTION after it, and neither a working spinner nor an
    input box (two rule lines) below it."""
    raw = [ln for ln in (screen or "").splitlines() if ln.strip()][-tail_rows:]
    rows = [_norm(ln) for ln in raw]
    starts, joined = [], ""
    for r in rows:
        starts.append(len(joined))
        joined += r + " "
    pos = max(joined.rfind(h) for h in HEADERS)
    if pos < 0:
        return False
    after = joined[pos:]
    if not any(o in after for o in OPTIONS):
        return False
    row = max(i for i, s in enumerate(starts) if s <= pos)
    below = raw[row + 1:]
    if any(is_spinner_row(ln) for ln in below):
        return False
    return sum(1 for ln in below if _RULE_RE.match(ln)) < 2


# Input-box evidence (input_ready): the prompt glyph that opens the box's first row, a numbered
# option row (any selector dialog: "❯ 1. Yes", "  2. No, exit"), and how many non-blank rows may sit
# under the box's bottom border (the footer: "? for shortcuts", the permission-mode line, a status
# line) before the "box" is too far up to be the live one.
_PROMPT_ROW_RE = re.compile(r"^[>❯](\s|$)")
_OPTION_ROW_RE = re.compile(r"^[>❯]?\s*\d+\.\s")
FOOTER_ROWS_MAX = 8


def input_ready(screen, tail_rows=TAIL_ROWS):
    """POSITIVE evidence that Claude Code's input box is the live thing at the bottom of `screen`,
    so a typed line lands in it and nowhere else. True when either
      - the working spinner (is_spinner_row, either form) is in the bottom `tail_rows` non-blank
        rows (claude is mid-turn: typed text queues in its input box), or
      - the bottom-most pair of rule lines sits within FOOTER_ROWS_MAX rows of the bottom, the first
        non-empty row between them starts with the prompt glyph ("> " / "❯ ") and no row between
        them is a numbered option.
    Anything else is NOT ready — including any dialog relay doesn't know: a selector's rows are
    numbered options, a bordered dialog's first row is its question rather than a prompt glyph,
    and a borderless one has no rule pair at the bottom at all. Absence of a known dialog is
    never treated as readiness."""
    raw = [ln for ln in (screen or "").splitlines() if ln.strip()][-tail_rows:]
    if is_busy(screen, tail_rows):
        return True
    rules = [i for i, ln in enumerate(raw) if _RULE_RE.match(ln)]
    if len(rules) < 2 or len(raw) - 1 - rules[-1] > FOOTER_ROWS_MAX:
        return False
    inside = [_norm(ln) for ln in raw[rules[-2] + 1:rules[-1]]]
    inside = [r for r in inside if r]
    if not inside:
        return False
    if not _PROMPT_ROW_RE.match(inside[0]):
        return False
    return not any(_OPTION_ROW_RE.match(r) for r in inside)


def launch_state(screen):
    """An executor's screen, for any typed line (the spawn-time /rename, a label retry, a send into
    a claude that hasn't started its conversation): "trust" (the folder-trust dialog is up — type
    nothing, and say so), "ready" (input_ready: positive evidence the input box is live — typing is
    safe), or "starting" (anything else: not drawn yet, or a dialog relay doesn't recognise — type
    nothing). Trust is checked first so the lead gets the specific "waiting for folder trust"."""
    if is_live(screen):
        return "trust"
    return "ready" if input_ready(screen) else "starting"
