"""
Unit tests for lib/diff_render.py: unified-diff parsing, report-mention extraction, vendor
integrity checking, and both render paths (stdlib fallback + diff2html primary). No network — the
vendored bundle used here is whatever is actually committed/staged under assets/vendor/.

Run: pytest tests/test_diff_render.py -v
"""
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
import diff_render as dr  # noqa: E402


SAMPLE_DIFF = """diff --git a/a.py b/a.py
index e69de29..b6fc4c6 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 line one
-old line
+new line
+added line
diff --git a/b.py b/b.py
new file mode 100644
index 0000000..d95f3ad
--- /dev/null
+++ b/b.py
@@ -0,0 +1,2 @@
+hello
+world
"""


class TestParseUnifiedDiff:
    def test_two_files_with_correct_paths(self):
        files = dr.parse_unified_diff(SAMPLE_DIFF)
        assert [f["new_path"] for f in files] == ["a.py", "b.py"]

    def test_additions_and_deletions_counted(self):
        files = dr.parse_unified_diff(SAMPLE_DIFF)
        a = files[0]
        assert a["additions"] == 2 and a["deletions"] == 1

    def test_new_file_flagged(self):
        files = dr.parse_unified_diff(SAMPLE_DIFF)
        assert files[1]["is_new"] is True
        assert files[0]["is_new"] is False

    def test_hunk_line_numbers(self):
        files = dr.parse_unified_diff(SAMPLE_DIFF)
        hunk = files[0]["hunks"][0]
        kinds = [ln[0] for ln in hunk["lines"]]
        assert kinds == ["context", "del", "add", "add"]

    def test_empty_diff_yields_no_files(self):
        assert dr.parse_unified_diff("") == []

    def test_malformed_input_never_raises(self):
        assert dr.parse_unified_diff("not a diff\nrandom garbage\n@@ nonsense @@\n+x") == []


class TestParseReportMentions:
    def test_file_line_style(self):
        mentions = dr.parse_report_mentions("Changed bin/relay:339-345 and scripts/iterm.py:161.")
        assert "bin/relay" in mentions
        assert "scripts/iterm.py" in mentions

    def test_bare_top_level_path(self):
        assert "README.md" in dr.parse_report_mentions("Updated README.md with a new sentence.")

    def test_dedupes_preserving_order(self):
        mentions = dr.parse_report_mentions("bin/relay:1 then bin/relay:5 again bin/relay")
        assert mentions.count("bin/relay") == 1

    def test_no_mentions_in_prose(self):
        assert dr.parse_report_mentions("Everything works great, nothing to see here.") == []


