"""
Bug-hunt tests for lib/diff_render.py and lib/board_render.py — the two pages a lead actually
reviews work through.

Oracle: README's `relay diff` paragraph ("renders an executor's `git diff --staged` to a
self-contained, offline HTML page … so you review diffs in one click"), README "The board",
and the two modules' own docstrings (notably `parse_unified_diff`'s per-file
additions/deletions contract, `js_embed`'s `</script` defusal, and board_render's "Pure:
render(data) -> html").

Run: pytest tests/test_lib_render_bughunt.py -v
"""
import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
import board_render as br  # noqa: E402
import diff_render as dr  # noqa: E402


def diff_for(path, old_lines, new_lines):
    """A minimal but real `git diff` shape for a one-file edit."""
    head = (f"diff --git a/{path} b/{path}\n"
            f"index 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(old_lines)} +1,{len(new_lines)} @@\n")
    return head + "".join(old_lines) + "".join(new_lines)


# ── diff parsing: what the review page is allowed to lose (nothing) ────────────────────────────
class TestUnifiedDiffFidelity:
    def test_a_deleted_line_that_looks_like_a_file_header_is_still_rendered(self):
        """README: `relay diff` renders the staged diff 'so you review diffs in one click'; and
        parse_unified_diff promises per-file `additions`/`deletions`. Deleting a line that starts
        with `-- ` (an SQL comment, a signature separator) makes git emit `--- sql comment` INSIDE
        the hunk — a real deletion the review page must show, and count."""
        text = ("diff --git a/q.sql b/q.sql\nindex b7393e8..422c2b7 100644\n"
                "--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n a\n--- sql comment\n b\n")
        f, = dr.parse_unified_diff(text)
        assert f["old_path"] == "q.sql" and f["new_path"] == "q.sql"
        assert f["deletions"] == 1
        assert ("del", 2, None, "-- sql comment") in f["hunks"][0]["lines"]
        assert "sql comment" in dr.render_stdlib_html(text, {"session_id": "s", "packet": 1})

    def test_an_added_line_that_looks_like_a_file_header_is_still_counted(self):
        """The `+++ ` mirror of the same rule: `---`/`+++` only ever appear in a file HEADER,
        before the first `@@`, so a `+++ ` line inside a hunk is content."""
        text = ("diff --git a/n.md b/n.md\nindex 1..2 100644\n--- a/n.md\n+++ b/n.md\n"
                "@@ -1,1 +1,2 @@\n a\n+++ a bullet-ish line\n")
        f, = dr.parse_unified_diff(text)
        assert f["new_path"] == "n.md"
        assert f["additions"] == 1

    def test_a_new_a_deleted_and_a_binary_file_are_each_labelled(self):
        """parse_unified_diff's contract: {is_new, is_deleted, is_binary} per file; the stdlib
        renderer turns them into '(new file)' / '(deleted)' / 'Binary file differs'."""
        text = ("diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n"
                "@@ -0,0 +1,1 @@\n+hello\n"
                "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n"
                "+++ /dev/null\n@@ -1,1 +0,0 @@\n-bye\n"
                "diff --git a/logo.png b/logo.png\nindex 1..2 100644\n"
                "Binary files a/logo.png and b/logo.png differ\n")
        new, gone, binary = dr.parse_unified_diff(text)
        assert (new["is_new"], new["additions"]) == (True, 1)
        assert (gone["is_deleted"], gone["deletions"]) == (True, 1)
        assert binary["is_binary"] is True and binary["hunks"] == []
        html = dr.render_stdlib_html(text, {"session_id": "s", "packet": 2})
        assert "(new file)" in html and "(deleted)" in html and "Binary file differs" in html

    def test_a_rename_shows_both_paths(self):
        """A pure rename carries `--- a/old` / `+++ b/new`; the card must name both sides so the
        reviewer sees the move, not just a file."""
        text = ("diff --git a/old/name.py b/new/name.py\nsimilarity index 100%\n"
                "rename from old/name.py\nrename to new/name.py\n")
        f, = dr.parse_unified_diff(text)
        assert (f["old_path"], f["new_path"]) == ("old/name.py", "new/name.py")
        assert "old/name.py &rarr; new/name.py" in dr.render_stdlib_html(text, {"session_id": "s"})

    def test_an_unparsable_hunk_header_drops_the_hunk_but_never_raises(self):
        """'Best-effort: unrecognized lines are skipped, never raises on malformed input'
        (parse_unified_diff docstring)."""
        text = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ garbage @@\n+orphan line\n")
        f, = dr.parse_unified_diff(text)
        assert f["hunks"] == [] and f["additions"] == 0

    def test_lines_before_any_diff_git_header_are_ignored(self):
        """Same clause: leading noise (a `git` banner, a stray blank) must not crash or invent a
        file entry."""
        assert dr.parse_unified_diff("warning: LF will be replaced\n\n") == []
        assert dr.parse_unified_diff("") == []

    def test_no_newline_marker_is_not_a_content_line(self):
        r"""'\ No newline at end of file' is cosmetic — the inline comment says so."""
        text = ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n"
                "-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n")
        f, = dr.parse_unified_diff(text)
        assert (f["additions"], f["deletions"]) == (1, 1)
        assert all("No newline" not in ln[3] for ln in f["hunks"][0]["lines"])

    def test_crlf_and_undecodable_bytes_survive_rendering(self):
        """`relay diff` reads whatever git prints; a CRLF file and bytes that aren't UTF-8 (read
        back with surrogateescape) must render, not explode — 'self-contained, offline HTML page'
        is unconditional in the README."""
        raw = diff_for("crlf.txt", ["-old\r\n"], ["+new\r\n"]).encode() + b"+\xff\xfe binary-ish\n"
        text = raw.decode("utf-8", "surrogateescape")
        f, = dr.parse_unified_diff(text)
        assert f["deletions"] == 1 and f["additions"] == 2
        html = dr.render_stdlib_html(text, {"session_id": "s", "packet": 1})
        assert "crlf.txt" in html

    def test_a_very_large_diff_renders_without_blowing_up(self):
        """A 5 MB-ish staged file is an ordinary vendored-asset commit; the page must still be
        produced (README promises the page, with no size caveat)."""
        adds = [f"+line {i} of a large generated file\n" for i in range(120_000)]
        text = diff_for("big.txt", [], adds)
        assert len(text) > 4 * 1024 * 1024
        f, = dr.parse_unified_diff(text)
        assert f["additions"] == 120_000
        html = dr.render_stdlib_html(text, {"session_id": "s", "packet": 1})
        assert "120000" in html or "+120000" in html


