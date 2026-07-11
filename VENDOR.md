# Vendored third-party assets

Third-party files checked directly into this repo (not fetched at install/run time), so `relay
diff` renders a rich side-by-side HTML page fully offline with zero network calls and no CDN
dependency. Each is pinned to an exact upstream version and its checksum is recorded here so
tampering or an accidental version drift is detectable at generate time (see `lib/diff_render.py`'s
`vendor_integrity_ok`, which relay diff calls before using any of these files).

| File | Version | Source URL | SHA-256 | License |
| --- | --- | --- | --- | --- |
| `assets/vendor/diff2html.min.js` | 3.4.56 | https://cdn.jsdelivr.net/npm/diff2html@3.4.56/bundles/js/diff2html.min.js | `a2110a09cee157bd5466da77be02107ac81a0baa2bc1f3fe81aac8183314598e` | MIT |
| `assets/vendor/diff2html.min.css` | 3.4.56 | https://cdn.jsdelivr.net/npm/diff2html@3.4.56/bundles/css/diff2html.min.css | `d3ecc0e9b2b1e5c8466c19de29bed052fd0863475d25829ecc858446efded372` | MIT |
| `assets/vendor/diff2html-LICENSE.md` | 3.4.56 | https://cdn.jsdelivr.net/npm/diff2html@3.4.56/LICENSE.md | `6de794566a8feb4233594420299cec2dbfba51ae57201763541a5e7d627cb245` | MIT |

Fetched 2026-07-10. `diff2html.min.js` is the **base** bundle (NOT the `-ui`/highlight.js
variant) — plain `Diff2Html.html(...)` output only, no syntax highlighting, no extra JS
dependencies. jsdelivr's `x-jsd-version` response header was checked at fetch time and confirmed
`3.4.56` exactly (not resolved to `latest`), and each file's SHA-256 was computed locally with
`shasum -a 256` immediately after the fetch, before anything else touched the file.

## Refresh procedure

To bump the pinned version: pick the new version number, `curl` the three URLs above with that
version substituted into the path (or check the package's GitHub releases for the LICENSE.md
location if it moves), overwrite the three files under `assets/vendor/`, recompute each file's
`shasum -a 256`, and update every field in the table above (version, URL if changed, hash) in the
same commit as the file changes — `relay diff`'s integrity check compares the CURRENT file bytes
against whatever hash is recorded here, so a stale hash after a bump makes `relay diff` silently
degrade to the stdlib fallback renderer instead of erroring loudly.

## Runtime dependencies (not vendored)

These are never shipped in this repo — relay invokes them only if present on your machine:

- **terminal-notifier** (optional, `brew install terminal-notifier`, BSD-licensed) — clickable,
  coalescing desktop banners; without it, iTerm leads still get clickable banners for free (native
  OSC 777 written to the lead's own tty, zero dependency), just without coalescing — Terminal.app
  leads fall back to macOS's built-in, unclickable notifications.
- **iterm2** Python package (optional, `pip3 install iterm2`) — true adjacent-tab placement via
  iTerm2's Python API; without it (or with the API disabled) placement falls back to AppleScript.
- **macOS system tools** — `osascript`, `ps`, `git`, and the `claude` CLI itself.
