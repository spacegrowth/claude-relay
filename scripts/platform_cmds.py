"""
The ONE platform seam: every OS-specific helper relay shells out to (open a file, copy to the
clipboard) goes through here, so Linux (Ubuntu + tmux) and macOS are told apart in a single place.

Contract: nothing here ever raises, and a False return is "no helper available / it failed" — the
caller prints the path or the text instead, so a headless box still gets the information.

  - `is_linux()`          — `platform.system() == "Linux"` (mockable in one spot).
  - `open_path(path)`     — macOS `open`; Linux `xdg-open` when on PATH; else False.
  - `copy_to_clipboard()` — macOS `pbcopy`; Linux `xclip -selection clipboard`, else `wl-copy`; else False.
  - `linux_helpers()`     — {tool: bool} presence of the optional Linux helpers (for `relay doctor`).

None of the Linux helpers is required; relay degrades to printing.
"""
import platform
import shutil
import subprocess

LINUX_HELPERS = ("xdg-open", "xclip", "wl-copy", "notify-send")


def is_linux():
    return platform.system() == "Linux"


def is_macos():
    return platform.system() == "Darwin"


def open_path(path):
    """Open `path` (file or URL) with the desktop's default handler. True when a helper ran and
    exited 0, False when there is none (headless box) or it failed. Never raises."""
    if is_linux():
        exe = shutil.which("xdg-open")
        if not exe:
            return False
        argv = ["xdg-open", str(path)]
    elif is_macos():
        argv = ["open", str(path)]
    else:
        return False
    try:
        r = subprocess.run(argv, capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def clipboard_argv():
    """The argv that reads stdin into the clipboard on this platform, or None."""
    if is_linux():
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard"]
        if shutil.which("wl-copy"):
            return ["wl-copy"]
        return None
    if is_macos():
        return ["pbcopy"]
    return None


def copy_to_clipboard(text):
    """Put `text` on the clipboard. True on success, False when no helper exists or it failed."""
    argv = clipboard_argv()
    if not argv:
        return False
    try:
        r = subprocess.run(argv, input=str(text), text=True, capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def linux_helpers():
    """{helper: on PATH?} for the optional Linux helpers relay uses when present."""
    return {h: bool(shutil.which(h)) for h in LINUX_HELPERS}
