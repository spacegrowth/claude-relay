"""
Backend selector: which terminal relay drives — iTerm2 (scripts/iterm.py), Terminal.app
(scripts/terminal_app.py) or tmux (scripts/tmux_backend.py). All expose the same operations; iTerm
addresses tabs by session id (title as a legacy fallback), Terminal by captured window id, tmux by
pane id (see each module's docstring).

Selection order:
1. $RELAY_TERMINAL ("iterm" | "terminal" | "tmux") — explicit per-invocation override, also what
   tests use to pin behavior regardless of where the suite runs.
2. `terminal_app` in ~/.relay-tasks/lead/config.json ("iterm" | "terminal" | "tmux"; "auto" falls
   through).
3. Inside tmux ($TMUX set, or $TERM_PROGRAM == "tmux") → tmux. Checked BEFORE $TERM_PROGRAM's
   Terminal.app test: a tmux pane on a Mac inherits the outer terminal's identity variables, and
   the pane — not the outer tab — is what relay can address. This is the Linux/SSH backend.
4. $TERM_PROGRAM auto-detect: "Apple_Terminal" → Terminal.app; anything else → iTerm (the default,
   and the richer backend: tab colors, title-based focus for leads).

Sessions record their backend at spawn ("backend" in session.json); by_name() resolves it so a
session spawned under one terminal keeps being addressed there even if relay is later invoked from
the other.

This module is also the ONE place that knows what a LIVE handle looks like — the current process's
own tab/pane id, read from the environment (`live_handle_from_env`, `is_live_handle`,
`tab_id_from_env`). Every site that used to read $ITERM_SESSION_ID/$TERM_SESSION_ID directly goes
through these.
"""
import os
import re

import iterm
import terminal_app
import tmux_backend

_BY_NAME = {"iterm": iterm, "terminal": terminal_app, "tmux": tmux_backend}

# A real iTerm session handle ("w#t#p#:UUID") — the shape row 89 trusts for a live $ITERM_SESSION_ID.
LIVE_ITERM_SESSION_RE = re.compile(r"^w\d+t\d+p\d+:[0-9A-Fa-f-]{36}$")
# A tmux pane handle as relay stores it ("tmux:%N").
LIVE_TMUX_HANDLE_RE = re.compile(r"^tmux:%\d+$")


def by_name(name):
    """The backend module registered under `name`, or None (caller falls back to the selected
    default — covers pre-backend session records)."""
    return _BY_NAME.get(str(name or "").lower())


def _in_tmux():
    return bool(os.environ.get("TMUX")) or os.environ.get("TERM_PROGRAM") == "tmux"


def select():
    env = os.environ.get("RELAY_TERMINAL", "").lower()
    if env in _BY_NAME:
        return _BY_NAME[env]
    try:
        import lead_guard
        cfg = lead_guard.load_config(os.path.join(os.path.expanduser("~"), ".relay-tasks"))
        name = str(cfg.get("terminal_app", "auto")).lower()
        if name in _BY_NAME:
            return _BY_NAME[name]
    except Exception:
        pass
    if _in_tmux():
        return tmux_backend
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return terminal_app
    return iterm


def is_live_handle(value):
    """True for a value shaped like a real live handle of either addressable kind: an iTerm session
    id ("w#t#p#:UUID") or a tmux pane handle ("tmux:%N")."""
    v = str(value or "")
    return bool(LIVE_ITERM_SESSION_RE.match(v) or LIVE_TMUX_HANDLE_RE.match(v))


def live_handle_from_env(bk=None):
    """The CURRENT process's own tab/pane handle for backend `bk` (default: the selected one), or
    None — strict: only a value that `is_live_handle` would accept.
      - tmux:  $TMUX_PANE as "tmux:%N";
      - iTerm: $ITERM_SESSION_ID / $TERM_SESSION_ID, only when shaped like a real iTerm handle;
      - anything else (Terminal.app): None — it has no live handle in the environment."""
    name = getattr(bk if bk is not None else select(), "NAME", None)
    if name == "tmux":
        pane = os.environ.get("TMUX_PANE", "")
        return tmux_backend.handle_for(pane) if re.match(r"^%\d+$", pane) else None
    if name == "iterm":
        v = os.environ.get("ITERM_SESSION_ID") or os.environ.get("TERM_SESSION_ID")
        return v if v and LIVE_ITERM_SESSION_RE.match(v) else None
    return None


def tab_id_from_env(bk=None):
    """The tab identity a lead MARKER records as `iterm_session` at arm time, and that the hooks /
    statusline / close-predecessor compare against it by exact string equality. Under tmux it is
    the pane handle (`live_handle_from_env`), never the $TERM_SESSION_ID a tmux pane on a Mac
    inherits from the outer iTerm tab. Under every other backend it is the raw $TERM_SESSION_ID,
    unchanged from before tmux existed — Terminal.app sets it to a bare UUID that is no iTerm
    handle, and migrate-by-tab for Terminal leads keys off exactly that value, so it must not be
    shape-filtered here (use `live_handle_from_env` where a strictly-live handle is wanted)."""
    b = bk if bk is not None else select()
    if getattr(b, "NAME", None) == "tmux":
        return live_handle_from_env(b)
    return os.environ.get("TERM_SESSION_ID")
