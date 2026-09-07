"""
report_verify — machine-check an executor's REPORT against its STAGED REALITY (backlog §6b, task
#7), with the §9 temper baked into every surface.

WHAT THIS IS NOT, stated first because it is the whole design constraint. Field data behind §9:
across ~15 real reports a counts-verifier would have caught exactly ONE thing (a lint miss). Every
*dangerous* problem was premise-level — wrong oracle, wrong write-side, suite green in the wrong
venv — and every one of those is invisible to re-running the declared commands. So this module is
deliberately built to be UNDER-trusted:

- the passing verdict is `COUNTS-MATCH`, never "PASS"/"VERIFIED"/"clean" — vocabulary that cannot
  be over-read at a glance;
- `CAVEAT` is emitted on every single output, not just failures;
- risk flags and UNVERIFIED lines are ECHOED verbatim, never summarised, never absorbed — the
  verifier's job is to put them in front of the lead, not to grade them;
- "did not run" is rendered differently from "ran and matched" (the §9.6a lesson): declared test
  commands are listed as NOT RE-RUN unless the caller explicitly asks for `--rerun`.

Everything here is pure — it takes the report text plus a `Reality` snapshot of git facts that
bin/relay gathers, and returns a result dict + rendered lines. That keeps the whole verdict surface
unit-testable without a git repo.

Where §6b leaves the mechanism open, this module prefers the CHEAP DETERMINISTIC check and says so
in its own output rather than guessing cleverly:
- claimed-changed files are read from the report's "What changed" section when it has one (scoped);
  with no such section the scan falls back to the whole report and the claimed-not-staged finding
  is DOWNGRADED to advisory, because a whole-report scan cannot tell "I changed x.py" from "I read
  x.py" and a false accusation is worse than a missed one here;
- `--rerun` only ever executes pytest-shaped commands with no shell metacharacters (see
  `rerunnable`): a report is untrusted text, and this tool must not become a way for one to run
  arbitrary shell in the lead's worktree.
"""
import re

# ── verdict vocabulary ────────────────────────────────────────────────────────────────────────
# Three words, none of which can be misread as "the report is true". There is deliberately no
# success-flavoured verdict: the best outcome this tool can report is that some numbers agreed.
MALFORMED = "MALFORMED"        # the report violates #6's TL;DR contract — unreadable as a report
MISMATCH = "MISMATCH"          # a claim contradicts staged reality
INCONCLUSIVE = "INCONCLUSIVE"  # a check the caller ASKED FOR could not be completed — see below
COUNTS_MATCH = "COUNTS-MATCH"  # the checkable numbers line up. That is ALL it means. See CAVEAT.

# INCONCLUSIVE exists because of the §9.6a lesson this module is built around: "did not run" must
# never look like "ran and matched". It fires ONLY when the caller explicitly asked for a check
# (`--rerun`) and that check produced no comparison — the declared command yielded no `N passed`,
# or there was nothing declared to compare against. Observed live on the first run of this tool:
# `python3 -m pytest` inside a subprocess resolved to an interpreter with no pytest installed, so
# the re-run "succeeded" with zero output — which under a three-verdict vocabulary would have
# stamped COUNTS-MATCH on a suite that never ran. That is the wrong-venv failure §9 names, and it
# would have come from this tool's own reporting. The DEFAULT (no --rerun) path is never
# INCONCLUSIVE: not asking for a re-run is a stated choice, rendered as "NOT RE-RUN".
VERDICT_PRECEDENCE = [MALFORMED, MISMATCH, INCONCLUSIVE, COUNTS_MATCH]
EXIT_CODES = {COUNTS_MATCH: 0, MISMATCH: 1, MALFORMED: 2, INCONCLUSIVE: 3}

# The §9 temper, verbatim-level. Printed on EVERY verdict, quoted in skills/verify/SKILL.md and in
# the README. If you are editing this, the bar is: a lead who reads only this block must come away
# knowing the tool did not, and cannot, tell them the report is true.
CAVEAT = [
    "COUNTS-MATCH means the numbers line up. It must NEVER be read as \"the report is true\".",
    "This tool re-checks declared files and counts against staged reality. Premise-level",
    "wrongness — wrong oracle, wrong write-side, suite green in the wrong venv — is INVISIBLE",
    "to it: across ~15 field reports a counts-verifier would have caught exactly one thing.",
    "The lead's judgement on the staged diff stays the real check. This never replaces it.",
]

# ── #6's TL;DR contract ───────────────────────────────────────────────────────────────────────
# Four fields, verbatim, in this order, right after the one-sentence outcome line. Absence is not
# "nothing to report" — absence is MALFORMED. That is #6's contract and this is where it is
# mechanically enforced for the first time.
TLDR_FIELDS = [("Status:", "status"), ("Risk flags:", "risk_flags"),
               ("UNVERIFIED:", "unverified"), ("Changed:", "changed")]
STATUS_VALUES = ("clean", "clean-with-caveats", "blocked", "partial")
TLDR_SCAN_LINES = 40  # the block sits at the top by contract; don't scan a whole 100-line report

_FIELD_LINE_RE = re.compile(r"^\s*[-*>#\s]*(" + "|".join(re.escape(p) for p, _ in TLDR_FIELDS) + r")(.*)$")


