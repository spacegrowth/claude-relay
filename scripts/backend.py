"""
Backend selector: which terminal app relay drives — iTerm2 (scripts/iterm.py) or Terminal.app
(scripts/terminal_app.py). Both expose the same operations; iTerm addresses tabs by title,
Terminal by captured window id (see each module's docstring).

Selection order:
1. $RELAY_TERMINAL ("iterm" | "terminal") — explicit per-invocation override, also what tests use
   to pin behavior regardless of where the suite runs.
2. `terminal_app` in ~/.relay-tasks/lead/config.json ("iterm" | "terminal"; "auto" falls through).
3. $TERM_PROGRAM auto-detect: "Apple_Terminal" → Terminal.app; anything else → iTerm (the default,
   and the richer backend: tab colors, title-based focus for leads).

Sessions record their backend at spawn ("backend" in session.json); by_name() resolves it so a
session spawned under one terminal keeps being addressed there even if relay is later invoked from
the other.
"""
import os

import iterm
import terminal_app

_BY_NAME = {"iterm": iterm, "terminal": terminal_app}


def by_name(name):
    """The backend module registered under `name`, or None (caller falls back to the selected
    default — covers pre-backend session records)."""
    return _BY_NAME.get(str(name or "").lower())


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
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return terminal_app
    return iterm