class TestVendorIntegrity:
    def _write_vendor(self, tmp_path, js_body="console.log(1)", css_body="body{}"):
        vendor_dir = tmp_path / "vendor"
        vendor_dir.mkdir()
        js_path = vendor_dir / "diff2html.min.js"
        css_path = vendor_dir / "diff2html.min.css"
        js_path.write_text(js_body)
        css_path.write_text(css_body)
        js_sha = hashlib.sha256(js_path.read_bytes()).hexdigest()
        css_sha = hashlib.sha256(css_path.read_bytes()).hexdigest()
        vendor_md = tmp_path / "VENDOR.md"
        vendor_md.write_text(
            "| File | Version | Source URL | SHA-256 | License |\n"
            "| --- | --- | --- | --- | --- |\n"
            f"| `assets/vendor/diff2html.min.js` | 9.9.9 | http://x | `{js_sha}` | MIT |\n"
            f"| `assets/vendor/diff2html.min.css` | 9.9.9 | http://x | `{css_sha}` | MIT |\n"
        )
        return vendor_dir, vendor_md

    def test_matching_hashes_pass(self, tmp_path):
        vendor_dir, vendor_md = self._write_vendor(tmp_path)
        assert dr.vendor_integrity_ok(vendor_dir, vendor_md) is True

    def test_corrupted_file_fails(self, tmp_path):
        vendor_dir, vendor_md = self._write_vendor(tmp_path)
        (vendor_dir / "diff2html.min.js").write_text("tampered!!!")
        assert dr.vendor_integrity_ok(vendor_dir, vendor_md) is False

    def test_missing_vendor_md_fails(self, tmp_path):
        vendor_dir, _ = self._write_vendor(tmp_path)
        assert dr.vendor_integrity_ok(vendor_dir, tmp_path / "nope.md") is False

    def test_missing_file_fails(self, tmp_path):
        vendor_dir, vendor_md = self._write_vendor(tmp_path)
        (vendor_dir / "diff2html.min.css").unlink()
        assert dr.vendor_integrity_ok(vendor_dir, vendor_md) is False

    def test_load_vendor_bundle_none_on_mismatch(self, tmp_path):
        vendor_dir, vendor_md = self._write_vendor(tmp_path)
        (vendor_dir / "diff2html.min.js").write_text("tampered!!!")
        assert dr.load_vendor_bundle(vendor_dir, vendor_md) is None

    def test_load_vendor_bundle_returns_text_on_match(self, tmp_path):
        vendor_dir, vendor_md = self._write_vendor(tmp_path, js_body="JSBODY", css_body="CSSBODY")
        js, css = dr.load_vendor_bundle(vendor_dir, vendor_md)
        assert js == "JSBODY" and css == "CSSBODY"

    def test_real_committed_vendor_passes(self):
        # The actual assets/vendor/ files this packet staged — proves VENDOR.md's recorded
        # hashes match what's really on disk, not just a synthetic tmp_path fixture.
        assert dr.vendor_integrity_ok() is True


class TestGeneratePage:
    def test_bogus_vendor_dir_falls_back_to_stdlib(self, tmp_path):
        page = dr.generate_page(SAMPLE_DIFF, {"session_id": "s1", "packet": 1, "scope_note": ""},
                                vendor_dir=tmp_path / "nonexistent", vendor_md=tmp_path / "nope.md")
        assert "file-card" in page  # stdlib renderer's per-file card marker
        assert "d2h-mount" not in page  # diff2html mount point absent

    def test_stdlib_page_has_stats_header(self, tmp_path):
        page = dr.render_stdlib_html(SAMPLE_DIFF, {"session_id": "s1", "packet": 1, "scope_note": ""})
        assert "2 files changed" in page
        # a.py: +2/-1 (1 context, 1 del, 2 add); b.py (new file): +2/-0 → totals +4/-1
        assert "+4" in page and "-1" in page

    def test_scope_note_rendered_when_present(self):
        page = dr.render_stdlib_html(SAMPLE_DIFF, {"session_id": "s1", "packet": 1,
                                                     "scope_note": "unfiltered — report not found/parsable"})
        assert "unfiltered — report not found/parsable" in page

    def test_scope_note_absent_when_empty(self):
        # The "scope-note" CSS class is always defined in <style> — check the actual DIV element
        # is absent, not the class name (which would false-positive on the stylesheet rule).
        page = dr.render_stdlib_html(SAMPLE_DIFF, {"session_id": "s1", "packet": 1, "scope_note": ""})
        assert '<div class="scope-note">' not in page

    def test_real_vendor_produces_diff2html_page(self):
        # Uses the actual assets/vendor/ files (staged in this repo, not a network fetch).
        page = dr.generate_page(SAMPLE_DIFF, {"session_id": "s1", "packet": 1, "scope_note": ""})
        assert "d2h-mount" in page
        assert "Diff2Html.html" in page
        # The vendored CSS is inlined verbatim (no <script>-closing risk in a <style> block).
        css_text = (dr.VENDOR_DIR / "diff2html.min.css").read_text()
        assert css_text[:200] in page
        # The vendored JS is inlined via js_embed()+eval (see TestJsEmbed) — not verbatim, since
        # its own source contains literal <script>/</script> strings (this bug's root cause).
        # Round-trip: extract the eval(...) payload and confirm it decodes back to the real JS.
        js_text = (dr.VENDOR_DIR / "diff2html.min.js").read_text()
        m = re.search(r'<script>eval\((".*?")\);</script>', page)
        assert m, "no eval(...) wrapper found for the vendored JS"
        assert json.loads(m.group(1)) == js_text