def _demark(line):
    """A report line with markdown ornament (heading hashes, bullets, bold, blockquote) stripped
    and whitespace collapsed — so field detection survives an executor that bulleted its TL;DR."""
    return " ".join(line.strip().lstrip("-*>#").strip().strip("*").strip().split())


def parse_tldr(text):
    """Parse the mandatory TL;DR block. Returns a dict of the five lead-facing values (outcome +
    the four fields; None when absent) plus `problems`: a list of human-readable contract
    violations. Non-empty `problems` ⇒ MALFORMED — that is #6's contract, mechanised."""
    out = {"outcome": None, "status": None, "risk_flags": None, "unverified": None,
           "changed": None, "problems": []}
    lines = text.splitlines()

    # The outcome sentence: first non-empty line, and it must be a plain sentence — not a heading,
    # not a "Report:" label, not the TL;DR block starting early with no outcome line at all.
    for raw in lines:
        if raw.strip():
            out["outcome"] = raw.strip()
            if raw.lstrip().startswith("#"):
                out["problems"].append("first line is a heading, not a plain outcome sentence")
            elif _FIELD_LINE_RE.match(raw):
                out["problems"].append("first line is a TL;DR field — the outcome sentence is missing")
            elif re.match(r"^\s*(report|summary)\s*:", raw, re.IGNORECASE):
                out["problems"].append("first line carries a label prefix, not a plain outcome sentence")
            break
    else:
        out["problems"].append("report is empty")
        return out

    # The four fields, with their line positions (order is part of the contract).
    seen = {}
    for i, raw in enumerate(lines[:TLDR_SCAN_LINES]):
        m = _FIELD_LINE_RE.match(_demark(raw) if raw.strip().startswith(("-", "*", ">", "#")) else raw)
        if not m:
            continue
        prefix, rest = m.group(1), m.group(2)
        key = dict(TLDR_FIELDS)[prefix]
        if key not in seen:
            seen[key] = i
            out[key] = " ".join(rest.split()) or None

    order = []
    for prefix, key in TLDR_FIELDS:
        if key not in seen:
            out["problems"].append(f"missing mandatory TL;DR line `{prefix}`")
        elif out[key] is None:
            out["problems"].append(f"TL;DR line `{prefix}` is present but empty")
        else:
            order.append((seen[key], prefix))

    if order != sorted(order) :
        out["problems"].append("TL;DR fields are out of the mandated order "
                               "(Status / Risk flags / UNVERIFIED / Changed)")

    if out["status"] and out["status"].split()[0].strip(".,;") not in STATUS_VALUES:
        out["problems"].append(f"Status value {out['status']!r} is not one of "
                               f"{' / '.join(STATUS_VALUES)}")
    return out


def is_none_value(value):
    """True when a TL;DR field says the literal 'none' — the format's way of asserting emptiness
    ON PURPOSE, which is very different from the line being absent."""
    return bool(value) and value.strip().rstrip(".").lower() == "none"


# ── "this report changed nothing, on purpose" ─────────────────────────────────────────────────
# Row 70 item 3 (2026-09-06 field note §3). An OPS packet — investigate, verify, answer a question
# — legitimately stages nothing, and its report says so ("Changed: none", "What changed: nothing
# staged"). `claimed_paths` then returns an empty set, which auto-close's landed rule read as "the
# landed test is unavailable here", so a finished ops session sat out the full idle timer with
# nothing left to do. This is the POSITIVE assertion that separates "the report says it changed
# nothing" from "we could not parse any claims", and only that first shape may land.
# The trailing group lets a none-value carry an explanation ("none — investigation only") without
# stopping it from reading as none; anything that names work stops matching at the first word.
_NOTHING_RE = re.compile(
    r"^\s*[-*>\s]*(?:none|nothing(?:\s+(?:staged|changed))?|no\s+files?(?:\s+(?:changed|staged))?|"
    r"n/?a|no\s+(?:code\s+)?changes?)\b\s*(?:[—–\-:;,(].*)?$",
    re.IGNORECASE)


def says_nothing_changed(text):
    """True when the report POSITIVELY asserts it changed nothing — its TL;DR `Changed:` field is a
    none-value, or every non-blank line of its "What changed" section is. Absence of either says
    nothing at all and reads as False."""
    tl = parse_tldr(text)
    if tl.get("changed") and _NOTHING_RE.match(tl["changed"]):
        return True
    section = what_changed_section(text)
    if section is None:
        return False
    lines = [ln for ln in section.splitlines() if ln.strip()]
    return bool(lines) and all(_NOTHING_RE.match(ln) for ln in lines)


def clean_no_change_report(text):
    """True when the report is a FINISHED ops report: a well-formed `Status: clean` TL;DR whose
    "What changed"/`Changed:` says it staged nothing (`says_nothing_changed`). Anything else —
    blocked/partial/caveated status, a malformed or absent TL;DR, a report that names files — is
    False, because parking on this signal closes a session and work that is still owed must not be
    closed out from under its lead."""
    tl = parse_tldr(text)
    if tl.get("problems") or (tl.get("status") or "").strip().rstrip(".") != "clean":
        return False
    return says_nothing_changed(text)