# ── escaping: the page is offline HTML built from untrusted executor text ──────────────────────
class TestDiffPageEscaping:
    @pytest.mark.parametrize("payload", [
        "</script><script>alert(1)</script>", "</SCRIPT ", "<!--", "<script",
        " line separator", "emoji 🚦 and \"quotes\" & <angles>"])
    def test_js_embed_defuses_every_script_terminating_shape(self, payload):
        """js_embed's docstring: json.dumps then every literal `<` → `\\u003c`, so 'the HTML parser
        scanning for `</script` never sees a `<` character at all'."""
        out = dr.js_embed(payload)
        assert "<" not in out
        import json
        assert json.loads(out) == payload

    def test_a_diff_containing_script_tags_cannot_break_out_of_the_page(self):
        """The diff of an HTML/JS file legitimately contains `</script>` — the same case the
        js_embed docstring names as the reason it exists."""
        text = diff_for("page.html", ["-<b>x</b>\n"], ["+</script><script>alert(1)</script>\n"])
        page = dr.render_diff2html_html(text, {"session_id": "s<1>", "packet": 1},
                                        "/*js*/", "/*css*/")
        assert "</script><script>alert(1)</script>" not in page
        assert page.count("<script>") == page.count("</script>")

    def test_meta_and_paths_are_html_escaped_in_the_stdlib_page(self):
        """_render_file_card escapes each path separately so `&rarr;` stays markup; the header
        escapes session_id/packet/scope_note."""
        text = diff_for('a"<b>.py', ["-x\n"], ["+<img src=x onerror=1>\n"])
        html = dr.render_stdlib_html(text, {"session_id": "<sid>", "packet": "1&2",
                                            "scope_note": "<b>scope</b>"})
        assert "<img src=x onerror=1>" not in html
        assert "&lt;img src=x onerror=1&gt;" in html
        assert "&lt;sid&gt;" in html and "1&amp;2" in html and "&lt;b&gt;scope&lt;/b&gt;" in html
        assert "&amp;rarr;" not in html   # the arrow must not be double-escaped

    def test_an_empty_diff_renders_a_page_that_says_so(self):
        """'a diff with no changes' still has to produce the page the closing line links to."""
        html = dr.render_stdlib_html("", {"session_id": "s", "packet": 1})
        assert "No changes." in html and "0 files changed" in html