class TestJsEmbed:
    """js_embed(payload): the ONE helper every script-block payload goes through — JSON-encode,
    then escape every '<' to \\u003c, so a payload containing '</script', '<script', or '<!--'
    can never prematurely close the enclosing <script> element (an HTML-parsing-layer rule that
    runs BEFORE any JS parsing, so ordinary string quoting does not protect against it)."""

    def test_round_trips_via_json(self):
        payload = "hello \"world\" \\ backslash"
        assert json.loads(dr.js_embed(payload)) == payload

    def test_escapes_close_script(self):
        embedded = dr.js_embed("before </script> after")
        assert "</script>" not in embedded
        assert json.loads(embedded) == "before </script> after"

    def test_escapes_open_script_and_comment(self):
        embedded = dr.js_embed("<script>evil()</script><!-- x -->")
        assert "<script" not in embedded and "<!--" not in embedded
        assert json.loads(embedded) == "<script>evil()</script><!-- x -->"

    def test_case_insensitive_close_script_also_defused(self):
        # HTML's script-end scan is case-insensitive; '<' escaping defuses it regardless of case
        # since it's the '<' itself (not the word "script") that's neutralized.
        embedded = dr.js_embed("</SCRIPT> </ScRiPt>")
        assert "<" not in embedded


class TestScriptInjectionRegression:
    """REGRESSION (packet 006): a diff whose CONTENT contains literal '</script>', '<script>', or
    '<!--' (e.g. this very module's own source, or any diff touching HTML/JS files) used to
    truncate the inline <script> block early — HTML parsing finds '</script' before JS parsing
    ever runs, killing the script and spilling the tail of the payload as raw visible text."""

    PATHOLOGICAL_DIFF = (
        "diff --git a/evil.html b/evil.html\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/evil.html\n"
        "@@ -0,0 +1,3 @@\n"
        "+<script>alert(1)</script>\n"
        "+<!-- a comment -->\n"
        "+more </script> text and <script> again\n"
    )

    def test_no_premature_script_close(self):
        page = dr.generate_page(self.PATHOLOGICAL_DIFF,
                                {"session_id": "s1", "packet": 1, "scope_note": ""})
        # Split on every <script OPEN tag; each resulting chunk must contain AT MOST one literal
        # </script> — its own closing tag — never an extra one smuggled in from the payload.
        chunks = page.split("<script")
        assert len(chunks) > 1, "no <script tag found at all — page structure changed unexpectedly"
        for chunk in chunks[1:]:
            assert chunk.count("</script>") <= 1

    def test_escaped_form_present_where_payload_carried_it(self):
        page = dr.generate_page(self.PATHOLOGICAL_DIFF,
                                {"session_id": "s1", "packet": 1, "scope_note": ""})
        # The payload's dangerous sequences must survive as the ESCAPED < form, not vanish.
        assert "\\u003c/script" in page
        assert "\\u003cscript" in page
        assert "\\u003c!--" in page

    def test_diff2html_mount_and_call_still_present(self):
        page = dr.generate_page(self.PATHOLOGICAL_DIFF,
                                {"session_id": "s1", "packet": 1, "scope_note": ""})
        assert "d2h-mount" in page
        assert "Diff2Html.html" in page

    def test_stdlib_fallback_also_survives_pathological_content(self, tmp_path):
        # The stdlib path has no <script> tags at all, so this bug class cannot occur there — but
        # confirm the pathological content still renders as properly html.escape()d text.
        page = dr.render_stdlib_html(self.PATHOLOGICAL_DIFF,
                                     {"session_id": "s1", "packet": 1, "scope_note": ""})
        assert "<script>" not in page.split("<style>", 1)[1].split("</style>", 1)[1]
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