# ── claimed files ─────────────────────────────────────────────────────────────────────────────
# A path mention is only a CLAIM if it looks like a repo path. diff_render's mention regex is
# deliberately permissive (it intersects against staged files afterwards, so over-matching there
# is free) — here over-matching would produce a false MISMATCH accusation, so this is stricter:
# either it has a "/", or it is a bare filename whose extension is alphabetic. That drops the
# known false-positive class, version numbers like "0.3.27" reading as `.27` files.
# The bare-filename branch caps the extension at 6 chars so a dotted Python identifier
# (`diff_render.parse_report_mentions`) doesn't read as a file. Both this and the word-pair case
# below were found by running this tool on its own report — ordinary technical prose produces them.
# The path branch's segment class is unicode-aware (BUG-lib-1: an ASCII-only class truncated
# `tests/tëst_data.py` into the accusable prefix `tests/t`) but still excludes: whitespace and
# path/prose punctuation (`/:,;()[]` backtick/quote — unchanged from before), PLUS `~` (so
# `~/.relay-tasks/...` can't be swept in as one unbroken match starting at the tilde — the
# existing `(?<![\w/.-])` lookbehind alone no longer blocked that once `~` became a legal segment
# character) and `*` (so a glob like `lib/*.py` isn't read as a literal path — AMBIGUOUS-lib-4's
# decidable half). The trailing `(?![\w-])` refuses a candidate cut off mid-word.
_CLAIM_RE = re.compile(
    r'(?<![\w/.-])((?:[^\s/:,;()\[\]`"\'~*]+/)+[^\s/:,;()\[\]`"\'~*]+|'
    r'[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]{0,5})'
    r'(?::\d+(?:-\d+)?)?(?![\w-])')
_HAS_EXTENSION_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,5}$")


def plausible_claims(paths, repo_entries=(), staged=()):
    """Filter path-shaped matches down to ones that could really be repo files.

    A slash alone does not make a path: "tolerates bulleted/ornamented blocks" is English prose,
    and accusing an executor of not staging `bulleted/ornamented` would be a false MISMATCH — the
    expensive error here, since a false accusation costs more trust than a missed catch. So a
    candidate survives only if it has a file extension, or its first segment is a real top-level
    entry in the repo (which is how extension-less paths like `bin/relay` stay checkable), or it
    is already staged (in which case it is confirmed, not accused).

    With no `repo_entries` (worktree gone) this keeps only extension-bearing paths — deliberately
    the weaker, non-accusing direction.

    A BARE name (no "/") must be a real top-level repo entry to survive — otherwise a technical
    term that merely happens to carry a file-shaped extension (`tier_windows.json`, mentioned in
    prose but never a repo file) becomes an accusable claim on extension alone (BUG-lib-10b)."""
    kept = []
    for p in paths:
        if p in staged:                 # confirmed, not accused
            kept.append(p)
        elif "/" in p:
            if _HAS_EXTENSION_RE.search(p) or p.split("/")[0] in repo_entries:
                kept.append(p)
        elif p in repo_entries:         # a BARE filename must be a real top-level repo entry
            kept.append(p)
    return kept

_WHAT_CHANGED_RE = re.compile(r"^what changed\b", re.IGNORECASE)
# A real markdown heading always ends the section. A fully-bold line ends it too (reports use bold
# pseudo-headings), but ONLY when the bold text names no file — a bold LEAD-IN like
# "**`lib/report_verify.py` (new) — the engine.**" is section CONTENT, not a new section.
# Found by running this tool on its own report: that lead-in terminated the section on its first
# line, so the section parsed EMPTY, so zero claims were checked, and the run still said
# COUNTS-MATCH. Silence read as agreement — the exact failure this module exists to prevent.
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_BOLD_HEADING_RE = re.compile(r"^\s*\*\*([^*/`]+)\*\*\s*:?\s*$")


_BULLET_RE = re.compile(r"^\s{0,3}[-*]\s")
# Depth-aware terminator match: captures the indent so a bulleted section can tell a SIBLING
# bullet (same indent as the opener, or shallower — ends the section) from a nested sub-bullet
# (deeper than the opener — that's the section's own body, e.g. "- What changed:\n  - a.py\n  -
# b.py"). Unlike `_BULLET_RE` above (capped at 0-3 spaces, used only to recognise a bulleted
# OPENER), this has no indent cap: a sub-bullet's indent can be anything deeper than the opener's.
_ANY_BULLET_RE = re.compile(r"^(\s*)[-*]\s")


def _ends_section(raw, bulleted=False, opener_indent=0):
    if _HEADING_RE.match(raw) or _BOLD_HEADING_RE.match(raw):
        return True
    if not bulleted:
        return False
    m = _ANY_BULLET_RE.match(raw)
    return bool(m) and len(m.group(1)) <= opener_indent