# ── the vendor integrity gate (VENDOR.md ↔ assets/vendor) ──────────────────────────────────────
class TestVendorIntegrity:
    def _vendor(self, tmp_path, js=b"JS", css=b"CSS", js_hash=None, css_hash=None):
        vd = tmp_path / "vendor"
        vd.mkdir()
        (vd / "diff2html.min.js").write_bytes(js)
        (vd / "diff2html.min.css").write_bytes(css)
        md = tmp_path / "VENDOR.md"
        md.write_text(
            f"| `assets/vendor/diff2html.min.js` | x | `{js_hash or hashlib.sha256(js).hexdigest()}` |\n"
            f"| `assets/vendor/diff2html.min.css` | x | `{css_hash or hashlib.sha256(css).hexdigest()}` |\n")
        return vd, md

    def test_a_matching_bundle_passes_and_is_used(self, tmp_path):
        """'Used only if the vendored files exist AND their SHA-256 matches what's recorded in
        VENDOR.md' (module docstring)."""
        vd, md = self._vendor(tmp_path)
        assert dr.vendor_integrity_ok(vd, md) is True
        page = dr.generate_page(diff_for("a.py", [], ["+x\n"]), {"session_id": "s"}, vd, md)
        assert "Diff2Html.html(diffText" in page

    def test_a_tampered_file_fails_the_check_and_falls_back(self, tmp_path):
        """'an integrity check against tampering/drift' — a mismatch must degrade to the stdlib
        renderer, never render the tampered bundle."""
        vd, md = self._vendor(tmp_path, js_hash="0" * 64)
        assert dr.vendor_integrity_ok(vd, md) is False
        page = dr.generate_page(diff_for("a.py", [], ["+x\n"]), {"session_id": "s"}, vd, md)
        assert "Diff2Html" not in page and 'class="file-card"' in page

    def test_a_missing_file_a_missing_manifest_and_an_empty_manifest_all_fail_closed(self, tmp_path):
        """'Any mismatch, missing file, or unreadable/unparsable VENDOR.md → False (never
        raises)'."""
        vd, md = self._vendor(tmp_path)
        (vd / "diff2html.min.css").unlink()
        assert dr.vendor_integrity_ok(vd, md) is False
        assert dr.parse_vendor_manifest(tmp_path / "nope.md") == {}
        assert dr.vendor_integrity_ok(vd, tmp_path / "nope.md") is False
        empty = tmp_path / "empty.md"
        empty.write_text("no rows here\n")
        assert dr.vendor_integrity_ok(vd, empty) is False

    def test_an_unreadable_vendor_file_is_not_an_exception(self, tmp_path):
        """Same clause — an OSError while hashing or reading must read as 'vendor unavailable'."""
        vd, md = self._vendor(tmp_path)
        (vd / "diff2html.min.js").chmod(0o000)
        try:
            assert dr.vendor_integrity_ok(vd, md) is False
            assert dr.load_vendor_bundle(vd, md) is None
        finally:
            (vd / "diff2html.min.js").chmod(0o644)

    def test_load_vendor_bundle_survives_a_file_vanishing_after_the_hash_check(self, tmp_path):
        """'Never raises — any I/O error is treated as "vendor unavailable"'. Reproduced by making
        the JS unreadable only for the read-back step."""
        vd, md = self._vendor(tmp_path)
        real_ok = dr.vendor_integrity_ok
        try:
            dr.vendor_integrity_ok = lambda *a, **k: True
            (vd / "diff2html.min.js").unlink()
            assert dr.load_vendor_bundle(vd, md) is None
        finally:
            dr.vendor_integrity_ok = real_ok

    def test_report_mentions_are_best_effort_and_deduplicated(self):
        """parse_report_mentions: 'in first-seen order, de-duplicated. Best-effort'."""
        got = dr.parse_report_mentions("bin/relay:12 and bin/relay again, plus README.md, v0.3.27")
        assert got == ["bin/relay", "README.md", "v0.3.27"]   # over-matching is documented as free
        assert dr.parse_report_mentions("") == []


