"""
iTerm2 backend for claude-relay: spawn a new tab running `claude` seeded with a prompt,
send a follow-up prompt into an existing live tab, and check aliveness.

Vendored/adapted from ~/.swiftbar/.lib/ccsessions/app.py (ITermBackend) — same title-matching
rules, scoped to just what relay needs. Tab title is owned by Claude Code's own OSC title updates
and mutable at any time, so send()/focus()/close()/is_alive() all address by the captured iTerm
session id (`iterm_session` / $TERM_SESSION_ID) first when one is available, falling back to the
bounded title match only for legacy/unowned sessions with no captured handle — see each function's
docstring. The caller (relay) additionally treats the recorded PID as the source of truth for
aliveness, the title/id match as a secondary confirmation.
"""
import os
import shlex
import subprocess

import iterm_pyapi

NAME = "iterm"  # backend key (see scripts/backend.py)
ITERM_APP_NAME = "iTerm"
CLAUDE_BIN = "claude"
# Claude Code's own OSC title updates append "<NBSP><em-dash><NBSP><extra>" after whatever label
# we set via /rename -- confirmed empirically (iTerm's actual title used U+00A0, not a regular
# space, which a terminal renders indistinguishably from a normal space -- do not "simplify" this
# to a plain space again, it silently breaks every title match).
TAB_TITLE_SEP = " —"


def run_osascript(script, timeout=None):
    try:
        return subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["osascript"], 1, "", "osascript timed out")


