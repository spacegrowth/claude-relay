"""
Terminal.app backend for claude-relay — the same six operations as scripts/iterm.py (spawn, send,
is_alive, focus, close, rename_by_id) with one structural difference: **window-id addressing, not
title matching**. Terminal.app only exposes Claude Code's OSC title on the SELECTED tab, so titles
can't be trusted for liveness or lookup (proven in ccsessions' TerminalBackend, which this vendors
patterns from). Instead, spawn captures the new window's AppleScript `id` and every later operation
addresses `window id N` directly — stable for the window's lifetime, immune to title churn.

Differences from iTerm, by design of Terminal.app itself:
- Executors open as new WINDOWS, not tabs (Terminal has no scriptable tab-create without the
  Accessibility/TCC permission; ccsessions' menu-click workaround isn't worth the permission nag).
- No tab colors (`tab_color` is accepted and ignored — Terminal.app has no such facility).
- The handle written to `iterm_id_file` is "twid:<window id>"; a missing/foreign handle makes every
  operation return False rather than guess.
"""
import shlex

from iterm import run_osascript, osa, build_claude_cmd, _app_running

NAME = "terminal"  # backend key (see scripts/backend.py)
APP = "Terminal"


def running():
    """Terminal.app is running — ps-based for the same sandbox reason as iterm._app_running."""
    return _app_running("Terminal.app/Contents/MacOS/Terminal")


def _wid(handle):
    """Window id from a stored handle ("twid:123" → "123"); None for absent/foreign handles
    (e.g. an iTerm "w0t0p0:UUID" from a session spawned under the other backend)."""
    h = str(handle or "")
    if h.startswith("twid:") and h[5:].isdigit():
        return h[5:]
    return None


def spawn(cwd, prompt, label, pidfile, model=None, skip_perms=False, rename_delay=1.5,
          env_prefix="", iterm_id_file=None, session_uuid=None, resume_id=None, tab_color=None,
          lead_handle=None, layout="tab", settings_file=None):
    """New Terminal WINDOW running the standard launch chain (cd → pidfile via $$ → exec claude).
    Writes "twid:<window id>" to `iterm_id_file` — the handle every later operation uses — then
    best-effort sets the tab's custom title to `label` (cosmetic only; addressing never relies on
    it). `tab_color` ignored (Terminal.app has no tab colors). `rename_delay` unused (no post-start
    /rename dance needed — the custom title is Terminal chrome, not Claude's OSC title).
    `lead_handle` ignored (Terminal.app addresses by window, not adjacent tabs — n/a here).
    `layout` ignored (Terminal.app has no split-pane scripting surface — always a new window).
    `settings_file` passed straight through to build_claude_cmd (same meaning as iTerm's backend)."""
    base = build_claude_cmd(prompt, model=model, skip_perms=skip_perms,
                            session_uuid=session_uuid, resume_id=resume_id,
                            settings_file=settings_file)
    cmd = f"cd {shlex.quote(cwd)} && {env_prefix}echo $$ > {shlex.quote(pidfile)} && exec {base}"
    # The window id must be derived from the RETURNED tab itself — "front window" races with any
    # window that is mid-close (observed live: it returned the closing window's id). A tab has no
    # scriptable `window` property (also observed — ccsessions wraps that access in `try`), but its
    # `tty` is unique, so: take the new tab's tty, then find the window owning that tty.
    script = (
        f'tell application "{APP}"\n'
        "  activate\n"
        f'  set t to do script "{osa(cmd)}"\n'
        "  set theTty to tty of t\n"
        "  repeat with w in windows\n"
        "    repeat with tb in tabs of w\n"
        "      if (tty of tb) is theTty then return (id of w) as text\n"
        "    end repeat\n"
        "  end repeat\n"
        '  return ""\n'
        "end tell"
    )
    r = run_osascript(script, timeout=10)
    wid = (r.stdout or "").strip()
    if r.returncode != 0 or not wid.isdigit():
        return
    if iterm_id_file:
        try:
            with open(iterm_id_file, "w") as f:
                f.write(f"twid:{wid}")
        except OSError:
            pass
    rename_by_id(f"twid:{wid}", label)


def is_alive(label, handle=None, pid=None):
    """The window still exists. (The caller separately tracks the PROCESS via its recorded pid —
    same division of labor as iTerm's title check.)"""
    wid = _wid(handle)
    if not wid or not running():
        return False
    r = run_osascript(f'tell application "{APP}" to return exists window id {wid}', timeout=3)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def send(label, prompt, handle=None, pid=None):
    """Always False: Terminal.app CANNOT inject text into a running process. iTerm's `write text`
    has no equivalent — `do script … in tab` queues a SHELL command for when the foreground process
    exits (verified live: it returns success while the running claude receives nothing), and
    keystroke injection via System Events needs the Accessibility permission. Returning False routes
    relay's send through its resume-fallback: the conversation reopens via `claude --resume` in a
    fresh window with the packet delivered as the launch prompt — same context, one extra window.
    SHORTCUT: in-place injection would need an opt-in System Events/Accessibility path; add it only
    if the resume-per-packet window churn ever actually hurts."""
    return False


def close(label, handle=None, pid=None):
    """BEST-EFFORT close of the session's window; the caller kills the process FIRST. Known quirk
    (observed live, macOS 15+): Terminal accepts the `close` AppleEvent without error and simply
    ignores it — the return value is therefore verified against `exists`, and callers treat False
    as 'window lingers, human closes it' rather than an error. The process is already dead, so
    nothing functional is lost."""
    wid = _wid(handle)
    if not wid:
        return False
    # Verify the close actually took — Terminal silently ignores `close` on a window whose
    # 'process still running' confirm sheet is up (the caller kills first precisely to avoid that,
    # but never report success on faith).
    script = (
        f'tell application "{APP}"\n'
        f"  if not (exists window id {wid}) then return false\n"
        f"  close window id {wid}\n"
        "  delay 0.2\n"
        f"  return (not (exists window id {wid})) as text\n"
        "end tell"
    )
    r = run_osascript(script, timeout=5)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def focus(label, handle=None, pid=None):
    """Bring the session's window to the front."""
    wid = _wid(handle)
    if not wid:
        return False
    script = (
        f'tell application "{APP}"\n'
        "  activate\n"
        f"  if not (exists window id {wid}) then return false\n"
        f"  set index of window id {wid} to 1\n"
        "  return true\n"
        "end tell"
    )
    r = run_osascript(script, timeout=5)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def rename_by_id(handle, new_name):
    """Set the tab's custom title (visible chrome so a human can tell tabs apart). Best-effort —
    addressing is by window id, so a failed rename costs nothing functional."""
    wid = _wid(handle)
    if not wid:
        return False
    script = (
        f'tell application "{APP}"\n'
        f"  if not (exists window id {wid}) then return false\n"
        "  try\n"
        f'    set custom title of selected tab of window id {wid} to "{osa(new_name)}"\n'
        "  end try\n"
        "  return true\n"
        "end tell"
    )
    r = run_osascript(script, timeout=5)
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def tty_by_id(handle):
    """Lead tab-color support is iTerm-only (Terminal.app has no tab colors) — always None."""
    return None