def what_changed_section(text):
    """The report's "What changed" section body, or None when it has no such section OR the
    section is empty. Runs from the heading/bullet naming it to the next heading.

    Returning None for an EMPTY section is deliberate, not laziness: an empty section yields zero
    claims, and zero claims would silently render as "everything the report claimed was staged".
    None instead sends the caller down the unscoped/advisory path, which says out loud that it
    could not scope the claims. Best-effort parsing must degrade to LOUD, never to agreement."""
    lines = text.splitlines()
    start = None
    bulleted = False
    opener_indent = 0
    body = []
    for i, raw in enumerate(lines):
        demarked = _demark(raw)
        m = _WHAT_CHANGED_RE.match(demarked)
        if m:
            # BUG-lib-2: a bullet/heading opener can carry the FIRST claim on its own line
            # ("- What changed: src/app.py:2 — appended a line."). The body used to start on the
            # NEXT line, discarding whatever the opener itself said — include the opener's
            # remainder (after the section name and its punctuation) as the first body line.
            rest = demarked[m.end():].lstrip(" :—-")
            if rest:
                body.append(rest)
            # BUG-lib-3: a bullet-opened section has no heading to end it, so it used to run to
            # EOF and swallow later bullets ("What I verified", a plain aside) as if they were
            # more "What changed" content. A bullet opener ends its section at the next SIBLING
            # bullet (same indent as the opener, or shallower) — never at a deeper, nested
            # sub-bullet, which is the section's own body ("- What changed:\n  - a.py\n  - b.py").
            bulleted = bool(_BULLET_RE.match(raw))
            opener_indent = (len(raw) - len(raw.lstrip(" "))) if bulleted else 0
            start = i + 1
            break
    if start is None:
        return None
    for raw in lines[start:]:
        if _ends_section(raw, bulleted=bulleted, opener_indent=opener_indent):
            break
        body.append(raw)
    return "\n".join(body) if "\n".join(body).strip() else None


def claimed_paths(text):
    """(paths, scoped) — repo-relative paths the report CLAIMS it changed, first-seen order.

    `scoped` is True when they came from a "What changed" section (trustworthy enough to accuse
    on) and False when the whole report was scanned (a mention there may be a file merely read, so
    the caller must downgrade)."""
    section = what_changed_section(text)
    body, scoped = (section, True) if section is not None else (text, False)
    seen = []
    for m in _CLAIM_RE.finditer(body):
        p = m.group(1)
        if p not in seen:
            seen.append(p)
    return seen, scoped


# ── declared tests ────────────────────────────────────────────────────────────────────────────
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped)\b", re.IGNORECASE)
_CMD_RE = re.compile(r"`([^`\n]+)`")
# An optional absolute/relative directory prefix is allowed on the interpreter, but ONLY when the
# basename is python/python3/pytest. A report that pins its venv (`/path/to/.venv/bin/python -m
# pytest`) is exactly the case §9 cares about — "suite green in the wrong venv" — so refusing to
# re-run a pinned interpreter would blind this tool to the one thing it could usefully see. The
# allowlist stays a basename allowlist: no arbitrary binary ever becomes runnable.
_PYTEST_CMD_RE = re.compile(r"^(?:[\w.\-/]*/)?(?:python3?(?:\s+-m\s+pytest)|pytest)\b")
_SHELL_META_RE = re.compile(r"[;&|><$`\\\n]|\$\(")


def declared_counts(text):
    """Test counts the report declares, as [(n, kind)] — e.g. [(749, 'passed')]. Deduplicated,
    first-seen order. Purely descriptive: nothing here is checked unless --rerun is given."""
    out = []
    for m in _COUNT_RE.finditer(text):
        item = (int(m.group(1)), m.group(2).lower().rstrip("s") if m.group(2).lower() != "passed"
                else "passed")
        if item not in out:
            out.append(item)
    return out


def declared_commands(text):
    """Backticked commands the report declares having run, first-seen order, deduplicated. Only
    the ones this tool would be willing to re-run are returned (see `rerunnable`) — a report is
    untrusted text and the rest are none of our business to execute."""
    out = []
    for m in _CMD_RE.finditer(text):
        cmd = " ".join(m.group(1).split())
        if rerunnable(cmd) and cmd not in out:
            out.append(cmd)
    return out


def rerunnable(cmd):
    """True for a command `--rerun` is allowed to execute: pytest-shaped, and free of shell
    metacharacters so it can be run WITHOUT a shell (argv-only). This allowlist is a security
    boundary, not a convenience filter — the input is text an executor wrote."""
    return bool(_PYTEST_CMD_RE.match(cmd)) and not _SHELL_META_RE.search(cmd)


def passed_count(output):
    """The pytest `N passed` count in a run's output, or None if it isn't there."""
    hits = re.findall(r"(\d+)\s+passed\b", output)
    return int(hits[-1]) if hits else None


# ── the staged-confirmation line ──────────────────────────────────────────────────────────────
_STAGED_CLAIM_RE = re.compile(
    r"\bstaged\b(?![^\n]*\bnot\s+staged\b)", re.IGNORECASE)
_STAGED_CONFIRM_RE = re.compile(
    r"\bstaged\b[^\n]*\b(not committed|uncommitted|never committed|ready for the lead|no commit)\b"
    r"|\b(not committed|uncommitted)\b[^\n]*\bstaged\b", re.IGNORECASE)


def claims_staged(text):
    """True when the report carries the REPORT FORMAT's staged-confirmation line ("changes are
    staged, not committed"). Its presence is what makes an EMPTY index a hard contradiction rather
    than merely odd."""
    return bool(_STAGED_CONFIRM_RE.search(text))


