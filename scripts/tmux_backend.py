"""
tmux backend for claude-relay — the same operations as scripts/iterm.py and scripts/terminal_app.py,
for the place neither of those exists: a lead running `claude` inside tmux on a Linux box over SSH
(and, identically, inside tmux on a Mac). Selected by $RELAY_TERMINAL=tmux, config
`terminal_app: tmux`, or automatically when $TMUX is set (see scripts/backend.py).

ADDRESSING: by tmux PANE ID, never by title. The handle written to `iterm_id_file` (and stored as a
session's / lead marker's `iterm_session`) is "tmux:%N" — `#{pane_id}`, unique for the server's
lifetime. `_pane(handle)` returns "%N" or None, and exactly like terminal_app every id-taking
operation returns False (running NOTHING) for an absent or foreign handle (an iTerm "w0t0p0:UUID",
a Terminal "twid:N"). There is no title fallback anywhere: tmux window names are relay-owned here
(`automatic-rename off`), but two windows can legitimately share one (a handoff pair), so a name
match would be exactly the §2 ambiguity the id discipline exists to avoid.

EVERY tmux call goes through `_tmux(args, timeout)` — an argv list to `subprocess.run`, never
`shell=True`, never a command string — so tests mock one seam. `$RELAY_TMUX_SOCKET`, when set,
adds `-L <name>` to every call (a user's named server; the e2e test's private one). Inside a tmux
pane without it, `tmux` finds its server through $TMUX on its own. tmux treats an argv word ENDING
in ';' as a command separator (verified live: `-n 'lbl;'` split the command), so `_tmux` escapes a
trailing ';' as '\\;' on every word — a label or prompt can never smuggle in a second command.
Compatible with tmux >= 3.2 (Ubuntu 22.04/24.04 ship 3.2a/3.4): no 3.5+-only flags or formats.

SPAWN TARGETING — choice (a) from the tmux-backend packet, and why:
  The window/pane is created running the user's normal login shell (`new-window -P -F
  '#{pane_id}'`, which prints the new pane's id atomically from the create call itself), and the
  short quote-free launch line `sh <bootstrap file> %N` is then TYPED into that pane with
  `send-keys -t %N` — addressed BY ID, never "the active pane", so it has the same misfire immunity
  as iTerm's by-id write (`_for_session_by_id`): a human switching windows between the create and
  the type cannot redirect it. The alternative (b) — run `sh <bootstrap>` as the window's own
  command — was rejected because a window whose command exits closes itself, taking the error text
  of a failed launch with it; (a) leaves a shell behind exactly like an iTerm tab does, so a launch
  that dies prints its error where the human can read it, and `relay list` reads the pane as
  `stalled` (process gone, tab open), not silently `dead`.
  The bootstrap's receiving-pane guard compares `$TMUX_PANE` against `$1` (bootstrap_file_content's
  `sid_expr`): inside tmux on a Mac, $ITERM_SESSION_ID/$TERM_SESSION_ID are INHERITED from the
  outer iTerm shell, present and wrong, so the iTerm guard text would mis-fire here. tmux sets
  $TMUX_PANE in the pane before the shell starts, so a copy typed anywhere but %N is inert.

TYPED LINES are two writes with a scaled beat (iterm.py's block comment above ENTER_GAP_MIN — the
discipline is mandatory in every backend): `send-keys -l -- <text>` (literal: nothing in the text
is read as a key name), sleep `enter_gap(text)`, then a separate `send-keys Enter`.

What tmux shows the human: the STATUS BAR window name is relay's label (`rename-window` +
`automatic-rename off`), and a lead's tab color becomes that window's `window-status-style`
background, so a lead and its executors group in the status bar like iTerm tabs do. Claude Code's
own OSC title lands in `#{pane_title}`, which is what `title_by_id` reads.
"""
import os
import re
import shlex
import subprocess
import time

from iterm import (build_claude_cmd, write_bootstrap_file, enter_gap, pids_on_tty, pid_on_tty,
                   CLAUDE_BIN)  # noqa: F401 — pids_on_tty/pid_on_tty/CLAUDE_BIN re-exported: the
                                # ps-by-tty match is terminal-agnostic (/dev/pts/N → "pts/N")

NAME = "tmux"  # backend key (see scripts/backend.py)
HANDLE_PREFIX = "tmux:"
SID_EXPR = "$TMUX_PANE"   # the bootstrap guard's runtime pane id (see the module docstring)
_PANE_RE = re.compile(r"^%\d+$")
DEFAULT_TIMEOUT = 5
# `_tmux`'s stderr for a subprocess timeout — `wait` reads it to tell "nobody signalled within the
# interval" (the normal tick) from a real failure (no server, no tmux).
TIMED_OUT = "tmux timed out"


