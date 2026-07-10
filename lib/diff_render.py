"""
diff_render — turn a `git diff --staged` text blob into a self-contained, offline-viewable HTML
review page for `relay diff`. Two render paths:

- Primary: the vendored diff2html bundle (assets/vendor/), inlined into the page — no CDN, no
  network at view time. Used only if the vendored files exist AND their SHA-256 matches what's
  recorded in VENDOR.md (an integrity check against tampering/drift, cheap to run at generate
  time — see `vendor_integrity_ok`).
- Fallback: a small stdlib-only renderer (`render_stdlib_html`) that needs nothing beyond `re`/
  `html` — used whenever the vendored bundle is missing, unreadable, or fails the integrity check.
  Same visual bar: per-file cards, ±line coloring, line numbers, hunk headers, a stats summary,
  and prefers-color-scheme dark/light.

Both paths share `parse_unified_diff` for the header stats (files/+/-) — diff2html computes its
own rendering client-side from the raw diff text, but the page header numbers come from this
module's own parse either way, so they're consistent regardless of which renderer drew the body.
"""
import hashlib
import html
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "assets" / "vendor"
VENDOR_MD = REPO_ROOT / "VENDOR.md"

# Matches a nested repo-relative path (has a "/") OR a bare filename with an extension (top-level
# file, no "/") — e.g. "bin/relay", "scripts/iterm.py", "README.md" — optionally followed by
# ":123" or ":123-145" (a file:line / file:line-range mention). Deliberately permissive: it's
# intersected against the session's ACTUAL staged files afterward, so over-matching (e.g. a
# version number like "3.4.56" reads as a "bare extension" match) is harmless — it just won't be
# in the staged-files set and gets dropped.
_PATH_MENTION_RE = re.compile(
    r'(?<![\w/.-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9]+)'
    r'(?::\d+(?:-\d+)?)?'
)

_HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$')


def parse_report_mentions(report_text):
    """Candidate repo-relative file paths mentioned in a report (file:line style or bare paths),
    in first-seen order, de-duplicated. Best-effort — the caller intersects this against the
    session's actually-staged files, so false positives here are harmless."""
    seen = []
    for m in _PATH_MENTION_RE.finditer(report_text):
        p = m.group(1)
        if p not in seen:
            seen.append(p)
    return seen


def parse_unified_diff(diff_text):
    """Parse `git diff` output into a list of per-file dicts:
    {old_path, new_path, is_new, is_deleted, is_binary, hunks, additions, deletions}
    where each hunk is {header, lines: [(kind, old_no, new_no, text)]} and kind is one of
    "context" / "add" / "del". Best-effort: unrecognized lines are skipped, never raises on
    malformed input (an empty or partial diff just yields fewer/empty entries)."""
    files = []
    cur = None
    hunk = None
    old_no = new_no = 0

    def flush_hunk():
        if hunk is not None and cur is not None:
            cur["hunks"].append(hunk)

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            flush_hunk()
            hunk = None
            m = re.match(r'^diff --git a/(.+) b/(.+)$', raw)
            old_path = new_path = m.group(2) if m else raw[len("diff --git "):]
            if m:
                old_path, new_path = m.group(1), m.group(2)
            cur = {"old_path": old_path, "new_path": new_path, "is_new": False,
                   "is_deleted": False, "is_binary": False, "hunks": [], "additions": 0,
                   "deletions": 0}
            files.append(cur)
            continue
        if cur is None:
            continue
        if raw.startswith("new file mode"):
            cur["is_new"] = True
        elif raw.startswith("deleted file mode"):
            cur["is_deleted"] = True
        elif raw.startswith("Binary files ") and raw.endswith("differ"):
            cur["is_binary"] = True
        elif raw.startswith("--- "):
            p = raw[4:]
            if p not in ("/dev/null",):
                cur["old_path"] = p[2:] if p.startswith(("a/", "b/")) else p
        elif raw.startswith("+++ "):
            p = raw[4:]
            if p not in ("/dev/null",):
                cur["new_path"] = p[2:] if p.startswith(("a/", "b/")) else p
        elif raw.startswith("@@ "):
            flush_hunk()
            hm = _HUNK_RE.match(raw)
            if hm:
                old_no = int(hm.group(1))
                new_no = int(hm.group(3))
                hunk = {"header": raw, "lines": []}
            else:
                hunk = None
        elif hunk is not None and raw.startswith("+"):
            hunk["lines"].append(("add", None, new_no, raw[1:]))
            new_no += 1
            cur["additions"] += 1
        elif hunk is not None and raw.startswith("-"):
            hunk["lines"].append(("del", old_no, None, raw[1:]))
            old_no += 1
            cur["deletions"] += 1
        elif hunk is not None and raw.startswith(" "):
            hunk["lines"].append(("context", old_no, new_no, raw[1:]))
            old_no += 1
            new_no += 1
        elif hunk is not None and raw.startswith("\\"):
            pass  # "\ No newline at end of file" — cosmetic, not a content line
    flush_hunk()
    return files