# ── subdirectory-relative claims ──────────────────────────────────────────────────────────────
# Row 70 item 6 (2026-09-06, gm-app-000221-r6). A report's "What changed" listed `lib/types.ts`,
# `components/DetailPane.svelte` — relative to `app/src`, which the section header itself named.
# All 21 files were staged under `app/src/...`, so every one read as "claimed, NOT staged" and the
# run stamped MISMATCH: a pure path-resolution false positive, and exactly the false accusation
# this module's own filters exist to avoid. A claimed path that is a suffix of exactly ONE staged
# path is the same file written from a different root, so it is CONFIRMED. Two or more matches is
# genuinely ambiguous — the tool must not pick one, so the claim stays a mismatch and the
# candidates are named. The match is segment-anchored ("/" + claim): `lib/types.ts` must not match
# `app/mylib/types.ts`, which is a different file.


def resolve_claim_suffixes(claims, staged, repo_files=None):
    """({claim: staged_path} for unique matches, {claim: [staged_paths]} for ambiguous ones).

    A claim already in `staged`, or that names a REAL file in the repo (`repo_files`, when the
    caller supplies it), is never resolved: it means the file it names, and quietly re-pointing it
    at a deeper staged path would hide a real mismatch instead of finding one."""
    known = set(repo_files or ())
    resolved, ambiguous = {}, {}
    staged = list(staged or ())
    for p in claims:
        if p in staged or p in known:
            continue
        matches = [sp for sp in staged if sp.endswith("/" + p)]
        if len(matches) == 1:
            resolved[p] = matches[0]
        elif len(matches) > 1:
            ambiguous[p] = matches
    return resolved, ambiguous


# ── the verdict ───────────────────────────────────────────────────────────────────────────────
def _finding(level, code, text):
    return {"level": level, "code": code, "text": text}


def verify(report_text, reality):
    """Machine-check `report_text` against `reality` (a dict of git facts gathered by the caller:
    `staged` / `modified` / `untracked` name lists, `commits_since` count, `rerun` results).
    Returns the full result dict — verdict, tldr, findings, and the echo material. Pure."""
    tldr = parse_tldr(report_text)
    staged = list(reality.get("staged") or [])
    modified = set(reality.get("modified") or [])
    claims, scoped = claimed_paths(report_text)
    claims = plausible_claims(claims, reality.get("repo_entries") or (), staged)
    resolved, ambiguous = resolve_claim_suffixes(claims, staged, reality.get("repo_files"))
    findings = []

    claimed_staged = [p for p in claims if p in staged or p in resolved]
    claimed_missing = [p for p in claims if p not in staged and p not in resolved]
    unclaimed = [p for p in staged if p not in claims and p not in set(resolved.values())]

    for p, match in resolved.items():
        findings.append(_finding("note", "claimed-path-resolved",
                                 f"{p} — resolved to staged `{match}` (the report writes its paths "
                                 f"relative to a subdirectory; unique suffix match, so this is "
                                 f"confirmed, not accused)"))
    for p in claimed_missing:
        why = ("modified in the worktree but NOT staged" if p in modified
               else "not staged, and not modified in the worktree")
        if p in ambiguous:
            findings.append(_finding("note", "claimed-path-ambiguous",
                                     f"{p} — matches {len(ambiguous[p])} staged paths "
                                     f"({', '.join('`%s`' % m for m in ambiguous[p])}); ambiguous, "
                                     f"so nothing is assumed — say which one you meant"))
        if scoped:
            findings.append(_finding("mismatch", "claimed-not-staged",
                                     f"{p} — claimed under \"What changed\" but {why}"))
        else:
            findings.append(_finding("note", "claimed-not-staged-unscoped",
                                     f"{p} — mentioned in the report but {why} "
                                     f"(advisory: no \"What changed\" section to scope the claim)"))
    for p in unclaimed:
        findings.append(_finding("note", "staged-not-claimed",
                                 f"{p} — staged but not named in the report"))

    # staged-not-committed. The falsifiable half is the index: a report that says "staged, not
    # committed" over an EMPTY index is a flat contradiction. Commits on the branch are only ever
    # advisory — the lead legitimately commits earlier packets from a reused session.
    if not staged:
        if claims_staged(report_text):
            findings.append(_finding("mismatch", "index-empty",
                                     "the report confirms its work is staged, but `git diff --cached` "
                                     "is EMPTY — nothing is staged in the session's worktree"))
        elif claims:
            findings.append(_finding("mismatch", "index-empty",
                                     f"the report names {len(claims)} changed file(s) but "
                                     f"`git diff --cached` is EMPTY"))
        else:
            findings.append(_finding("note", "index-empty",
                                     "nothing staged, and the report claims no files — consistent, "
                                     "but there is no work here to review"))
    elif not claims_staged(report_text):
        findings.append(_finding("note", "no-staged-confirmation",
                                 "no staged-not-committed confirmation line found in the report "
                                 "(the REPORT FORMAT asks for one)"))

    commits = reality.get("commits_since")
    if commits:
        findings.append(_finding("note", "commits-since",
                                 f"{commits} commit(s) on this branch since the packet was sent — "
                                 f"GATES say stage, never commit. Expected only if the LEAD "
                                 f"committed an earlier packet from this session."))

    # Declared tests. Default is NOT re-run, and that renders as its own state — "did not run" must
    # never look like "ran and matched" (§9.6a).
    # `rerun` is None ⇒ not requested (a stated choice, never INCONCLUSIVE); a list ⇒ requested,
    # and every entry that yields no comparison downgrades the verdict rather than being a note.
    rerun = reality.get("rerun")
    if rerun is not None and not rerun:
        findings.append(_finding("inconclusive", "rerun-nothing-to-run",
                                 "--rerun was requested, but the report declares no re-runnable "
                                 "(pytest-shaped, shell-metacharacter-free) command — the check "
                                 "you asked for did NOT happen"))
    if rerun:
        for r in rerun:
            if r.get("passed") is None:
                findings.append(_finding("inconclusive", "rerun-unparsable",
                                         f"`{r['cmd']}` was re-run but produced no `N passed` "
                                         f"count — the suite did not run, or ran somewhere it "
                                         f"could not report. NOT a match; nothing was compared"))
            elif r.get("declared") is None:
                findings.append(_finding("inconclusive", "rerun-nothing-declared",
                                         f"`{r['cmd']}` re-ran: {r['passed']} passed — but the "
                                         f"report declares no count to compare it against"))
            elif r["passed"] != r["declared"]:
                findings.append(_finding("mismatch", "counts-differ",
                                         f"`{r['cmd']}` re-ran: {r['passed']} passed, but the "
                                         f"report declares {r['declared']} passed"))
    for n, kind in declared_counts(report_text):
        if kind in ("failed", "error") and n:
            findings.append(_finding("note", "declared-failures",
                                     f"the report itself declares {n} {kind} — read it, this tool "
                                     f"is not judging that"))

    if tldr["problems"]:
        verdict = MALFORMED
    elif any(f["level"] == "mismatch" for f in findings):
        verdict = MISMATCH
    elif any(f["level"] == "inconclusive" for f in findings):
        verdict = INCONCLUSIVE
    else:
        verdict = COUNTS_MATCH

    return {"verdict": verdict, "tldr": tldr, "findings": findings, "claims": claims,
            "claims_scoped": scoped, "claimed_staged": claimed_staged,
            "claimed_missing": claimed_missing, "unclaimed": unclaimed, "staged": staged,
            "declared_counts": declared_counts(report_text),
            "declared_commands": declared_commands(report_text), "rerun": rerun}