# ── the board page ─────────────────────────────────────────────────────────────────────────────
def board(**over):
    data = {"leads": [], "executors": [], "relay_bin": "relay"}
    data.update(over)
    return br.render(data)


class TestBoardEscaping:
    def test_every_chip_value_is_escaped(self):
        """board_render is documented 'Pure: render(data) -> html' and routes every other value
        through `_e` (html.escape). Two chips do not, so a non-int value reaches the page raw."""
        html = board(leads=[{"session_id": "L", "project": "p"}],
                     executors=[{"session_id": "e1", "owner_lead": "L", "status": "busy",
                                 "hit_rate": "<script>alert(1)</script>",
                                 "queued": "<img src=x onerror=1>"}])
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x onerror=1>" not in html

    def test_report_and_packet_bodies_are_escaped_even_when_they_contain_script_tags(self):
        """The board inlines report/packet text in a `<pre>`; a report legitimately quoting
        `</script>` must not terminate the page's own script block or inject markup."""
        html = board(leads=[{"session_id": "L", "project": "p"}], executors=[{
            "session_id": "e1", "owner_lead": "L", "status": "reported",
            "packets": [{"n": 1, "report_path": "/r", "gist": "g",
                         "report_body": {"text": "</script><script>alert(1)</script>",
                                         "truncated": True, "path": "/tmp/<r>.md"},
                         "packet_body": {"text": "<b>packet</b>", "truncated": False}}]}])
        assert "<script>alert(1)</script>" not in html
        assert "&lt;/script&gt;" in html and "&lt;b&gt;packet&lt;/b&gt;" in html
        assert "&lt;r&gt;.md" in html

    def test_session_ids_topics_branches_and_urls_are_escaped_everywhere_they_appear(self):
        """Every executor-controlled string (id, topic, scope, worktree, model, diff URL) is
        interpolated into ids, attributes and text — all of it goes through `_e`."""
        html = board(leads=[{"session_id": 'L"x', "project": "<lead>"}], executors=[{
            "session_id": 'e"1', "owner_lead": 'L"x', "status": "reported",
            "topic": "<topic>", "scope": '"scope"', "model": "<model>",
            "worktree": "/wt/<b>", "tokens": "<tok>", "mb": "<mb>",
            "auto_closed": "<auto>", "rendered_status": "<st>", "keep": True, "heavy": True,
            "unannounced": True, "orphan": True, "agent": True, "context": "<ctx>",
            "mcp": ["<a>", "b"],
            "packets": [{"n": "<n>", "report_path": "/r", "gist": "<gist>",
                         "diff_url": 'file:///"><script>x</script>',
                         "tldr": {"outcome": "<outcome>", "status": "<s>",
                                  "risk": "<risk>", "unverified": "none"}}]}])
        for bad in ("<topic>", "<gist>", "<outcome>", "<risk>", "<model>", "<lead>",
                    "<script>x</script>", "<auto>", "<mb>", "<ctx>"):
            assert bad not in html, bad
        assert "&lt;topic&gt;" in html and "&lt;gist&gt;" in html and "&lt;risk&gt;" in html


