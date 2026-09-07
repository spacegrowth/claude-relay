---
name: review
description: >-
  Review an executor's report through a same-model fork instead of reading the staged diff
  yourself: the fork verifies, reads the full diff against the packet, runs the acceptance
  commands, and returns numbered findings plus a recommendation — under 40 lines, in the fork's
  own disposable context, never yours. Invoke with /relay:review <session>, or when asked "review
  X's changes", "review that report", "check this executor's work" — this is now the DEFAULT way
  a lead reviews a reported packet, not an alternative to it.
arguments: [session_id]
---

**Why this exists.** Reading a staged diff inline costs the lead's OWN context — one real 15-file
review cost ~40k tokens of a 200k window, in 25 minutes, before anything was even committed
(`_staging/lead-context-burn-note.md`). A fork spawned via `Agent(subagent_type: "fork")` inherits
this session's full context (project decisions, review discipline) but does its reading in ITS
OWN, disposable context — same judgement, ~3k tokens back in the lead instead of ~40k. This applies
to every project relay leads, with **no special case for relay's own repo**.

## What to run

Resolve three things first — the session's worktree, its current packet number, and that packet's
file paths — via `${CLAUDE_PLUGIN_ROOT}/bin/relay list --json` or `relay check $session_id --json`
(both name `worktree` and `current_packet`; the packet/report files live at
`~/.relay-tasks/$session_id/packets/<NNN>-packet.md` / `<NNN>-report.md`). Then launch exactly
ONE fork with this FIXED prompt — the same shape every time, so every review reads the same way
regardless of who's leading:

```
You are reviewing executor session $session_id's packet <NNN> on the lead's behalf. Do the
following IN ORDER, then return ONLY the output block at the end — nothing else, under 40 lines
total, no preamble, no restating the task, no closing remarks.

1. Run `${CLAUDE_PLUGIN_ROOT}/bin/relay verify $session_id` and note its verdict line verbatim.
2. Read `~/.relay-tasks/$session_id/packets/<NNN>-report.md` and note its TL;DR block (Status /
   Risk flags / UNVERIFIED / Changed) verbatim.
3. Read `~/.relay-tasks/$session_id/packets/<NNN>-packet.md` for the packet's goal and acceptance
   criteria. Read the FULL staged diff in the executor's worktree (`git -C <worktree> diff
   --cached`) and check it against THAT — the packet's actual ask, not just the report's own
   description of itself.
4. Run the packet's declared acceptance/test commands yourself, in the worktree, and report the
   counts you actually saw — do not trust the report's stated counts without re-running them.
5. Return exactly this shape:

Verify: <the verdict line>
TL;DR: <the verbatim TL;DR block>
Findings:
1. file:line — blocker|should-fix|note — one sentence.
2. file:line — blocker|should-fix|note — one sentence.
(or, if none: "No findings.")
Recommendation: commit | fix-list | send back — one sentence why.
```

## After the fork returns

**Read the findings, not the diff.** This does not relax "never skim the report instead of the
diff" — the fork did that read FOR you; skipping straight to the report's own words instead of the
fork's findings is still the exact mistake that rule exists to catch. The fork reads the whole
diff every time — that applies to every project relay leads, with no special case for relay's own
repo. Open the hunks inline yourself only when a finding names a sign-off-gated path for this repo
(core logic, ledgers, parity/golden tests, migrations, deploys — and for relay's own repo
specifically, `hooks/`, `lib/lead_guard.py`, ledger formats) AND the change there is more than a
guard clause or a rename — a second look at those hunks, never a blanket re-read of the diff.

**Then, if you're clearing an autonomous auto-commit (#16 phase 2):** save the fork's returned
findings block to a file and pass it as the condition-5 attestation artifact, not a bare flag:

```
${CLAUDE_PLUGIN_ROOT}/bin/relay verify $session_id --for-autocommit --in-plan --diff-reviewed --findings <path-you-saved-the-findings-to>
```

`verify` copies that file to the session's `packets/<NNN>-review.md`, ledgers `report_reviewed`
with its path and finding count, and treats it as condition 5 — a durable record of what the fork
actually found, not just a boolean saying "reviewed: yes". (Bare `--diff-reviewed` without
`--findings` still works for the rarer inline-read path above; the ledger then just says `inline`.)

In manual (non-autonomous) mode there's no gate to clear — read the findings, decide, then commit
or send a fix-list packet exactly as you would have after reading the diff yourself.