# ── auto-commit clearance (task #16 phase 2, §6f + §9) ────────────────────────────────────────
# The gate that lets an autonomous lead commit an executor's work WITHOUT asking. Five conditions,
# ALL required. This module can only ever check three of them; the other two are the lead's, and
# the design point is that it must say so rather than quietly scoring 3/5 as a pass.
#
# §9 is binding here more than anywhere else in this file: **the verifier gates the AUTOMATION, it
# never replaces the lead reading the diff.** A COUNTS-MATCH is the ceiling of what the machine
# knows (see CAVEAT) — it is a necessary condition for auto-commit, never a sufficient one. That is
# exactly why conditions 3 and 5 exist and why they are attestations rather than inferences: if
# this tool guessed at them, it would be manufacturing the very "the report looked clean" judgement
# that #16 phase 2 was blocked on until #7 existed.
CLEARED = "CLEARED"

# Condition 4's sign-off list. The generic entries are the §6f stop-list ("core logic, ledgers,
# parity/golden tests, migrations, deploys"); the relay/* entries are THIS repo's own dogfooding
# instances of it, named by the packet. Substring match on the repo-relative path — deliberately
# blunt, because a false "sign-off needed" costs one question and a false clearance costs trust.
SIGNOFF_PATH_MARKERS = [
    ("hooks/", "relay's hooks — the wake/gate paths autonomy itself rides on"),
    ("lib/lead_guard.py", "relay's lead-guard: wake, gate and marker state"),
    ("migration", "migrations"),
    ("parity", "parity tests"),
    ("golden", "golden tests"),
    ("schema", "schema definitions"),
    ("deploy", "deploy paths"),
]
# Ledger FORMAT changes are sign-off-gated too, and they are not a path — they are a shape of edit.
# Cheap deterministic proxy: the staged diff adds or removes a ledger-write call.
_LEDGER_EDIT_RE = re.compile(r"^[+-].*append_ledger\s*\(", re.MULTILINE)


def signoff_hits(staged, staged_diff=""):
    """[(path_or_marker, why)] for every sign-off-gated thing this staged work touches. Empty list
    ⇒ condition 4 holds."""
    hits = []
    for path in staged:
        for marker, why in SIGNOFF_PATH_MARKERS:
            if marker in path:
                hits.append((path, why))
                break
    if staged_diff and _LEDGER_EDIT_RE.search(staged_diff):
        hits.append(("(ledger format)", "the staged diff adds or removes an append_ledger call"))
    return hits