def _tmux(args, timeout=DEFAULT_TIMEOUT):
    """THE one tmux seam. Runs `tmux [-L $RELAY_TMUX_SOCKET] <args…>` as an argv list and returns a
    CompletedProcess — never raises: a missing `tmux` binary reads as returncode 127, a timeout as
    returncode 1, both with the reason in stderr. Every argv word ending in ';' is escaped (see the
    module docstring) so no caller-supplied text can split the command."""
    argv = ["tmux"]
    sock = os.environ.get("RELAY_TMUX_SOCKET")
    if sock:
        argv += ["-L", sock]
    for a in args:
        a = str(a)
        argv.append(a[:-1] + "\\;" if a.endswith(";") else a)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 1, "", TIMED_OUT)
    except (FileNotFoundError, OSError) as e:
        return subprocess.CompletedProcess(argv, 127, "", f"tmux unavailable: {e}")


def _ok(r):
    return r is not None and r.returncode == 0


def _pane(handle):
    """Pane id from a stored handle ("tmux:%12" → "%12"); None for absent/foreign handles."""
    h = str(handle or "")
    if h.startswith(HANDLE_PREFIX) and _PANE_RE.match(h[len(HANDLE_PREFIX):]):
        return h[len(HANDLE_PREFIX):]
    return None


def handle_for(pane):
    """The stored-handle form of a pane id ("%12" → "tmux:%12")."""
    return f"{HANDLE_PREFIX}{pane}"


def _display(pane, fmt):
    """`display-message -p -t <pane> <fmt>` → the stripped value, or None on any failure."""
    r = _tmux(["display-message", "-p", "-t", pane, fmt])
    out = (r.stdout or "").strip()
    return out if _ok(r) and out else None


def running():
    """A tmux server answers (`list-sessions` exits 0) — mirrors iterm/terminal_app `running()`."""
    return _ok(_tmux(["list-sessions"]))


def _type_line(pane, text):
    """Type `text` into `pane` as two writes with a scaled beat — the ONLY way this backend writes a
    line to a running session. True iff both writes landed (i.e. the pane exists)."""
    if not _ok(_tmux(["send-keys", "-t", pane, "-l", "--", text])):
        return False
    time.sleep(enter_gap(text))
    return _ok(_tmux(["send-keys", "-t", pane, "Enter"]))


