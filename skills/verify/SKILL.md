---
name: verify
description: >-
  Machine-check an executor's report against its staged reality — TL;DR block well-formed, claimed
  files actually staged, declared counts cross-checked — stamping MALFORMED / MISMATCH /
  INCONCLUSIVE / COUNTS-MATCH. Invoke with /relay:verify, or when asked "check that report",
  "did it really do what it says", "verify X's claims before I commit".
arguments: [session_id]
---

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.

Run: `${CLAUDE_PLUGIN_ROOT}/bin/relay verify $session_id`

Add `--packet N` for an older packet, and `--rerun` to also re-run the pytest-shaped commands the
report declares (off by default so verify stays fast).

## READ THIS BEFORE YOU READ A VERDICT

**A `COUNTS-MATCH` must never be read as "the report is true."** This is the whole design
constraint, not a footnote. The field data behind it: across ~15 real executor reports, a
counts-verifier would have caught exactly **one** thing (a lint miss). Every *dangerous* problem
was premise-level — wrong oracle, wrong write-side, suite green in the wrong venv — and every one
of those is **invisible** to re-running the declared commands.

So: this tool checks that the numbers line up. It cannot check that the work was right. **Your
judgement on the staged diff stays the real check** — if you let a COUNTS-MATCH displace it, you
have made relay less safe, not more, because you have swapped the thing that caught the real
problems for the thing that catches lint misses.

That is also why there is no "PASS", no "VERIFIED", and no "clean" in this command's vocabulary.
The verdict words are deliberately chosen so they cannot be over-read at a glance.

## The verdicts

| Verdict | Exit | Means |
|---|---|---|
| `COUNTS-MATCH` | 0 | The checkable numbers agree. **That is all it means.** Still read the diff. |
| `MISMATCH` | 1 | A claim contradicts staged reality — a file claimed but never staged, a "staged" confirmation over an empty index, or (with `--rerun`) a declared test count that doesn't reproduce. |
| `MALFORMED` | 2 | The report violates the REPORT FORMAT's TL;DR contract — most often a missing `UNVERIFIED:` line. Per #6, an absent UNVERIFIED line reads as **malformed, never as "nothing to report."** |
| `INCONCLUSIVE` | 3 | A check you asked for did not complete (e.g. `--rerun` produced no `N passed` count). **Not** a COUNTS-MATCH with a caveat — nothing was compared. |

## What it actually checks

- **The TL;DR block** — `Status` / `Risk flags` / `UNVERIFIED` / `Changed` present, non-empty, in
  order, after a plain one-sentence outcome line. Missing or malformed → `MALFORMED`.
- **Claimed vs staged files** — paths named under the report's "What changed" section, checked
  against `git diff --cached --name-only` in the session's worktree. A claimed file that isn't
  staged is a `MISMATCH` naming it (and it says whether the file was at least *modified*). A
  *staged* file the report didn't name is only a note — reports legitimately summarise.
  If the report has no "What changed" section the scan can't tell "I changed x" from "I read x",
  so it downgrades to advisory instead of accusing. It says so in the output.
- **Staged, not committed** — a report asserting its work is staged over an empty index is a hard
  contradiction. Commits on the branch are advisory only (you legitimately commit earlier packets
  from a reused session).
- **Declared tests** — counts and commands are listed but **NOT re-run** by default, and that
  renders as its own state. Pass `--rerun` to actually run them; only pytest-shaped commands with
  no shell metacharacters are ever executed, argv-only, never through a shell. A report is text an
  executor wrote — this command will not become a way for one to run arbitrary shell in your
  worktree.
- **Risk flags and UNVERIFIED lines** are lifted and **echoed verbatim**, loudly, on every run.
  The verifier surfaces them; it never absorbs, grades, or resolves them. They do not change the
  verdict — that is your call, which is the point.

Every run appends a `report_verify` event to the ledger (verdict, session, packet).

## How to use it in a review

Run it *before* you read the diff, not instead of it. It is a cheap pre-filter that tells you
where to look harder:

- `MALFORMED` → send it back; you can't trust a TL;DR you can't read, and a missing UNVERIFIED
  line is exactly where the honest gaps were going to be.
- `MISMATCH` → read the named file(s) first. This is the one class the tool is genuinely good at.
- `INCONCLUSIVE` → the check didn't happen. Decide whether to run it properly or to verify by hand.
- `COUNTS-MATCH` → nothing is known to be wrong. Now do the actual review: read the staged diff,
  check the premise (was the executor even solving the right problem?), and check the risk flags
  it echoed back at you.

**Do not wire this into an auto-commit path.** #16 phase 2 is a separate, user-approved step; a
zero exit code from this command is not, on its own, permission to commit anything.