def clearance(result, staged_diff="", in_plan=False, diff_reviewed=False):
    """Evaluate the five auto-commit conditions. Returns
    {cleared: bool, reason: str|None, conditions: [{n, name, ok, detail, checkable}]}.

    `reason` is the slug for the NOT-CLEARED-BECAUSE-<reason> line — the FIRST failed condition in
    numeric order, so the headline is stable and the lead is pointed at the earliest problem.
    Pure: the caller supplies the attestations, this never infers them."""
    tldr = result["tldr"]
    conds = []

    v = result["verdict"]
    conds.append({"n": 1, "name": "verify verdict is COUNTS-MATCH", "checkable": True,
                  "ok": v == COUNTS_MATCH, "detail": f"verdict is {v}",
                  "slug": f"verdict-is-{v}"})

    # Condition 2 — all three TL;DR fields, each failing with its own slug so the announce can name
    # WHICH one stopped it. `clean-with-caveats` stops: the caveats are the point.
    status_ok = (tldr["status"] or "").strip().rstrip(".").lower() == "clean"
    risk_ok = is_none_value(tldr["risk_flags"])
    unver_ok = is_none_value(tldr["unverified"])
    if not status_ok:
        detail, slug = f"Status is {tldr['status']!r}, not 'clean'", "status-not-clean"
    elif not risk_ok:
        detail, slug = f"Risk flags: {tldr['risk_flags']}", "risk-flags-present"
    elif not unver_ok:
        detail, slug = f"UNVERIFIED: {tldr['unverified']}", "unverified-claims-present"
    else:
        detail, slug = "Status: clean / Risk flags: none / UNVERIFIED: none", "tldr-not-clean"
    conds.append({"n": 2, "name": "TL;DR is clean / none / none", "checkable": True,
                  "ok": status_ok and risk_ok and unver_ok, "detail": detail, "slug": slug})

    conds.append({"n": 3, "name": "the packet was in the approved plan", "checkable": False,
                  "ok": bool(in_plan), "slug": "not-attested-in-plan",
                  "detail": ("attested by the lead (--in-plan)" if in_plan else
                             "NOT attested — autonomy is within the plan, never expands it")})

    hits = signoff_hits(result["staged"], staged_diff)
    conds.append({"n": 4, "name": "nothing sign-off-gated is touched", "checkable": True,
                  "ok": not hits, "slug": "signoff-gated-path-touched",
                  "detail": ("; ".join(f"{p} ({w})" for p, w in hits) if hits
                             else "no sign-off-gated path in the staged set")})

    conds.append({"n": 5, "name": "the lead has READ the staged diff", "checkable": False,
                  "ok": bool(diff_reviewed), "slug": "not-attested-diff-reviewed",
                  "detail": ("attested by the lead (--diff-reviewed)" if diff_reviewed else
                             "NOT attested — the verifier gates the automation, it never "
                             "replaces reading the diff")})

    failed = [c for c in conds if not c["ok"]]
    return {"cleared": not failed, "reason": failed[0]["slug"] if failed else None,
            "conditions": conds, "signoff_hits": hits}


def render_clearance(clr):
    """The --for-autocommit block as (line, styles) pairs, appended under the normal verify output.

    Note what CLEARED does and does not assert: conditions 3 and 5 are the LEAD's attestations,
    passed in as flags. A CLEARED line therefore means "the machine-checkable conditions hold AND
    the lead asserted the other two" — it is a record of a decision, not a discovery. The block
    says so on every run so the line can't be quoted out of that context later."""
    L = []

    def add(text="", *styles):
        L.append((text, styles))

    add("─" * 74, "dim")
    add("  AUTO-COMMIT CLEARANCE (#16 phase 2) — all five conditions must hold", "bold")
    for c in clr["conditions"]:
        mark = "✓" if c["ok"] else "✗"
        who = "" if c["checkable"] else "  [lead's attestation — this tool cannot check it]"
        add(f"    {mark} {c['n']}. {c['name']}{who}", *(("dim",) if c["ok"] else ("red",)))
        add(f"         {c['detail']}", "dim")
    add()
    if clr["cleared"]:
        add(f"  AUTO-COMMIT: {CLEARED}", "yellow", "bold")
        add("  This clears the AUTOMATION only. It is not a statement that the work is correct —", "yellow")
        add("  see the caveat above. The lead owns the commit and everything in it.", "yellow")
    else:
        add(f"  AUTO-COMMIT: NOT-CLEARED-BECAUSE-{clr['reason']}", "red", "bold")
        add("  Fall back to today's behaviour: stop and ask the user, naming the condition above.", "red")
    add("─" * 74, "dim")
    return L


# ── fork-review findings (skills/review/SKILL.md, condition-5 attestation) ──────────────────────
_FINDING_LINE_RE = re.compile(r"^\s*\d+\.\s+\S")


def count_findings(text):
    """Count numbered finding lines in a `/relay:review` fork's returned findings text (the skill's
    fixed prompt: 'numbered findings, each `file:line — blocker|should-fix|note — one sentence`').
    A plain line-count, not a format validator — a fork's exact formatting can vary a little; this
    only needs to give `report_reviewed`'s ledger event a useful count, never to gate on shape."""
    return sum(1 for line in text.splitlines() if _FINDING_LINE_RE.match(line))