def osa(s):
    """Escape a Python string for embedding inside an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")


def _app_running(binary_suffix):
    """App-running check via `ps` comm listing — NOT pgrep: sandboxed shells (Claude Code's Bash
    tool) hide the GUI app's process from pgrep entirely (both -x and -f miss it while ps lists it
    fine — observed live; every title check then silently returned empty and executors read as
    dead). The guard must be accurate because a `tell application` to a non-running app would
    LAUNCH it."""
    try:
        r = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=5)
        return any(line.strip().endswith(binary_suffix) for line in r.stdout.splitlines())
    except Exception:
        return False


def running():
    return _app_running("iTerm.app/Contents/MacOS/iTerm2")


def live_session_names():
    if not running():
        return set()
    script = (
        f'tell application "{ITERM_APP_NAME}"\n'
        '  set out to ""\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      repeat with s in sessions of t\n"
        '        set out to out & (name of s) & linefeed\n'
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )
    r = run_osascript(script, timeout=3)
    if r.returncode != 0:
        return set()
    return {ln for ln in (l.strip() for l in r.stdout.splitlines()) if ln}


def title_is_live(label, live_names):
    """Bounded match: label is live if some tab title equals it, starts/ends with it at a
    label boundary, or has it followed by the status-separator (Claude's own suffix)."""
    for title in live_names:
        if (
            title == label
            or title.startswith(label + " ")
            or title.endswith(" " + label)
            or (label + TAB_TITLE_SEP) in title
            or title.startswith(label + TAB_TITLE_SEP)
        ):
            return True
    return False


def _session_exists_by_id(iterm_id):
    """True/False if an id-based lookup ran and found (or didn't find) a matching session, None if
    the osascript call itself failed (iTerm not running, timeout, etc) — callers treat None like
    False (id lookup found nothing) and fall back to title matching."""
    if not iterm_id:
        return None
    uuid = iterm_id.split(":")[-1]
    script = _for_session_by_id(uuid, "          return true\n") + "return false"
    r = run_osascript(script, timeout=5)
    if r.returncode != 0:
        return None
    return r.stdout.strip().lower() == "true"


def is_alive(label, handle=None, pid=None):
    # pid is part of the shared backend signature (Terminal.app addresses by window id); unused here.
    # handle ($TERM_SESSION_ID) is unambiguous where present — two tabs can share a title (e.g. a
    # handoff predecessor/successor pair both titled "[Lead] <project>") but never an iTerm session
    # id. Fall back to the bounded title match when handle is empty or the id lookup finds nothing.
    if handle:
        found = _session_exists_by_id(handle)
        if found:
            return True
    return title_is_live(label, live_session_names())


def build_claude_cmd(prompt, model=None, skip_perms=False, session_uuid=None, resume_id=None,
                     settings_file=None, mcp_flags=None, agent_flags=None):
    """The `claude …` invocation both backends launch. resume_id reopens an existing conversation
    (no --model — the session already has one); otherwise a fresh session, optionally pinned to
    session_uuid so relay can `--resume` it later without scraping transcripts.

    settings_file: path to a `--settings` JSON file — how an EXECUTOR gets hooks at all (a plain
    `claude` launch has none; the lead gets its hooks from the plugin instead). Used to arm the
    wake-watch executor-escalation Stop hook (lead_guard.write_escalation_settings).

    mcp_flags: extra argv words from lead_guard.mcp_cli_flags (`--strict-mcp-config --mcp-config …`)
    that pin the executor's MCP set — relay's policy (default none), applied on fresh launches AND
    resumes alike, since MCP loading is per-process, not per-conversation.

    agent_flags: lead_guard.executor_agent_flags — the inline executor agent (`--agents … --agent
    relay-executor --disallowedTools …`), likewise re-passed on every relaunch."""
    base = CLAUDE_BIN + (" --dangerously-skip-permissions" if skip_perms else "")
    if settings_file:
        base += " --settings " + shlex.quote(settings_file)
    for w in list(mcp_flags or []) + list(agent_flags or []):
        base += " " + shlex.quote(str(w))
    if resume_id:
        base += " --resume " + shlex.quote(resume_id)
    else:
        if session_uuid:
            base += " --session-id " + shlex.quote(session_uuid)
        if model:
            base += " --model " + shlex.quote(model)
    base += " " + shlex.quote(prompt)
    return base


def tab_color_escape(rgb):
    """iTerm's proprietary tab-color escape as REAL bytes — for writing straight to a tty (how the
    lead's own tab gets painted). rgb = (r, g, b), 0-255 each."""
    r, g, b = rgb
    return ("\033]6;1;bg;red;brightness;%d\a"
            "\033]6;1;bg;green;brightness;%d\a"
            "\033]6;1;bg;blue;brightness;%d\a" % (int(r), int(g), int(b)))


def tab_color_printf(rgb):
    """The same escape as a printf-format literal (backslash sequences, no raw control bytes) —
    for embedding `printf '<this>'` inside a spawned shell command, which paints the executor's
    tab before `exec claude` takes over the tty."""
    return tab_color_escape(rgb).replace("\033", "\\033").replace("\a", "\\a")


def notify_via_tty(tty_path, title, body):
    """Write iTerm's OSC 777 'notify' escape straight to a session's tty device — the same channel
    tab_color_escape uses, which is why it works from a controlling-terminal-less process (a hook
    has no /dev/tty; this opens the target tty BY PATH instead, exactly like tty_by_id's caller
    already does for tab colors). Confirmed live: clicking the resulting native macOS notification
    focuses iTerm on the exact posting session (window+tab), even when that session isn't the
    frontmost one and the write came from a fully detached subprocess (see
    docs/async-rewake-findings.md). Requires macOS's own per-app notification permission for iTerm
    to be granted (System Settings → Notifications) — iTerm's in-app "Terminal may post
    notifications" profile setting is a separate, additional gate.

    Best-effort and NEVER raises: strips \\033/\\007/newlines from title/body (would otherwise break
    or truncate the escape sequence) and swallows any write failure (stale tty path, permission,
    closed session) so callers can always fall through to their next notification tier. Returns
    True/False for the write itself succeeding — NOT proof the banner rendered (that's inherently
    unobservable from here)."""
    def clean(s):
        return (s or "").replace("\033", "").replace("\007", "").replace("\n", " ").replace("\r", " ")
    try:
        with open(tty_path, "w") as f:
            f.write("\033]777;notify;%s;%s\007" % (clean(title), clean(body)))
        return True
    except Exception:
        return False


def _for_session_by_id(uuid, action):
    """AppleScript fragment: walk windows → tabs → sessions and, on the session whose iTerm id
    equals `uuid`, run `action` (which must `return`). Shared by rename_by_id and tty_by_id."""
    return (
        f'tell application "{ITERM_APP_NAME}"\n'
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      repeat with s in sessions of t\n"
        f'        if (id of s) is "{osa(uuid)}" then\n'
        f"{action}"
        "        end if\n"
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
        "end tell\n"
    )


def tty_by_id(iterm_id):
    """The /dev/ttysNNN of the iTerm session whose id matches `iterm_id` ($TERM_SESSION_ID,
    "w#t#p#:UUID"), or None. Used to paint the LEAD's tab: hook/CLI processes have no controlling
    terminal (/dev/tty is 'device not configured' there — confirmed), so the escape must be written
    to the session's tty device found via AppleScript."""
    if not iterm_id:
        return None
    uuid = iterm_id.split(":")[-1]
    script = _for_session_by_id(uuid, "          return tty of s\n") + 'return ""'
    r = run_osascript(script, timeout=5)
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out.startswith("/dev/") else None


def title_by_id(iterm_id):
    """The CURRENT title of the iTerm session whose id matches `iterm_id` ("w#t#p#:UUID"), or None.

    The identity-aware counterpart to live_session_names(): that one answers "does ANY live tab
    carry this title", which is the wrong question whenever two tabs can legitimately share a label
    — a handoff predecessor/successor pair both titled "[Lead] <project>" is the case backlog §2 is
    about, where the successor's label loop read the PREDECESSOR's title as proof its own tab was
    fine and never re-titled itself.

    None means "no answer": the session id didn't resolve (tab closed), the osascript call failed,
    or the title came back empty. Callers must NOT read None as "the title is wrong" or as "the tab
    is fine" — it is unknown, and the safe response is to leave the tab alone rather than act on a
    guess about a tab you can't see."""
    if not iterm_id:
        return None
    uuid = iterm_id.split(":")[-1]
    script = _for_session_by_id(uuid, "          return name of s\n") + 'return ""'
    r = run_osascript(script, timeout=5)
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def pid_on_tty(tty_path, binary_suffix=CLAUDE_BIN):
    """The pid of the process named `binary_suffix` (default "claude") attached to `tty_path`
    (a "/dev/ttysNNN" from tty_by_id), or None. Used to SIGTERM a predecessor lead's claude process
    before closing its tab — iTerm pops a 'confirm close running process' dialog otherwise, which
    blocks osascript indefinitely (see close()'s docstring)."""
    if not tty_path:
        return None
    tty_name = tty_path.removeprefix("/dev/")
    try:
        r = subprocess.run(["ps", "-axo", "pid=,tty=,comm="], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid_s, tty, comm = parts
        if tty == tty_name and comm.endswith(binary_suffix):
            try:
                return int(pid_s)
            except ValueError:
                continue
    return None


def _create_target_block(lead_handle=None, layout="tab"):
    """AppleScript fragment binding `targetSession` to a freshly created tab or pane, and setting
    `targetFound` true on every path that binds it. With no `lead_handle`, same as always: new tab
    in the current window (or a new window if none exist) — `layout` is ignored in this branch since
    there's no lead session to split.

    TARGET-OR-ABORT (2026-08-01 spawn-misfire incident, ~/.relay-tasks/
    incident-spawn-misfire-2026-08-01.md): every binding here must be to the object CREATE RETURNED,
    never to a focus-dependent expression re-read after creation. The pre-fix code did
    `tell <window> to create tab …` and then, as a SEPARATE statement,
    `set targetSession to current session of <window>` — "current session" is whatever tab is
    SELECTED at the moment that statement runs, so a human cycling tabs between the two statements
    silently redirected `targetSession` at an existing tab, and the whole bootstrap payload
    (`… && exec claude --session-id …`) was typed into it. That is exactly what happened to the
    d2cengine_refactor lead's spawn of `alerts-badge`. `create tab with default profile` RETURNS the
    new tab, so binding through that return value is immune to focus moving.

    With `lead_handle` (the spawning lead's own iTerm session id), first try to find the window
    AND session matching it — same session-id walk as `_for_session_by_id` — then:
    - `layout="tab"` (default): create the new tab in the lead's window (`leadWindow`).
    - `layout="pane"`: split the lead's own SESSION (`leadSession`) vertically instead — the
      executor lands as a pane inside the lead's tab, not a separate tab. Confirmed live
      (osascript probe on this machine): `tell leadSession to set targetSession to (split
      vertically with default profile)` returns the new pane's session directly, and normal
      `write text`/`/rename` against it work exactly like a tab's session (a pane IS a session —
      title-matching, is_alive, send all keep working unchanged).
    `foundLeadWindow` is a plain boolean flag (not `try`/`on error`, since a no-match walk isn't
    an AppleScript error — it just completes without setting anything) so the fallback below
    still fires whenever the lead's session can't be located (unowned spawns, lead not in iTerm,
    lead's tab closed between arm and spawn, etc).

    NOTE (investigated live via osascript probes on this machine): iTerm2's AppleScript `move`
    command and `set index of tab` do NOT actually reposition tabs — `move tab to before/after
    otherTab` returns success but the tab order is unchanged, and `set index of tab to N` throws
    outright. So "adjacent to the lead's tab" as a separate TAB is NOT achievable here;
    same-window-at-end is the best available tab placement. `layout="pane"` sidesteps this
    entirely — a split pane has no "position in the tab bar" to fight over, it's just next to the
    lead's own pane by construction.
    """
    fresh_window = (
        "    set newWindow to (create window with default profile)\n"
        "    set targetSession to current session of newWindow\n"
        "    set targetFound to true\n"
    )
    fresh_tab_here = (
        "    tell current window to set newTab to (create tab with default profile)\n"
        "    set targetSession to current session of newTab\n"
        "    set targetFound to true\n"
    )
    if not lead_handle:
        return (
            "  if (count of windows) is 0 then\n"
            f"{fresh_window}"
            "  else\n"
            f"{fresh_tab_here}"
            "  end if\n"
        )
    uuid = osa(lead_handle.split(":")[-1])
    if layout == "pane":
        create_in_lead = (
            "    tell leadSession to set targetSession to (split vertically with default profile)\n"
            "    set targetFound to true\n"
        )
    else:
        create_in_lead = (
            "    tell leadWindow to set newTab to (create tab with default profile)\n"
            "    set targetSession to current session of newTab\n"
            "    set targetFound to true\n"
        )
    return (
        "  set foundLeadWindow to false\n"
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      repeat with s in sessions of t\n"
        f'        if (id of s) is "{uuid}" then\n'
        "          set leadWindow to w\n"
        "          set leadSession to s\n"
        "          set foundLeadWindow to true\n"
        "        end if\n"
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
        "  if foundLeadWindow then\n"
        f"{create_in_lead}"
        "  else if (count of windows) is 0 then\n"
        f"{fresh_window}"
        "  else\n"
        f"{fresh_tab_here}"
        "  end if\n"
    )


def _target_by_session_id_block(uuid):
    """AppleScript fragment binding `targetSession` to the session whose id already equals `uuid`
    — used when `iterm_pyapi.try_create_adjacent_tab` has ALREADY created the tab at the right
    index via the Python API; this fragment just hands the resulting session over to the existing
    write-text/rename AppleScript machinery (confirmed live: Python API session ids and
    AppleScript's `id of session` are the same UUID space, so this lookup always matches).

    Sets `targetFound` only when the walk actually matches. Pre-#24 this fragment left
    `targetSession` UNBOUND on a miss (pyapi tab already closed, id-space surprise, …) while the
    caller typed the payload regardless; the caller now aborts on an unbound target — see spawn().

    ID-FORMAT NORMALIZATION (#25, incident audit item 1a): the walk compares against `id of session`,
    which is the BARE UUID. Every id that reaches relay from a shell — `$ITERM_SESSION_ID`, hence
    every stored `iterm_session` handle — is the `w#t#p#:UUID` form instead. Feeding a handle-form id
    to this walk would compare `"w1t9p0:UUID"` against `"UUID"`, miss on every session, and (pre-#24)
    fall through to the frontmost tab. `try_create_adjacent_tab` happens to return the bare form
    today, so this normalization is belt-and-braces rather than a live bug — but it is one line, and
    the failure it prevents is the whole incident. Case is not normalized here on purpose:
    AppleScript's `is` comparison on strings is case-insensitive by default."""
    uuid = str(uuid or "").split(":")[-1]
    return (
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      repeat with s in sessions of t\n"
        f'        if (id of s) is "{osa(uuid)}" then\n'
        "          set targetSession to s\n"
        "          set targetFound to true\n"
        "        end if\n"
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
    )


# (History — the inline guarded-bootstrap era, #24 Ask 3 then #25's wedge-proof reshape: the full
# bootstrap used to be TYPED, wrapped in a quote-free session-id conditional built from _GUARD_*
# segments joined around the runtime sid. It ended with §15b below: typing ~1KB of fragile text
# into a fresh tab proved unreliable regardless of shape — observed truncation mid-payload AND
# head-of-line corruption. The guard's properties survive in the file protocol: same runtime-sid
# comparison, same inert-on-mismatch, same exit-0, and the typed line is now too short and
# quote-free for any truncation to wedge. tests/test_backends.py keeps the old inline shape as a
# literal in its historical repro.)

# --- §15b (2026-08-02, field-blocking): the bootstrap is no longer TYPED at all -------------------
# Two live corruption modes killed the inline protocol, both on the CORRECT freshly-created tab:
#   - mid-payload truncation at a consistent ~1KB offset (inside the single-quoted Task prompt),
#     leaving an unterminated quote and a `quote>`-wedged tab (`cutoff-jump`, twice);
#   - head-of-payload corruption (`_relay_sid=` arrived as `sp_relay_sid=`), which made the guard
#     read an empty variable and no-op the launch in its own intended tab (rl-spawn resume).
# Both are symptoms of typing long fragile text into a shell that hasn't settled. The structural
# fix: write the COMPLETE bootstrap (guard, cd, colors, pidfile, exec claude with the full prompt,
# any length) to a per-session file, and type only one short, quote-free launch line:
#     sh <session dir>/bootstrap.sh <runtime session id>
# The receiving-tab guard moves INSIDE the file, comparing $ITERM_SESSION_ID against $1 — and $1 is
# spliced by AppleScript from `id of targetSession` at send time, so the guard's id and the typed-
# into session remain the same value by construction (#24's property, preserved). Truncate the
# typed line anywhere and the worst case is command-not-found or "sh: can't open" — nothing
# quote-open, nothing destructive; a truncated trailing $1 makes the guard mismatch and the file
# no-op with the notice. A sacrificial empty write precedes the launch line so head-eating consumes
# a blank line instead of the command. The file is also the exact durable record of what a launch
# ran — inspectable after any incident, unlike keystrokes.
BOOTSTRAP_FILENAME = "bootstrap.sh"


def bootstrap_file_content(cmd):
    """The full contents of the per-launch bootstrap file. Quotes and blocks are fine here — this
    text is never typed into a terminal, so the wedge-proof constraints of the inline era don't
    apply; only the SHORT launch line is typed, and that stays quote-free."""
    return (
        "#!/bin/sh\n"
        "# relay bootstrap — written per launch by relay (see scripts/iterm.py, §15b).\n"
        "# Invoked as: sh <this file> <intended iTerm session id>. A copy run anywhere else\n"
        "# (or with a truncated/missing id) prints one line and exits 0 — inert by construction.\n"
        '_relay_sid="${ITERM_SESSION_ID:-$TERM_SESSION_ID}"\n'
        'if [ "x${_relay_sid##*:}" != "x$1" ] || [ "x$1" = "x" ]; then\n'
        '  echo "relay: ignored a mis-delivered bootstrap meant for session ${1:-<missing>}"\n'
        "  exit 0\n"
        "fi\n"
        f"{cmd}\n"
    )


def write_bootstrap_file(pidfile, cmd):
    """Write the bootstrap next to the session's pidfile (the per-session dir relay already owns),
    mode 0700, overwritten on every launch. Returns the file's absolute path."""
    path = os.path.join(os.path.dirname(os.path.abspath(pidfile)), BOOTSTRAP_FILENAME)
    with open(path, "w") as f:
        f.write(bootstrap_file_content(cmd))
    os.chmod(path, 0o700)
    return path


def _match_session_block(label, action):
    """AppleScript fragment: walk windows -> tabs -> sessions, and on the first session whose
    name matches `label` (bounded match), run `action`, set matched to true, then return."""
    label_e, sep_e = osa(label), osa(TAB_TITLE_SEP)
    return (
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      repeat with s in sessions of t\n"
        f'        if (name of s contains " {label_e}{sep_e}") or (name of s starts with "{label_e}{sep_e}") '
        f'or (name of s ends with " {label_e}") or (name of s is equal to "{label_e}") '
        f'or (name of s contains " {label_e} (") or (name of s starts with "{label_e} (") then\n'
        f"{action}"
        "          set matched to true\n"
        "          return matched\n"
        "        end if\n"
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
    )


def spawn(cwd, prompt, label, pidfile, model=None, skip_perms=False, rename_delay=1.5, env_prefix="",
          iterm_id_file=None, session_uuid=None, resume_id=None, tab_color=None, lead_handle=None,
          layout="tab", settings_file=None, mcp_flags=None, agent_flags=None):
    """Open a new iTerm tab (or pane), cd into `cwd`, launch `claude [--model X] <prompt>`, then
    (after a delay for claude to finish starting) send `/rename <label>` into the SAME session — one
    AppleScript call that resolves the target ONCE to a session id and then addresses every write by
    that id, so nothing that happens to focus or window order in between can redirect the payload.

    TARGET-OR-ABORT + INERT PAYLOAD (2026-08-01/02 spawn-misfire incident, write-up at
    ~/.relay-tasks/incident-spawn-misfire-2026-08-01.md — a lead's bootstrap, `exec claude
    --session-id …` and all, was typed into an unrelated live tab while the human cycled tabs).
    This took two rounds, and the first round's reasoning was incomplete:
      1. (#24) every target binding is to the object creation RETURNED (`_create_target_block`) —
         never re-read from the focus-dependent `current session of <window>`;
      2. (#25) but binding is not addressing. A specifier like `session N of tab M of window K` is
         POSITIONAL, so holding it across a reorder still wrote to the wrong session — proven by the
         `cutoff-jump` misfire, where the payload's guard carried the CORRECT id. Every write now
         re-resolves BY ID inside the matching loop iteration, the walk doubles as the existence
         re-check, and a miss returns a verdict WITHOUT typing. No focus-dependent expression
         reaches the send path at all;
      3. the frontmost tab's title at send time is captured and returned, so a caller reporting a
         failed launch can tell the human which tab to inspect;
      4. (§15b) the full bootstrap lives in a per-session FILE (write_bootstrap_file) and only a
         short quote-free `sh <file> <sid>` line is typed — the file's guard compares the receiving
         tab's own $ITERM_SESSION_ID against the runtime sid in $1, so a stray or truncated copy is
         a no-op even in a plain shell, and NO truncation point of the typed line can wedge anything.

    Returns a dict — `{"ok": bool, "reason": str, "session_id": str|None, "front_title": str|None}`
    — see `_read_spawn_outcome`. Never raises; `ok=False` means the launch did not happen, and
    `reason` distinguishes "nothing was typed" from "osascript failed, state indeterminate".

    Writes the launched process's PID to `pidfile` via `$$` + `exec` (the shell's PID becomes
    claude's PID after exec replaces the process image, so no race with backgrounding/job
    control, and the tab/pane stays fully interactive). PID capture and the tab-color printf are
    identical whether `targetSession` ends up being a tab's session or a split pane's session — a
    pane IS a session, so every downstream mechanism (pidfile, iterm_id_file, /rename, tab_color)
    is unchanged by `layout`.

    `tab_color` (r, g, b) paints the tab via a printf'd escape before exec — executors inherit
    their lead's color so related tabs group visually.

    `lead_handle` ($TERM_SESSION_ID of the spawning lead's own iTerm session, if known): when
    given, the new tab/pane is created in the LEAD'S window instead of whatever window happens to
    be current — best-effort, falls back to today's current-window behavior if the lead's session
    can't be located (unowned spawns, lead not in iTerm, any lookup miss). AppleScript alone can't
    place a new TAB truly adjacent to the lead's (see `_create_target_block`'s note — `move tab`/
    `set index of tab` are no-ops/errors on this machine), so for `layout="tab"` this first tries
    `iterm_pyapi.try_create_adjacent_tab` (iTerm2's Python API, which DOES support index-placed
    tab creation) and, only if that succeeds, hands the resulting session to the existing
    AppleScript write-text/rename machinery via `_target_by_session_id_block`. Any failure there
    (package not installed, API not enabled, timeout, anything) silently falls back to
    `_create_target_block`'s same-window-at-end placement — byte-identical to the pre-Python-API
    behavior, never blocking or failing the spawn over a cosmetic nicety.

    `layout` ("tab" default, or "pane"): with `layout="pane"` AND a resolvable `lead_handle`, the
    executor is opened as a split pane inside the LEAD'S OWN tab instead of a new tab — see
    `_create_target_block`. `layout="pane"` without a resolvable `lead_handle` degrades to the
    same tab-creation fallback as `layout="tab"` (never fails a spawn over layout preference).

    `env_prefix` is a test-only hook (default "", no effect on real usage): a shell fragment
    prepended before the PID-capture step, e.g. `'PATH="/tmp/fakebin:$PATH" '` to scope a stub
    `claude` binary to just this one spawned command, without touching the real system PATH.

    `settings_file`: passed straight through to build_claude_cmd's `--settings` — how an executor
    gets ANY hooks at all (see that function's docstring).
    """
    base = build_claude_cmd(prompt, model=model, skip_perms=skip_perms,
                            session_uuid=session_uuid, resume_id=resume_id,
                            settings_file=settings_file, mcp_flags=mcp_flags,
                            agent_flags=agent_flags)
    # Record the new session's own iTerm id (ITERM_SESSION_ID, set in the interactive iTerm shell;
    # fall back to TERM_SESSION_ID) into a file BEFORE exec replaces the shell — the handle used by
    # the rename-retry (_ensure_tab_label) and the lead tab-color path. Best-effort; empty var →
    # empty file. NOTE: `relay focus` does NOT use this — it title-matches.
    capture = ""
    if iterm_id_file:
        capture = f' && echo "${{ITERM_SESSION_ID:-$TERM_SESSION_ID}}" > {shlex.quote(iterm_id_file)}'
    color_part = f"printf '{tab_color_printf(tab_color)}' && " if tab_color else ""
    cmd = (f"cd {shlex.quote(cwd)} && {color_part}{env_prefix}echo $$ > {shlex.quote(pidfile)}{capture} "
           f"&& exec {base}")
    # §15b: the full bootstrap goes to a FILE; only a short quote-free launch line is typed (see
    # bootstrap_file_content's block comment for the two field corruption modes this closes). The
    # runtime `sid` is appended by AppleScript as $1 so the file's guard checks the same value the
    # send targeted. relay-owned session dirs have no spaces, so the path needs no quoting — and it
    # must not get any: a quote in the typed line is exactly the wedge risk the file protocol removes.
    boot_path = write_bootstrap_file(pidfile, cmd)
    payload_expr = f'"sh {osa(boot_path)} " & sid'
    rename_e = osa("/rename " + label)
    # Only worth attempting adjacency for a separate TAB — a "pane" layout is inherently adjacent
    # (it's split off the lead's own session), no placement problem to solve.
    pyapi_session_id = (
        iterm_pyapi.try_create_adjacent_tab(lead_handle) if layout == "tab" and lead_handle else None
    )
    target_block = (
        _target_by_session_id_block(pyapi_session_id) if pyapi_session_id
        else _create_target_block(lead_handle, layout)
    )
    # TARGET-OR-ABORT, addressed BY ID AT WRITE TIME (#24 established the first half; #25 the second).
    #
    # #24 bound `targetSession` to what creation returned, re-checked it, then wrote through that
    # held reference. The 2026-08-02 `cutoff-jump` misfire proved that is still not enough: the
    # typed payload carried the CORRECT session id in its guard (so creation and resolution were
    # both right) yet the write landed in the frontmost tab. The reason is that an AppleScript
    # object specifier from `create tab`/`repeat with s in …` is POSITIONAL — `session N of tab M of
    # window K` — and iTerm orders `windows` front-to-back. Anything that reorders windows/tabs
    # between binding and writing (the human switching tabs, i.e. the incident's own trigger) leaves
    # the reference denoting a DIFFERENT session, while `id of targetSession`, read earlier, still
    # reports the right one. `send()`/`close()`/`focus()` never had this bug because they write
    # INSIDE the id match (`_for_session_by_id`); this is the same discipline applied to spawn.
    #
    # So: read `sid` immediately after binding (the one unavoidable deref, adjacent to creation),
    # then never touch `targetSession` again — every write re-resolves by id and happens in the same
    # loop iteration that matched it. The walk IS the re-check: no match, no write, verdict returned.
    # `/rename` re-resolves the same way after the delay, and is best-effort (the bootstrap is
    # already delivered by then; `_ensure_tab_label` owns label retries).
    write_payload = (
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      repeat with s in sessions of t\n"
        "        if (id of s) is sid then\n"
        # Sacrificial empty write first (§15b): the observed head-of-payload corruption eats the
        # first characters typed into a just-created tab, so let it eat a blank line — the real
        # launch line is the SECOND write, after the shell has had a beat to settle.
        '          tell s to write text ""\n'
        "          delay 0.2\n"
        f"          tell s to write text ({payload_expr})\n"
        "          set didWrite to true\n"
        "        end if\n"
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
    )
    write_rename = (
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      repeat with s in sessions of t\n"
        "        if (id of s) is sid then\n"
        f'          tell s to write text "{rename_e}"\n'
        "        end if\n"
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
    )
    script = (
        'set frontTitle to ""\n'
        "set targetFound to false\n"
        "set didWrite to false\n"
        f'tell application "{ITERM_APP_NAME}"\n'
        "  activate\n"
        f"{target_block}"
        '  if not targetFound then return "NOTARGET" & linefeed & "" & linefeed & frontTitle\n'
        "  set sid to id of targetSession\n"
        "  try\n"
        "    set frontTitle to name of current session of current window\n"
        "  end try\n"
        f"{write_payload}"
        '  if not didWrite then return "GONE" & linefeed & sid & linefeed & frontTitle\n'
        f"  delay {rename_delay}\n"
        f"{write_rename}"
        '  return "OK" & linefeed & sid & linefeed & frontTitle\n'
        "end tell"
    )
    r = run_osascript(script, timeout=rename_delay + 5)
    return _read_spawn_outcome(r)


def _read_spawn_outcome(r):
    """Parse spawn()'s three-line AppleScript verdict into the dict spawn() returns:
    `{"ok", "reason", "session_id", "front_title"}`.

    `ok` is True only for a payload typed into a re-checked target session. Reasons:
    - "ok"            — typed into `session_id`; `front_title` is what was frontmost at send time.
    - "no-target"     — tab creation/lookup never bound a session. NOTHING was typed.
    - "target-gone"   — the bound session vanished before the send. NOTHING was typed.
    - "script-failed" — osascript itself errored/timed out; since every abort path returns before the
                        first `write text`, a failure here still means nothing was typed UNLESS it
                        happened mid-send, which callers must report as indeterminate.
    Callers use `front_title` for the incident's Ask 2 (tell the human which tab to inspect)."""
    if r.returncode != 0:
        return {"ok": False, "reason": "script-failed", "session_id": None, "front_title": None}
    lines = (r.stdout or "").splitlines()
    verdict = lines[0].strip() if lines else ""
    sid = lines[1].strip() if len(lines) > 1 else ""
    front = lines[2].strip() if len(lines) > 2 else ""
    reason = {"OK": "ok", "NOTARGET": "no-target", "GONE": "target-gone"}.get(verdict, "script-failed")
    return {"ok": reason == "ok", "reason": reason,
            "session_id": sid or None, "front_title": front or None}


def send(label, prompt, handle=None, pid=None):
    """Write `prompt` into the existing live tab matched by `handle` (unique iTerm session id) when
    given, else by `label` (bounded title match). id-based first, same reasoning as close(): once
    Claude Code's own OSC titling clobbers a tab's title, title-match addressing misfires — this is
    what caused an earlier `relay send` to report "tab was gone — resumed" on a live executor.
    Falls back to the title match when `handle` is empty or the id lookup finds nothing (e.g. a
    legacy/unowned session with no captured handle). pid: shared backend signature, unused here.
    Returns True if a match was found (by either path).

    The text and the Enter are sent as TWO separate writes: `write text` delivers text+newline in
    one burst, which Claude Code treats as a PASTE — the newline lands as a literal line break and
    the message sits unsubmitted in the input box (observed live: the executor silently waited for
    a human Enter). Writing the text with `newline NO`, then a bare newline after a beat, reads as
    a distinct Enter keypress and actually submits."""
    cmd_e = osa(prompt)  # raw text typed into the session, not a shell command
    action = (f'          tell s to write text "{cmd_e}" newline NO\n'
              "          delay 0.3\n"
              '          tell s to write text ""\n')
    if handle:
        uuid = handle.split(":")[-1]
        id_action = action + "          return true\n"
        script = _for_session_by_id(uuid, id_action) + "return false"
        r = run_osascript(script, timeout=5)
        if r.returncode == 0 and r.stdout.strip().lower() == "true":
            return True
    script = (
        "set matched to false\n"
        f'tell application "{ITERM_APP_NAME}"\n'
        f"{_match_session_block(label, action)}"
        "end tell\n"
        "return matched"
    )
    r = run_osascript(script, timeout=5)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def rename_by_id(iterm_id, new_name):
    """Give the lead's OWN tab a stable, relay-controlled title so `relay focus` can find it by title
    (exactly like executors). Writes `/rename <new_name>` into the session whose iTerm id matches
    `iterm_id` ($TERM_SESSION_ID, "w#t#p#:UUID" — we match the UUID against iTerm's `id of session`).
    The lead is usually mid-turn when this runs, so the /rename queues as its next input (Claude Code
    buffers it) — harmless, one-time at arm. Best-effort; returns True if a session matched."""
    if not iterm_id:
        return False
    uuid = iterm_id.split(":")[-1]  # "w1t8p0:UUID" -> "UUID" (iTerm's session id)
    cmd_e = osa("/rename " + new_name)
    action = f'          tell s to write text "{cmd_e}"\n          return true\n'
    script = _for_session_by_id(uuid, action) + "return false"
    r = run_osascript(script, timeout=5)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def close(label, handle=None, pid=None):
    """Close the iTerm tab/session matched by `handle` (unique iTerm session id) when given, else by
    `label` (bounded title match). id-based matching first because a title CAN be shared by two live
    tabs (e.g. a handoff predecessor/successor pair, both titled "[Lead] <project>") — closing by
    title alone risks closing the wrong one. Falls back to the title match when `handle` is empty or
    the id lookup finds nothing (e.g. the tab already closed itself).

    The caller should kill the session's process FIRST so iTerm doesn't pop a 'confirm close running
    process' dialog (which would block osascript). The executor's report is already on disk, so
    closing loses nothing. Returns True if a session matched and the close command ran.
    pid: shared backend signature, unused here (iTerm addresses by title/id, not pid)."""
    if handle:
        uuid = handle.split(":")[-1]
        script = _for_session_by_id(uuid, "          tell s to close\n          return true\n") + "return false"
        r = run_osascript(script, timeout=5)
        if r.returncode == 0 and r.stdout.strip().lower() == "true":
            return True
    action = "          tell s to close\n"
    script = (
        "set matched to false\n"
        f'tell application "{ITERM_APP_NAME}"\n'
        f"{_match_session_block(label, action)}"
        "end tell\n"
        "return matched"
    )
    r = run_osascript(script, timeout=5)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def focus(label, handle=None, pid=None):
    """Jump to the live iTerm session matched by `handle` (unique iTerm session id) when given, else
    by `label` (bounded title match) — same id-first/title-fallback shape as send()/close(), for the
    same reason: a title clobbered by Claude Code's own OSC titling misdirects title-match lookups.
    Either path: `activate` iTerm, then `tell w to select` + `select t` + `tell s to select` —
    select the WINDOW (brings it to front), the TAB, AND the exact SESSION/PANE within it. This is
    the reliable mechanism proven in claude-sessions-swiftbar (ccsessions); the same authorized
    osascript path as spawn/send. (The iterm2:///reveal URL scheme was tried and dropped: `open`
    always exits 0 so it reported false success but didn't actually switch.) `tell s to select` was
    confirmed live (osascript probe on this machine) to genuinely shift the active PANE within a
    split tab — not just a no-op — so a notification click lands on the exact executor pane, not
    just its tab (for tab-layout executors, `s` is the tab's only session, so this is a harmless
    no-op). Returns True if a tab matched (by either path)."""
    if handle:
        uuid = handle.split(":")[-1]
        id_action = ("          tell w to select\n          select t\n          tell s to select\n"
                     "          return true\n")
        script = (
            f'tell application "{ITERM_APP_NAME}" to activate\n'
            + _for_session_by_id(uuid, id_action)
            + "return false"
        )
        r = run_osascript(script, timeout=5)
        if r.returncode == 0 and r.stdout.strip().lower() == "true":
            return True
    action = "          tell w to select\n          select t\n          tell s to select\n"
    script = (
        "set matched to false\n"
        f'tell application "{ITERM_APP_NAME}"\n'
        "  activate\n"
        f"{_match_session_block(label, action)}"
        "end tell\n"
        "return matched"
    )
    r = run_osascript(script, timeout=5)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"