def _diff_stats(files):
    additions = sum(f["additions"] for f in files)
    deletions = sum(f["deletions"] for f in files)
    return len(files), additions, deletions


def parse_vendor_manifest(vendor_md=VENDOR_MD):
    """{basename: sha256} for every `assets/vendor/<file>` row in VENDOR.md's table. Empty dict if
    the file is missing/unreadable/has no matching rows — callers treat that as 'no vendor'."""
    try:
        text = vendor_md.read_text()
    except OSError:
        return {}
    manifest = {}
    for m in re.finditer(r'assets/vendor/([\w.\-]+)`?[^`]*`([0-9a-f]{64})`', text):
        manifest[m.group(1)] = m.group(2)
    return manifest


def vendor_integrity_ok(vendor_dir=VENDOR_DIR, vendor_md=VENDOR_MD, required=("diff2html.min.js", "diff2html.min.css")):
    """True iff every file in `required` exists under `vendor_dir` AND its live SHA-256 matches
    the hash recorded for it in VENDOR.md. Any mismatch, missing file, or unreadable/unparsable
    VENDOR.md → False (never raises) — the caller degrades to the stdlib renderer."""
    manifest = parse_vendor_manifest(vendor_md)
    if not manifest:
        return False
    for name in required:
        expected = manifest.get(name)
        path = vendor_dir / name
        if not expected or not path.exists():
            return False
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False
        if actual != expected:
            return False
    return True


def load_vendor_bundle(vendor_dir=VENDOR_DIR, vendor_md=VENDOR_MD):
    """(js_text, css_text) from the vendored diff2html bundle if present and integrity-checked,
    else None. Never raises — any I/O error is treated as 'vendor unavailable'."""
    if not vendor_integrity_ok(vendor_dir, vendor_md):
        return None
    try:
        js = (vendor_dir / "diff2html.min.js").read_text()
        css = (vendor_dir / "diff2html.min.css").read_text()
    except OSError:
        return None
    return js, css