# ── rendering ─────────────────────────────────────────────────────────────────────────────────
# Returns (text, styles) pairs so bin/relay can colour without this module importing a terminal
# layer — and so tests can assert on the text of every line, including the caveat.
def render(result, session_id, packet):
    """The full verify output as a list of (line, styles) pairs. The caveat block is emitted for
    EVERY verdict, not only COUNTS-MATCH — a MISMATCH is just as easy to over-read in the other
    direction ("it found nothing else, so the rest must be fine")."""
    v = result["verdict"]
    style = {COUNTS_MATCH: "yellow", MISMATCH: "red", MALFORMED: "red", INCONCLUSIVE: "red"}[v]
    bar = "═" * 74
    L = []

    def add(text="", *styles):
        L.append((text, styles))

    add(bar, "dim")
    add(f"  relay verify · {session_id} · packet {packet:03d}", "bold")
    add(bar, "dim")
    add(f"  VERDICT: {v}", style, "bold")
    add()
    for line in CAVEAT:
        add(f"  ⚠  {line}", "yellow")
    add()

    # #6's contract first — a malformed report is not a report, so say that before anything else.
    if result["tldr"]["problems"]:
        add("  TL;DR BLOCK — MALFORMED (#6's contract: absence reads as malformed, never as "
            "\"nothing to report\")", "red", "bold")
        for p in result["tldr"]["problems"]:
            add(f"    ✗ {p}", "red")
    else:
        add("  TL;DR block: well-formed (Status / Risk flags / UNVERIFIED / Changed)", "dim")
        add(f"    Status: {result['tldr']['status']}", "dim")
    add()

    # Echoed, never absorbed. Loud when they carry content, quiet when they say 'none'.
    for label, key in (("RISK FLAGS", "risk_flags"), ("UNVERIFIED", "unverified")):
        val = result["tldr"][key]
        if val is None:
            add(f"  {label}: (line missing — see MALFORMED above)", "red", "bold")
        elif is_none_value(val):
            add(f"  {label}: none (the report's own claim — echoed, not confirmed)", "dim")
        else:
            add(f"  {label} — echoed verbatim from the report, NOT assessed here:", "red", "bold")
            add(f"    {val}", "red", "bold")
    add()

    add("  STAGED REALITY", "bold")
    scope = ("\"What changed\" section" if result["claims_scoped"]
             else "whole report (no \"What changed\" section — claims can't be scoped, so "
                  "claimed-not-staged is advisory below)")
    add(f"    claim scope: {scope}", "dim")
    add(f"    claimed and staged:   {len(result['claimed_staged'])}", "dim")
    add(f"    claimed, NOT staged:  {len(result['claimed_missing'])}",
        *(("red",) if result["claimed_missing"] and result["claims_scoped"] else ("dim",)))
    add(f"    staged, not claimed:  {len(result['unclaimed'])} (advisory — reports summarise)", "dim")
    add(f"    index: {len(result['staged'])} file(s) staged and uncommitted", "dim")
    add()

    add("  DECLARED TESTS", "bold")
    if result["rerun"] is None:
        add("    NOT RE-RUN (default — verify stays fast). Pass --rerun to actually run these.", "dim")
        counts = ", ".join(f"{n} {k}" for n, k in result["declared_counts"]) or "(none declared)"
        add(f"    counts the report declares: {counts}", "dim")
        for cmd in result["declared_commands"] or []:
            add(f"    command declared: `{cmd}`", "dim")
        if not result["declared_commands"]:
            add("    commands declared: (none this tool would re-run — pytest-shaped only)", "dim")
    else:
        for r in result["rerun"]:
            got = f"{r['passed']} passed" if r["passed"] is not None else "NO `N passed` — did not run"
            declared = r["declared"] if r["declared"] is not None else "nothing declared"
            add(f"    RE-RAN `{r['cmd']}` → {got} (report declares: {declared})", "dim")
        if not result["rerun"]:
            add("    --rerun given, but the report declared no re-runnable (pytest-shaped) command",
                "red")
    add()

    mismatches = [f for f in result["findings"] if f["level"] == "mismatch"]
    unchecked = [f for f in result["findings"] if f["level"] == "inconclusive"]
    notes = [f for f in result["findings"] if f["level"] == "note"]
    if mismatches:
        add("  MISMATCHES", "red", "bold")
        for f in mismatches:
            add(f"    ✗ {f['text']}", "red")
        add()
    if unchecked:
        add("  CHECKS THAT DID NOT COMPLETE (you asked for them; they did not happen)", "red", "bold")
        for f in unchecked:
            add(f"    ? {f['text']}", "red")
        add()
    if notes:
        add("  NOTES (advisory — not part of the verdict)", "dim")
        for f in notes:
            add(f"    · {f['text']}", "dim")
        add()

    add(bar, "dim")
    if v == COUNTS_MATCH:
        add("  COUNTS-MATCH is the ceiling of what this tool can say. Read the staged diff.",
            "yellow", "bold")
    elif v == MALFORMED:
        add("  MALFORMED: this report does not meet the format's contract. Do not read a missing "
            "UNVERIFIED line as \"nothing to report\".", "red", "bold")
    elif v == INCONCLUSIVE:
        add("  INCONCLUSIVE: a check you asked for did NOT complete. This is NOT a COUNTS-MATCH "
            "with a caveat — nothing was compared.", "red", "bold")
    else:
        add("  MISMATCH: a claim contradicts staged reality. Read the staged diff.", "red", "bold")
    add(bar, "dim")
    return L