def _hex(rgb):
    r, g, b = (max(0, min(255, int(v))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def paint_tab(handle, rgb):
    """Best-effort status-bar color for the window holding `handle`'s pane: `window-status-style
    bg=#rrggbb` — tmux's equivalent of an iTerm tab color, so a lead's executors group with it.
    Returns True only if tmux accepted it; never raises."""
    pane = _pane(handle)
    if not pane or not rgb:
        return False
    try:
        return _ok(_tmux(["set-option", "-w", "-t", pane, "window-status-style", f"bg={_hex(rgb)}"]))
    except Exception:
        return False


def _create_target(cwd, label, lead_pane, layout):
    """Create the executor's pane and return (pane_id|None, stderr). Placement, each step degrading
    to a plain `new-window` on any lookup miss — never failing the spawn over layout:
      - layout="pane" + a resolvable lead pane → `split-window -h` off the lead's own pane (side by
        side, like iTerm's `split vertically`);
      - layout="tab" + a resolvable lead pane → `new-window -a` right after the lead's window;
      - otherwise → `new-window` (tmux's current/most-recent session)."""
    fmt = ["-P", "-F", "#{pane_id}"]
    attempts = []
    if lead_pane and layout == "pane":
        attempts.append(["split-window", "-h", "-t", lead_pane] + fmt + ["-c", cwd])
    elif lead_pane:
        win = _display(lead_pane, "#{window_id}")
        if win:
            attempts.append(["new-window", "-a", "-t", win] + fmt + ["-n", label, "-c", cwd])
    attempts.append(["new-window"] + fmt + ["-n", label, "-c", cwd])
    err = ""
    for args in attempts:
        r = _tmux(args, timeout=10)
        pane = (r.stdout or "").strip()
        if _ok(r) and _PANE_RE.match(pane):
            return pane, ""
        err = (r.stderr or "").strip() or f"tmux {args[0]} returned {pane!r}"
    return None, err


def _fail(reason, err, session_id=None):
    return {"ok": False, "reason": reason, "session_id": session_id, "front_title": None,
            "error": err}


def spawn(cwd, prompt, label, pidfile, model=None, skip_perms=False, rename_delay=1.5,
          env_prefix="", iterm_id_file=None, session_uuid=None, resume_id=None, tab_color=None,
          lead_handle=None, layout="tab", settings_file=None, mcp_flags=None, agent_flags=None,
          effort=None):
    """New tmux window (or split pane) running the standard launch chain (cd → pidfile via $$ →
    exec claude), written to the per-session bootstrap FILE (§15b, `write_bootstrap_file`) with the
    `$TMUX_PANE` guard, then started by typing `sh <bootstrap> %N` into the pane BY ID (targeting
    choice (a), see the module docstring). Writes "tmux:%N" to `iterm_id_file` directly — the id is
    known from the create call, so the chain needs no `echo $TMUX_PANE` capture. Names the window
    `label` with automatic-rename off (the status bar stays relay-owned), paints it with
    `tab_color`, and after `rename_delay` types `/rename <label>` (two writes) so Claude Code's own
    title matches. `layout`/`lead_handle`: see `_create_target`. `env_prefix`/`settings_file`/
    `mcp_flags`/`agent_flags`/`effort`: same meaning as iterm.spawn.

    Returns the shared outcome dict `{"ok", "reason", "session_id", "front_title"}` (front_title is
    always None — nothing is typed into a focus-dependent target here, so there is no "which tab
    should the human inspect" to report). Any tmux failure → `{"ok": False, "reason":
    "script-failed", …, "error": <stderr>}`; a pane that vanished between create and type →
    "target-gone" (nothing was launched). Never raises."""
    try:
        base = build_claude_cmd(prompt, model=model, skip_perms=skip_perms,
                                session_uuid=session_uuid, resume_id=resume_id,
                                settings_file=settings_file, mcp_flags=mcp_flags,
                                agent_flags=agent_flags, effort=effort)
        cmd = (f"cd {shlex.quote(cwd)} && {env_prefix}echo $$ > {shlex.quote(pidfile)} "
               f"&& exec {base}")
        boot_path = write_bootstrap_file(pidfile, cmd, sid_expr=SID_EXPR)
    except Exception as e:
        return _fail("script-failed", f"bootstrap: {e}")
    pane, err = _create_target(cwd, label, _pane(lead_handle), layout)
    if not pane:
        return _fail("script-failed", err)
    handle = handle_for(pane)
    if iterm_id_file:
        try:
            with open(iterm_id_file, "w") as f:
                f.write(handle)
        except OSError:
            pass
    split = layout == "pane" and _pane(lead_handle) is not None and \
        _display(pane, "#{window_panes}") not in (None, "1")
    if not split:
        # A split pane shares the LEAD's window: renaming or recoloring "its" window would
        # clobber the lead's own status-bar entry, so only a window of its own gets either.
        _rename_window(pane, label)
        if tab_color:
            paint_tab(handle, tab_color)
    # Sacrificial Enter first (§15b): a just-created pane's shell may still be starting; a blank
    # line absorbs anything that eats the head of the input. Then the short launch line, by id.
    if not _ok(_tmux(["send-keys", "-t", pane, "Enter"])):
        return _fail("target-gone", "pane vanished before the launch line was typed", handle)
    time.sleep(0.2)
    if not _type_line(pane, f"sh {boot_path} {pane}"):
        return _fail("script-failed", "send-keys of the launch line failed", handle)
    time.sleep(rename_delay)
    _type_line(pane, "/rename " + label)   # best-effort — _ensure_tab_label owns label retries
    return {"ok": True, "reason": "ok", "session_id": handle, "front_title": None}


def _rename_window(pane, name):
    r1 = _tmux(["rename-window", "-t", pane, "--", name])
    r2 = _tmux(["set-option", "-w", "-t", pane, "automatic-rename", "off"])
    return _ok(r1) and _ok(r2)


def exists_by_id(handle):
    """HANDLE-ONLY liveness: the pane id is in `list-panes -a`. No title fallback (see the module
    docstring)."""
    pane = _pane(handle)
    if not pane:
        return False
    r = _tmux(["list-panes", "-a", "-F", "#{pane_id}"])
    return _ok(r) and pane in (r.stdout or "").split()


def is_alive(label, handle=None, pid=None):
    """The pane still exists. NO title fallback, deliberately: tmux window names are relay-owned,
    but two windows can share one (a handoff predecessor/successor pair), so a name match could
    answer about the wrong pane. The caller tracks the PROCESS separately via its recorded pid."""
    return exists_by_id(handle)


def send(label, prompt, handle=None, pid=None):
    """Type `prompt` into the session's pane (literal `send-keys -l`, scaled beat, separate Enter).
    True iff the pane exists and both writes landed. Handle-only — no title fallback."""
    pane = _pane(handle)
    if not pane:
        return False
    return _type_line(pane, prompt)


def press_enter(label, handle=None, pid=None):
    """One bare Enter into the pane — the retry when a typed line didn't submit."""
    pane = _pane(handle)
    if not pane:
        return False
    return _ok(_tmux(["send-keys", "-t", pane, "Enter"]))


def rename_by_id(handle, new_name):
    """Retitle BOTH surfaces: the status-bar window name (`rename-window` + automatic-rename off —
    what the human sees; skipped when the pane shares its window with others, i.e. a split-pane
    executor inside its lead's window) AND Claude Code's own title, by typing `/rename <new_name>`
    as two writes (what `title_by_id` reads). iTerm does only the latter, Terminal.app only the
    former; tmux needs both. True iff the pane exists and the /rename landed."""
    pane = _pane(handle)
    if not pane:
        return False
    if _display(pane, "#{window_panes}") in (None, "1"):
        _rename_window(pane, new_name)
    return _type_line(pane, "/rename " + new_name)


def close_by_id(handle):
    """HANDLE-ONLY close: `kill-pane -t %N`. The caller kills the process first, as for every
    backend. True iff tmux killed it."""
    pane = _pane(handle)
    if not pane:
        return False
    return _ok(_tmux(["kill-pane", "-t", pane]))


def close(label, handle=None, pid=None):
    """Close the session's pane (by id only — see close_by_id)."""
    return close_by_id(handle)


def focus(label, handle=None, pid=None):
    """Bring the pane to the front: switch the client to the pane's session when it is showing a
    different one (best-effort — a detached server has no client), then `select-window` +
    `select-pane`. True iff the pane was selected."""
    pane = _pane(handle)
    if not pane:
        return False
    target_session = _display(pane, "#{session_id}")
    if not target_session:
        return False
    current = (_tmux(["display-message", "-p", "#{session_id}"]).stdout or "").strip()
    if current != target_session:
        _tmux(["switch-client", "-t", target_session])
    _tmux(["select-window", "-t", pane])
    return _ok(_tmux(["select-pane", "-t", pane]))


def tty_by_id(handle):
    """The pane's tty (`#{pane_tty}`: /dev/ttysNNN on macOS, /dev/pts/N on Linux), or None."""
    pane = _pane(handle)
    if not pane:
        return None
    out = _display(pane, "#{pane_tty}")
    return out if out and out.startswith("/dev/") else None


def title_by_id(handle):
    """The pane's current title (`#{pane_title}` — Claude Code's OSC title lands here), or None
    when the pane doesn't resolve. None means "unknown", exactly as for iterm.title_by_id."""
    pane = _pane(handle)
    if not pane:
        return None
    return _display(pane, "#{pane_title}")


def notify(handle, title, body):
    """A status-line message on the client showing the pane: `display-message -t %N -d 4000`
    (`-d` exists since tmux 3.2). '#' is doubled — the message is a tmux format. Best-effort: True
    iff tmux accepted it, never raises. The tmux analogue of iterm.notify_via_tty's OSC 777 banner
    (tmux does not forward OSC 777 to the outer terminal)."""
    pane = _pane(handle)
    if not pane:
        return False
    def clean(s):
        return (s or "").replace("\n", " ").replace("\r", " ").replace("#", "##")
    msg = f"{clean(title)} — {clean(body)}"
    try:
        return _ok(_tmux(["display-message", "-t", pane, "-d", "4000", "--", msg]))
    except Exception:
        return False


def version():
    """`tmux -V` output, or None when tmux isn't on PATH (for `relay doctor`)."""
    r = _tmux(["-V"])
    return (r.stdout or "").strip() if _ok(r) else None


# ── seeing an executor: screen reads, instant wake, pane transcripts ────────────────────────────
# Three tmux-only primitives behind `relay peek` / `relay check`'s screen line, the lead poller's
# instant wake, and the per-session pane.log. Every one is best-effort and handle-only like the
# rest of this module: a foreign/absent handle runs NOTHING and answers None/False.

WAKE_CHANNEL_PREFIX = "relay-wake-"


def capture(handle, lines=40):
    """The pane's screen plus `lines` lines of scrollback above it, as text: `capture-pane -p -J -t
    %N -S -<lines>` (-J joins tmux-wrapped lines back into one). None on a foreign handle or any
    tmux failure (pane gone, no server)."""
    pane = _pane(handle)
    if not pane:
        return None
    try:
        n = max(0, int(lines))
    except (TypeError, ValueError):
        n = 40
    r = _tmux(["capture-pane", "-p", "-J", "-t", pane, "-S", f"-{n}"])
    return r.stdout if _ok(r) else None


def current_command(handle):
    """`#{pane_current_command}` — the name of the pane's foreground process (a shell once the
    launched claude has exited), or None when the pane doesn't resolve."""
    pane = _pane(handle)
    if not pane:
        return None
    return _display(pane, "#{pane_current_command}")


def wake_channel(lead_sid):
    """The `wait-for` channel a lead's Stop-hook poller blocks on and an executor signals."""
    return f"{WAKE_CHANNEL_PREFIX}{lead_sid}"


def wait(channel, timeout):
    """Block on `tmux wait-for <channel>` for at most `timeout` seconds. Returns "signalled" (someone
    ran `wait-for -S <channel>`, or a signal was already stored for it — tmux keeps one for a
    channel nobody waits on, so a signal sent between two waits still lands), "timeout" (the
    interval passed quietly — the normal tick), or "error" (no tmux / no server / any failure; the
    caller falls back to a plain sleep). Never raises."""
    if not channel:
        return "error"
    try:
        r = _tmux(["wait-for", str(channel)], timeout=max(0.1, float(timeout)))
    except Exception:
        return "error"
    if _ok(r):
        return "signalled"
    if r is not None and (r.stderr or "") == TIMED_OUT:
        return "timeout"
    return "error"


def signal(channel):
    """`tmux wait-for -S <channel>` — wake whoever waits on it. True iff tmux accepted it; never
    raises."""
    if not channel:
        return False
    try:
        return _ok(_tmux(["wait-for", "-S", str(channel)]))
    except Exception:
        return False


class WakeWaiter:
    """One lead poller's wait, ticked once per poll interval: `tick(interval)` blocks on the lead's
    wake channel for up to `interval` and returns True when it WAITED (signalled or timed out), or
    False when the caller must do its own `time.sleep(interval)` instead — on any tmux error, and
    for the rest of this poller's life once `wait-for` keeps returning instantly (a stub `tmux`, or
    anything else that would turn the loop into a hot spin)."""

    FAST = 0.05        # a "signalled" return quicker than this counts as instant
    MAX_FAST = 3       # this many instant returns in a row → give up on wait-for for this poller

    def __init__(self, channel):
        self.channel = channel
        self.fast = 0
        self.disabled = False

    def tick(self, interval):
        if self.disabled:
            return False
        t0 = time.time()
        res = wait(self.channel, interval)
        if res == "timeout":
            self.fast = 0
            return True
        if res == "signalled":
            self.fast = self.fast + 1 if time.time() - t0 < self.FAST else 0
            if self.fast >= self.MAX_FAST:
                self.disabled = True
            return True
        self.disabled = True   # "error": tmux can't serve this poller — sleep from now on
        return False


def pipe_pane(handle, log_path):
    """Make sure everything the pane prints is appended to `log_path`: `pipe-pane -o -t %N 'cat >>
    <path>'` (path shlex-quoted; tmux runs the command through /bin/sh). A pane that is ALREADY
    piped (`#{pane_pipe}` is 1) is left alone and answers True: `-o` alone is not enough, because
    on a piped pane tmux's `-o` CLOSES the existing pipe (it is a toggle — verified live), so a
    second call would silently stop the transcript. True iff the pane ends up piped."""
    pane = _pane(handle)
    if not pane or not log_path:
        return False
    if _display(pane, "#{pane_pipe}") == "1":
        return True
    return _ok(_tmux(["pipe-pane", "-o", "-t", pane, f"cat >> {shlex.quote(str(log_path))}"]))


def unpipe_pane(handle):
    """Close the pane's pipe (`pipe-pane -t %N` with no command) — used before re-opening it onto a
    freshly rotated log. True iff tmux accepted it."""
    pane = _pane(handle)
    if not pane:
        return False
    return _ok(_tmux(["pipe-pane", "-t", pane]))