_PAGE_STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0;
       padding: 24px; background: #fff; color: #1a1a1a; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #c9d1d9; }
}
header.diffmeta { margin-bottom: 20px; }
header.diffmeta h1 { font-size: 18px; margin: 0 0 6px; }
header.diffmeta .stats { font-size: 13px; opacity: 0.75; }
header.diffmeta .scope-note { display: inline-block; margin-top: 8px; padding: 4px 10px;
  border-radius: 4px; background: #fff3cd; color: #664d03; font-size: 12.5px; }
@media (prefers-color-scheme: dark) {
  header.diffmeta .scope-note { background: #3d3418; color: #f0dca0; }
}
"""


def _render_header(meta, files_count, additions, deletions):
    scope_html = (f'<div class="scope-note">{html.escape(meta.get("scope_note", ""))}</div>'
                  if meta.get("scope_note") else "")
    return (
        '<header class="diffmeta">'
        f'<h1>{html.escape(meta.get("session_id", ""))} '
        f'&middot; packet {html.escape(str(meta.get("packet", "")))}</h1>'
        f'<div class="stats">{files_count} file{"s" if files_count != 1 else ""} changed &middot; '
        f'<span style="color:#2da44e">+{additions}</span> '
        f'<span style="color:#cf222e">-{deletions}</span></div>'
        f'{scope_html}'
        '</header>'
    )


_STDLIB_BODY_STYLE = """
.file-card { border: 1px solid #d0d7de; border-radius: 6px; margin-bottom: 16px; overflow: hidden; }
@media (prefers-color-scheme: dark) { .file-card { border-color: #30363d; } }
.file-card summary { padding: 8px 12px; background: #f6f8fa; cursor: pointer; font-family: monospace;
  font-size: 13px; list-style: none; }
@media (prefers-color-scheme: dark) { .file-card summary { background: #161b22; } }
.file-card summary::-webkit-details-marker { display: none; }
.file-card .binary-note { padding: 10px 12px; font-style: italic; opacity: 0.7; }
table.hunk { border-collapse: collapse; width: 100%; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
table.hunk td { padding: 0 8px; white-space: pre-wrap; word-break: break-all; vertical-align: top; }
td.lno { width: 1%; min-width: 40px; text-align: right; opacity: 0.45; user-select: none; }
tr.hunk-header td { background: #ddf4ff; color: #0969da; padding: 3px 12px; font-weight: 600; }
@media (prefers-color-scheme: dark) { tr.hunk-header td { background: #0c2d6b; color: #79c0ff; } }
tr.add td { background: #dafbe1; }
tr.add td.lno { background: #ccf3d6; }
tr.del td { background: #ffebe9; }
tr.del td.lno { background: #ffd7d5; }
@media (prefers-color-scheme: dark) {
  tr.add td { background: #033a16; }
  tr.add td.lno { background: #04260e; }
  tr.del td { background: #67060c; }
  tr.del td.lno { background: #4b0207; }
}
"""


def _render_file_card(f):
    # old_p/new_p are escaped individually, BEFORE the static "&rarr;"/"(new file)"/"(deleted)"
    # markup is spliced in — escaping the whole assembled string afterward (as this used to do)
    # would double-escape "&rarr;" into visible "&amp;rarr;" text.
    old_p, new_p = html.escape(f["old_path"]), html.escape(f["new_path"])
    title = new_p if f["old_path"] == f["new_path"] else f"{old_p} &rarr; {new_p}"
    if f["is_new"]:
        title += " (new file)"
    elif f["is_deleted"]:
        title += " (deleted)"
    parts = [f'<details class="file-card" open><summary>{title} '
             f'<span style="opacity:0.6">+{f["additions"]} -{f["deletions"]}</span></summary>']
    if f["is_binary"]:
        parts.append('<div class="binary-note">Binary file differs</div>')
    else:
        parts.append('<table class="hunk">')
        for hunk in f["hunks"]:
            parts.append(f'<tr class="hunk-header"><td class="lno"></td><td class="lno"></td>'
                          f'<td>{html.escape(hunk["header"])}</td></tr>')
            for kind, old_no, new_no, text in hunk["lines"]:
                cls = {"add": "add", "del": "del", "context": ""}[kind]
                sign = {"add": "+", "del": "-", "context": " "}[kind]
                old_s = str(old_no) if old_no is not None else ""
                new_s = str(new_no) if new_no is not None else ""
                parts.append(
                    f'<tr class="{cls}"><td class="lno">{old_s}</td><td class="lno">{new_s}</td>'
                    f'<td>{html.escape(sign + text)}</td></tr>'
                )
        parts.append('</table>')
    parts.append('</details>')
    return "".join(parts)


def render_stdlib_html(diff_text, meta):
    """The fallback renderer: parses `diff_text` itself (stdlib only, no vendored deps) and
    produces a full self-contained HTML page — per-file collapsible cards, ±line coloring, old/new
    line numbers, hunk headers, a stats summary header, and prefers-color-scheme dark/light."""
    files = parse_unified_diff(diff_text)
    n_files, additions, deletions = _diff_stats(files)
    body = "".join(_render_file_card(f) for f in files) or '<p><em>No changes.</em></p>'
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>relay diff — {html.escape(meta.get('session_id', ''))}</title>"
        f"<style>{_PAGE_STYLE}{_STDLIB_BODY_STYLE}</style></head><body>"
        f"{_render_header(meta, n_files, additions, deletions)}"
        f"<main>{body}</main>"
        "</body></html>"
    )


def js_embed(payload):
    """JSON-encode `payload` for safe embedding inside an inline <script> block, as EITHER a JS
    data expression or (via `eval`, see render_diff2html_html) a way to smuggle arbitrary raw JS
    source through the same one safe path.

    Why this is needed: HTML's parser looks for the literal byte sequence `</script` (matched
    CASE-INSENSITIVELY, before any JS parsing happens) to end a <script> element. A payload that
    contains that substring — e.g. a diff touching HTML/JS files, or even this very module's own
    source (it literally contains the strings `<script>`/`</script>`) — truncates the block early:
    the JS dies with an unterminated-string error and the rest of the payload spills onto the page
    as raw visible text. Regular JS string quoting does NOT protect against this; it's an HTML
    parsing rule, not a JS one, and it runs first.

    The fix: `json.dumps` first (correct JS-string escaping of quotes/backslashes/control chars),
    THEN replace every literal `<` with its unicode escape `\\u003c`. Inside a JSON/JS string,
    `\\u003c` and a literal `<` are byte-for-byte identical once the JS engine parses the string —
    so this changes nothing semantically — but the HTML parser scanning for `</script` never sees
    a `<` character at all, so `</script`, `<!--`, and `<script` are ALL defused in one move,
    regardless of case or surrounding content. This is the ONE helper every script-block payload
    goes through — no per-site ad-hoc escaping."""
    return json.dumps(payload).replace("<", "\\u003c")


def render_diff2html_html(diff_text, meta, js_text, css_text):
    """The primary renderer: inlines the vendored diff2html JS+CSS and lets it draw a side-by-side
    diff client-side from the raw diff text. BOTH payloads that enter a <script> block — the
    vendored library's own source (`js_text`) AND the diff text — go through `js_embed` (see its
    docstring for why): `js_text` is embedded as a JSON string and executed via `eval`, exactly
    equivalent to inlining it directly as a script body (the vendored bundle is a UMD IIFE that
    reads `this` as the global object either way), and `diffText` is embedded as a plain JS string
    expression, never interpolated raw into a template."""
    files = parse_unified_diff(diff_text)
    n_files, additions, deletions = _diff_stats(files)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>relay diff — {html.escape(meta.get('session_id', ''))}</title>"
        f"<style>{_PAGE_STYLE}</style>"
        f"<style>{css_text}</style>"
        "</head><body>"
        f"{_render_header(meta, n_files, additions, deletions)}"
        '<div id="d2h-mount"></div>'
        f"<script>eval({js_embed(js_text)});</script>"
        "<script>"
        f"var diffText = {js_embed(diff_text)};"
        "document.getElementById('d2h-mount').innerHTML = Diff2Html.html(diffText, "
        "{drawFileList: true, matching: 'lines', outputFormat: 'side-by-side'});"
        "</script>"
        "</body></html>"
    )


def generate_page(diff_text, meta, vendor_dir=VENDOR_DIR, vendor_md=VENDOR_MD):
    """The single entry point `relay diff` calls: renders via the vendored diff2html bundle if
    it's present and passes its integrity check, else falls back to the stdlib renderer. Returns
    the full HTML string. Never raises over a vendor problem — that's exactly the case the
    fallback exists for."""
    bundle = load_vendor_bundle(vendor_dir, vendor_md)
    if bundle is not None:
        js_text, css_text = bundle
        return render_diff2html_html(diff_text, meta, js_text, css_text)
    return render_stdlib_html(diff_text, meta)
