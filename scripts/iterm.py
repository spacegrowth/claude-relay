"""
iTerm2 backend for claude-relay: spawn a new tab running `claude` seeded with a prompt,
send a follow-up prompt into an existing live tab, and check aliveness.

Vendored/adapted from ~/.swiftbar/.lib/ccsessions/app.py (ITermBackend) — same title-matching
rules, scoped to just what relay needs. Addressing is inherently by mutable tab title (owned by
Claude Code's own OSC title updates), which is why the caller (relay) treats the recorded PID as
the source of truth for aliveness and the title match as a secondary confirmation only.
"""
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


def is_alive(label, handle=None, pid=None):
    # handle/pid are part of the shared backend signature (Terminal.app addresses by window id);
    # iTerm addresses by title, so they're unused here.
    return title_is_live(label, live_session_names())


def build_claude_cmd(prompt, model=None, skip_perms=False, session_uuid=None, resume_id=None):
    """The `claude …` invocation both backends launch. resume_id reopens an existing conversation
    (no --model — the session already has one); otherwise a fresh session, optionally pinned to
    session_uuid so relay can `--resume` it later without scraping transcripts."""
    base = CLAUDE_BIN + (" --dangerously-skip-permissions" if skip_perms else "")
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


def _create_target_block(lead_handle=None, layout="tab"):
    """AppleScript fragment binding `targetSession` to a freshly created tab or pane. With no
    `lead_handle`, same as always: new tab in the current window (or a new window if none exist) —
    `layout` is ignored in this branch since there's no lead session to split.

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
    if not lead_handle:
        return (
            "  if (count of windows) is 0 then\n"
            "    set newWindow to (create window with default profile)\n"
            "    set targetSession to current session of newWindow\n"
            "  else\n"
            "    tell current window to create tab with default profile\n"
            "    set targetSession to current session of current window\n"
            "  end if\n"
        )
    uuid = osa(lead_handle.split(":")[-1])
    if layout == "pane":
        create_in_lead = "    tell leadSession to set targetSession to (split vertically with default profile)\n"
    else:
        create_in_lead = (
            "    tell leadWindow to create tab with default profile\n"
            "    set targetSession to current session of leadWindow\n"
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
        "    set newWindow to (create window with default profile)\n"
        "    set targetSession to current session of newWindow\n"
        "  else\n"
        "    tell current window to create tab with default profile\n"
        "    set targetSession to current session of current window\n"
        "  end if\n"
    )


def _target_by_session_id_block(uuid):
    """AppleScript fragment binding `targetSession` to the session whose id already equals `uuid`
    — used when `iterm_pyapi.try_create_adjacent_tab` has ALREADY created the tab at the right
    index via the Python API; this fragment just hands the resulting session over to the existing
    write-text/rename AppleScript machinery (confirmed live: Python API session ids and
    AppleScript's `id of session` are the same UUID space, so this lookup always matches)."""
    return (
        "  repeat with w in windows\n"
        "    repeat with t in tabs of w\n"
        "      repeat with s in sessions of t\n"
        f'        if (id of s) is "{osa(uuid)}" then\n'
        "          set targetSession to s\n"
        "        end if\n"
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
    )


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
          layout="tab"):
    """Open a new iTerm tab (or pane), cd into `cwd`, launch `claude [--model X] <prompt>`, then
    (after a delay for claude to finish starting) send `/rename <label>` into the SAME session —
    one AppleScript call holding a single `targetSession` reference throughout, so there's no race
    with "current session" shifting if another spawn happens in between (two separate osascript
    calls relying on "current session of current window" staying correct would have that race).

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
    """
    base = build_claude_cmd(prompt, model=model, skip_perms=skip_perms,
                            session_uuid=session_uuid, resume_id=resume_id)
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
    cmd_e = osa(cmd)
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
    script = (
        f'tell application "{ITERM_APP_NAME}"\n'
        "  activate\n"
        f"{target_block}"
        "  tell targetSession\n"
        f'    write text "{cmd_e}"\n'
        f"    delay {rename_delay}\n"
        f'    write text "{rename_e}"\n'
        "  end tell\n"
        "end tell"
    )
    run_osascript(script, timeout=rename_delay + 5)


def send(label, prompt, handle=None, pid=None):
    """Write `prompt` into the existing live tab matched by `label` (bounded title match) — no
    new process, preserves that session's conversation. Returns True if a match was found.
    handle/pid: shared backend signature, unused here (iTerm addresses by title).

    The text and the Enter are sent as TWO separate writes: `write text` delivers text+newline in
    one burst, which Claude Code treats as a PASTE — the newline lands as a literal line break and
    the message sits unsubmitted in the input box (observed live: the executor silently waited for
    a human Enter). Writing the text with `newline NO`, then a bare newline after a beat, reads as
    a distinct Enter keypress and actually submits."""
    cmd_e = osa(prompt)  # raw text typed into the session, not a shell command
    action = (f'          tell s to write text "{cmd_e}" newline NO\n'
              "          delay 0.3\n"
              '          tell s to write text ""\n')
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
    """Close the iTerm tab/session matched by `label` (bounded title match). The caller should kill
    the session's process FIRST so iTerm doesn't pop a 'confirm close running process' dialog (which
    would block osascript). The executor's report is already on disk, so closing loses nothing.
    Returns True if a session matched and the close command ran.
    handle/pid: shared backend signature, unused here (iTerm addresses by title)."""
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
    """Jump to the live iTerm session matched by `label` (bounded title match): `activate` iTerm,
    then `tell w to select` + `select t` + `tell s to select` — select the WINDOW (brings it to
    front), the TAB, AND the exact SESSION/PANE within it. This is the reliable mechanism proven in
    claude-sessions-swiftbar (ccsessions); the same authorized osascript path as spawn/send. (The
    iterm2:///reveal URL scheme was tried and dropped: `open` always exits 0 so it reported false
    success but didn't actually switch.) `tell s to select` was confirmed live (osascript probe on
    this machine) to genuinely shift the active PANE within a split tab — not just a no-op — so a
    notification click lands on the exact executor pane, not just its tab (for tab-layout
    executors, `s` is the tab's only session, so this is a harmless no-op). Returns True if a tab
    matched."""
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
