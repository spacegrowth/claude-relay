"""
Shape checks for skills/<name>/SKILL.md — every skill relay ships, plus the new `review` skill
specifically (backlog row 65: fork-based review as the default).

`.claude-plugin/plugin.json` carries no per-skill registration list (verified against git history:
`skills/verify/SKILL.md` landed without touching plugin.json) — every `skills/<name>/SKILL.md` is
picked up by directory convention alone. "Registered" here means exactly that: present at the
right path with the same frontmatter shape every other skill has, which is what these tests check.

Run: pytest tests/test_skills_manifest.py -v
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _skill_dirs():
    return sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())


def _frontmatter(text):
    m = FRONTMATTER_RE.match(text)
    return m.group(1) if m else None


class TestEverySkillHasTheSameShape:
    """The shape every skill in this plugin already has — pinned here so a new one can't silently
    ship without it."""

    def test_every_skill_dir_has_a_skill_md(self):
        for d in _skill_dirs():
            assert (d / "SKILL.md").is_file(), f"{d} has no SKILL.md"

    def test_every_skill_md_starts_with_frontmatter(self):
        for d in _skill_dirs():
            text = (d / "SKILL.md").read_text()
            assert _frontmatter(text) is not None, f"{d}/SKILL.md has no --- frontmatter block"

    def test_every_skill_name_matches_its_directory(self):
        for d in _skill_dirs():
            fm = _frontmatter((d / "SKILL.md").read_text())
            m = re.search(r"^name:\s*(\S+)", fm, re.MULTILINE)
            assert m and m.group(1) == d.name, f"{d}/SKILL.md's name: doesn't match its directory"

    def test_every_skill_has_a_non_empty_description(self):
        for d in _skill_dirs():
            fm = _frontmatter((d / "SKILL.md").read_text())
            assert re.search(r"^description:\s*\S", fm, re.MULTILINE), \
                f"{d}/SKILL.md has no non-empty description:"

    def test_every_skill_names_its_own_invoke_form_in_the_description(self):
        """Every existing skill's description says how to invoke it (/relay:<name> or similar) —
        the same convention a new skill must follow."""
        for d in _skill_dirs():
            text = (d / "SKILL.md").read_text()
            assert f"/relay:{d.name}" in text, f"{d}/SKILL.md never mentions /relay:{d.name}"


class TestReviewSkill:
    """skills/review/SKILL.md (backlog row 65) — the fork-based review this packet ships."""

    @property
    def _path(self):
        return SKILLS_DIR / "review" / "SKILL.md"

    def _text(self):
        return self._path.read_text()

    def test_the_skill_directory_exists(self):
        assert self._path.is_file()

    def test_frontmatter_is_well_formed(self):
        fm = _frontmatter(self._text())
        assert fm is not None
        assert re.search(r"^name:\s*review\s*$", fm, re.MULTILINE)
        assert re.search(r"^description:\s*\S", fm, re.MULTILINE)
        assert re.search(r"^arguments:\s*\[session_id\]\s*$", fm, re.MULTILINE)

    def test_describes_launching_a_same_model_fork(self):
        text = self._text()
        assert 'Agent(subagent_type: "fork")' in text

    def test_fixed_prompt_covers_every_step_the_packet_names(self):
        text = self._text()
        for needle in (
            "relay verify",                       # runs verify, quotes the verdict
            "TL;DR",                              # quotes the report's TL;DR verbatim
            "FULL staged diff",                   # reads the full diff, not a summary
            "acceptance",                         # runs the packet's acceptance commands
            "blocker|should-fix|note",            # numbered findings in the required shape
            "Recommendation:",                    # one-line recommendation
            "under 40 lines",                     # the length cap
        ):
            assert needle in text, f"missing {needle!r}"

    def test_tells_the_lead_to_read_findings_not_the_diff(self):
        text = self._text()
        assert "Read the findings, not the diff" in text
        assert "sign-off-gated" in text           # inline fallback for gated paths

    def test_points_at_verify_with_findings_for_the_attestation(self):
        text = self._text()
        assert "--diff-reviewed --findings" in text

    def test_no_relay_repo_special_case(self):
        text = self._text()
        assert "every project" in text
        assert "no special case for relay's own repo" in text.lower() or \
               "no special case" in text.lower()