class TestBoardShape:
    def test_an_empty_board_still_renders_a_page(self):
        """'no executor sessions yet' is the documented empty state; the page must be complete
        HTML with no lead and no executor."""
        html = board()
        assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
        assert "no executor sessions yet" in html
        assert 'data-default="home"' in html

    def test_a_lead_with_no_live_executors_says_so(self):
        html = board(leads=[{"session_id": "L", "project": "proj"}],
                     executors=[{"session_id": "e1", "owner_lead": "L", "status": "closed"}])
        assert "no live executors" in html and "Closed" in html

    def test_packet_states_cover_reported_in_flight_and_no_report(self):
        """_packet_state: report_path → ok/'reported'; current → flight/'in flight'; else
        none/'no report'."""
        html = board(leads=[{"session_id": "L", "project": "p"}], executors=[{
            "session_id": "e1", "owner_lead": "L", "status": "busy",
            "packets": [{"n": 1, "report_path": "/r"}, {"n": 2, "current": True}, {"n": 3}]}])
        assert "reported" in html and "in flight" in html and "no report" in html

    def test_an_executor_with_no_packets_says_so(self):
        html = board(leads=[{"session_id": "L", "project": "p"}],
                     executors=[{"session_id": "e1", "owner_lead": "L", "status": "busy"}])
        assert "no packets on disk yet" in html

    def test_mcp_chip_renders_list_empty_list_and_missing(self):
        """_mcp_short: a list joins with commas (empty list → 'none'); a missing key → '?'."""
        for mcp, want in (( ["linear", "chrome"], "linear,chrome"), ([], "none")):
            html = board(leads=[{"session_id": "L", "project": "p"}],
                         executors=[{"session_id": "e1", "owner_lead": "L", "status": "busy",
                                     "mcp": mcp}])
            assert f"mcp<b>{want}</b>" in html
        assert "mcp<b>?</b>" in board(leads=[{"session_id": "L", "project": "p"}],
                                     executors=[{"session_id": "e1", "owner_lead": "L",
                                                 "status": "busy"}])

    def test_a_tldr_with_only_a_status_omits_the_other_rows(self):
        """_row returns '' for a None value — the grid must not print empty Risk/Unverified
        labels the report never made."""
        html = board(leads=[{"session_id": "L", "project": "p"}], executors=[{
            "session_id": "e1", "owner_lead": "L", "status": "reported",
            "packets": [{"n": 1, "report_path": "/r", "tldr": {"status": "clean"}}]}])
        assert "<dt>Status</dt>" in html
        assert "<dt>Risk</dt>" not in html and "<dt>Unverified</dt>" not in html

    def test_scope_is_appended_to_topic_only_when_it_differs(self):
        same = board(leads=[{"session_id": "L", "project": "p"}],
                     executors=[{"session_id": "e1", "owner_lead": "L", "status": "busy",
                                 "topic": "t", "scope": "t"}])
        assert " · t" not in same
        diff = board(leads=[{"session_id": "L", "project": "p"}],
                     executors=[{"session_id": "e1", "owner_lead": "L", "status": "busy",
                                 "topic": "t", "scope": "s"}])
        assert "t · s" in diff

    def test_the_diff_link_is_offered_when_the_packet_has_one(self):
        html = board(leads=[{"session_id": "L", "project": "p"}], executors=[{
            "session_id": "e1", "owner_lead": "L", "status": "reported",
            "packets": [{"n": 1, "report_path": "/r", "diff_url": "file:///tmp/d.html"}]}])
        assert 'href="file:///tmp/d.html"' in html and "Open staged diff" in html

    def test_an_orphaned_executor_is_grouped_not_dropped(self):
        """render() groups executors whose owner_lead names no known lead under
        'Unowned / orphaned' — losing them would hide live work."""
        html = board(leads=[{"session_id": "L", "project": "p"}],
                     executors=[{"session_id": "e1", "owner_lead": "ghost", "status": "busy"},
                                {"session_id": "e2", "status": "busy"}])
        assert "Unowned / orphaned" in html
        assert "ex-e1" in html and "ex-e2" in html

    def test_a_valid_rgb_colour_renders_as_the_lead_dot(self):
        html = board(leads=[{"session_id": "L", "project": "p", "color": [12, 34, 56]}])
        assert "background:rgb(12,34,56)" in html

    @pytest.mark.parametrize("color", ["red", ["a", "b", "c"], [None, None, None]])
    def test_a_colour_that_is_not_three_numbers_falls_back_to_the_dim_dot(self, color):
        """`_lead_dot` already carries a fallback for an unusable colour ('background:var(--dim)'),
        and the board is the always-visible overview: lead_guard.list_leads pins the same rule for
        the surface it feeds — 'a single bad marker must never blank the whole list'
        (lib/lead_guard.py:625-629). A hand-edited or truncated marker must degrade to the dim dot,
        never take the page down."""
        html = board(leads=[{"session_id": "L", "project": "p", "color": color}])
        assert "background:var(--dim)" in html

    def test_warnings_render_at_every_level(self):
        html = board(warnings=[{"level": "bad", "text": "b"}, {"level": "warn", "text": "w"},
                               {"level": "info", "text": "i"}, {"text": "no level"}])
        assert "alerts" in html
        for t in ("b", "w", "i", "no level"):
            assert f"<span>{t}</span>" in html
