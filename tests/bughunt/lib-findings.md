# Bug-hunt findings — area `lib`

Scope: `lib/report_verify.py`, `lib/lead_guard.py`, `lib/board_render.py`, `lib/diff_render.py`.
Branch `wt/bh-lib` off `main` b659a73. Every finding below was reproduced live before it was
written down; every one has a `@pytest.mark.xfail(strict=True)` repro test naming it, except the
AMBIGUOUS entries (per the shared protocol: do not guess).

**No source file was edited.** The proposed fixes are descriptions, not applied changes.

Coverage, full unit suite (`--ignore-glob='tests/test_e2e_*'`):

| file | before | after |
|---|---|---|
| `lib/report_verify.py` | 99% | 100% |
| `lib/lead_guard.py` | 88% | 99% |
| `lib/board_render.py` | 96% | 100% |
| `lib/diff_render.py` | 94% | 100% |

New tests (358 passed, 17 xfailed — the xfails are the ten bugs below, some parametrised):

- `tests/test_lib_report_verify_bughunt.py`
- `tests/test_lib_render_bughunt.py` (both renderers)
- `tests/test_lib_lead_guard_bughunt.py` — part 1, state on disk
- `tests/test_lib_lead_guard_policy_bughunt.py` — part 2, the pure policy layer
- `tests/test_lib_lead_guard_locks_bughunt.py` — part 3, locks / escalation / peer addressing

Tally: **10 BUG**, **4 AMBIGUOUS**, **0 SUSPECT-TEST**.

---

## BUG-lib-1 — a non-ASCII filename becomes a false MISMATCH accusation

**Severity:** wrong-result (false accusation — the class this module names as its expensive error)

**Contract** — `lib/report_verify.py:162-165` (`plausible_claims` docstring):
> "accusing an executor of not staging `bulleted/ornamented` would be a false MISMATCH — the
> expensive error here, since a false accusation costs more trust than a missed catch."

And `skills/verify/SKILL.md`: "A claimed file that isn't staged is a `MISMATCH` naming it."

**Actual** — `lib/report_verify.py:154-156`, `_CLAIM_RE`'s character class is ASCII-only
(`[A-Za-z0-9_.-]`), so a path containing any non-ASCII character is truncated at that character
and the *prefix* becomes the claim:

```
report:  "- tests/tëst_data.py:1 — renamed the fixture."
staged:  ["tests/tëst_data.py"]
claims:  ['tests/t']                       ← survives plausible_claims ("tests" is a repo entry)
verdict: MISMATCH
  ✗ tests/t — claimed under "What changed" but not staged, and not modified in the worktree
  · tests/tëst_data.py — staged but not named in the report
```

Both findings are wrong, and they are wrong in the two directions at once: the tool accuses a path
that does not exist AND reports the real, correctly-claimed file as unclaimed.

**Repro test:** `TestClaimExtraction::test_a_non_ascii_filename_is_not_falsely_accused`

**Proposed fix:** make the class unicode-aware, and refuse a candidate that was cut mid-word:

```diff
-    r'(?<![\w/.-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]{0,5})'
-    r'(?::\d+(?:-\d+)?)?')
+    r'(?<![\w/.-])((?:[^\s/:,;()\[\]`"\']+/)+[^\s/:,;()\[\]`"\']+|'
+    r'[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]{0,5})'
+    r'(?::\d+(?:-\d+)?)?(?![\w-])')
```
The trailing `(?![\w-])` alone kills the false accusation (the truncated `tests/t` is dropped
because `ë` is a word character); widening the class is what makes the real path checkable.
`lib/diff_render.py:34-37` has the same ASCII class but is harmless there — it is intersected against
the staged set, and its docstring says over/under-matching is free.

---

## BUG-lib-2 — a claim written on the "What changed" bullet itself is never checked

**Severity:** wrong-result (a real claim silently goes unverified)

**Contract** — `lib/report_verify.py:194-201` (`what_changed_section` docstring):
> "The report's "What changed" section body … Runs from the **heading/bullet** naming it to the
> next heading."

A bullet is an accepted opener (and `_demark`, `lib/report_verify.py:79-83`, exists so "field
detection survives an executor that bulleted its TL;DR").

**Actual** — `lib/report_verify.py:203-207`: `start = i + 1`. The body begins on the line AFTER the
opener, so everything the opener line itself says is discarded. For the very common one-line form:

```
- What changed: src/app.py:2 — appended a line.
```

`src/app.py` is never added to `claims`, so a report that names a file it never staged passes the
claimed-vs-staged check on that file. With a heading opener (`## What changed`) the same report is
checked correctly, so the tool's strictness depends on the report's markdown style.

**Repro test:** `TestClaimExtraction::test_a_claim_on_the_what_changed_bullet_itself_is_checked`

**Proposed fix:** when the opener line carries text after the section name, include that remainder
in the body:

```python
m = _WHAT_CHANGED_RE.match(_demark(raw))
if m:
    rest = _demark(raw)[m.end():].lstrip(" :—-")
    body_prefix = [rest] if rest else []
    start = i + 1
```

---

## BUG-lib-3 — a bulleted "What changed" section has no terminator and swallows the report

**Severity:** wrong-result (false MISMATCH accusations — same expensive-error class as BUG-lib-1)

**Contract** — the same two docstrings as BUG-lib-1 and BUG-lib-2: the scoped path is the one
"trustworthy enough to accuse on" (`lib/report_verify.py:219-223`), and the unscoped path exists
because "a mention there may be a file merely read, so the caller must downgrade".

**Actual** — `_ends_section` (`lib/report_verify.py:190-191`) only recognises a markdown heading or
a fully-bold pseudo-heading. A report whose sections are bullets has neither, so the "What changed"
section runs to the end of the report and every path in it is treated as an accusable claim:

```
- What changed: src/app.py:2 — appended a line.
- What I verified: ran the suite over tests/test_app.py, 5 passed.
- I also read lib/other.py to understand the format.
→ MISMATCH
  ✗ tests/test_app.py — claimed under "What changed" but not staged …
  ✗ lib/other.py     — claimed under "What changed" but not staged …
```

Both files are explicitly described as *read*, not changed. Combined with BUG-lib-2 the same report
loses its one true claim and gains two false ones.

**Repro test:**
`TestClaimExtraction::test_a_bulleted_section_does_not_swallow_the_rest_of_the_report`

**Proposed fix:** when the section was opened by a bullet, end it at the next top-level bullet:

```python
def _ends_section(raw, bulleted=False):
    if _HEADING_RE.match(raw) or _BOLD_HEADING_RE.match(raw):
        return True
    return bulleted and re.match(r"^\s{0,3}[-*]\s", raw) is not None
```
Conservative alternative, in the module's own spirit: when the opener was a bullet, mark the claims
**unscoped** so they downgrade to advisory instead of accusing.

---

## BUG-lib-4 — a diff line that looks like a file header is dropped from the review page

**Severity:** wrong-result (the page the lead commits from silently omits a change)

**Contract** — `README.md:235-238`:
> "`relay diff <sid>` … renders an executor's `git diff --staged` to a self-contained, offline HTML
> page … so you review diffs in one click"

and `lib/diff_render.py:50-60` (`parse_unified_diff`): it returns per-file `additions`/`deletions`,
and the module docstring says "the page header numbers come from this module's own parse either
way, so they're consistent regardless of which renderer drew the body."

**Actual** — `lib/diff_render.py:90-97` matches `--- ` / `+++ ` as file headers **anywhere**,
including inside a hunk body. In a real unified diff those two lines only ever appear in the file
header, before the first `@@`; inside a hunk they are content (`-` + a line starting `-- `). So
deleting an SQL comment, a `-- ` signature separator, or any `-- `-prefixed line corrupts the file:

```
$ git diff --cached
--- a/q.sql
+++ b/q.sql
@@ -1,3 +1,2 @@
 a
--- sql comment            ← a DELETED line
 b

parse_unified_diff → {'old_path': 'sql comment', 'new_path': 'q.sql',
                      'deletions': 0, hunks: [context 'a', context 'b']}
```

Consequences on the rendered page: the deleted line is **gone**, the header says "+0 -0" over a
diff that deletes a line, and the file card is titled `sql comment → q.sql` as if it were a rename.
On the **primary** (diff2html) path the body is drawn correctly by the vendored library while the
header stats still come from this parse — so the page contradicts itself. The `+++ ` mirror
(adding a line starting `++ `) loses an addition the same way.

**Repro tests:** `TestUnifiedDiffFidelity::test_a_deleted_line_that_looks_like_a_file_header_is_still_rendered`,
`…::test_an_added_line_that_looks_like_a_file_header_is_still_counted`

**Proposed fix** (`lib/diff_render.py:90-97`) — header lines are only headers before the first hunk:

```diff
-        elif raw.startswith("--- "):
+        elif hunk is None and raw.startswith("--- "):
             p = raw[4:]
             if p not in ("/dev/null",):
                 cur["old_path"] = p[2:] if p.startswith(("a/", "b/")) else p
-        elif raw.startswith("+++ "):
+        elif hunk is None and raw.startswith("+++ "):
```
`new file mode` / `deleted file mode` / `Binary files …` deserve the same guard for consistency,
though those three are not reachable from a hunk body (git prefixes content with `+`/`-`/space).

---

## BUG-lib-5 — two board chips are interpolated without escaping

**Severity:** cosmetic today, latent injection (not reachable from current producers)

**Contract** — `lib/board_render.py:11` ("Pure: render(data) -> html") and `_e` at
`lib/board_render.py:17-18`, which every other value in the module goes through.

**Actual** — `lib/board_render.py:246` and `:254`:

```python
out.append(f'<span class="chip">cache hit<b>{ex["hit_rate"]}%</b></span>')
out.append(f'<span class="chip flag">\U0001F4E5 {ex["queued"]} queued</span>')
```

Both reach the page raw. Verified: `hit_rate="<script>alert(1)</script>"` renders verbatim.

**Honest reachability:** `bin/relay:2478-2479` supplies `round(...)` and `len(read_queue(sid))`, so
both are ints today and this is NOT currently exploitable. It is an escaping hole that only stays
closed by accident of the producer — every other field in the same function is defended.

**Repro test:** `TestBoardEscaping::test_every_chip_value_is_escaped`

**Proposed fix:** wrap both in `_e(...)`, as every neighbouring line already does.

---

## BUG-lib-6 — one bad lead colour takes the whole board page down

**Severity:** crash

**Contract** — `_lead_dot` (`lib/board_render.py:350-355`) already carries a fallback for an
unusable colour (`background:var(--dim)`), i.e. degrading is the intended behaviour. The rule for
the surface it feeds is stated for the sibling reader, `lib/lead_guard.py:625-629` (`list_leads`):
> "this is the always-visible LEADS surface, so a single bad marker must never blank the whole
> list."

**Actual** — the guard checks the LENGTH but not the contents:

```python
if color and len(color) == 3:
    return f'…rgb({int(color[0])},{int(color[1])},{int(color[2])})…'
```

`{"color": "red"}` has length 3, so `int("r")` raises `ValueError` and `board_render.render()`
aborts — no page at all, for every lead and every executor, from one marker field.
`lead_guard.write_marker` writes the colour with a plain `write_text` (no tmp+rename), so a
hand-edited or half-written marker is a real way to reach this.

**Repro test:** `TestBoardShape::test_a_colour_that_is_not_three_numbers_falls_back_to_the_dim_dot`
(parametrised over `"red"`, `["a","b","c"]`, `[None,None,None]`)

**Proposed fix:**

```python
def _lead_dot(m):
    color = m.get("color")
    try:
        r, g, b = (int(v) for v in color)
        return f'<span class="cdot" style="background:rgb({r},{g},{b})"></span>'
    except Exception:
        return '<span class="cdot" style="background:var(--dim)"></span>'
```

---

## BUG-lib-7 — the `mcp-unparsable` lint rule can never fire

**Severity:** wrong-message (a promised warning that is unreachable; a misleading one fires instead)

**Contract** — the rule's own message, `lib/lead_guard.py:1937-1939`:
> `"MCP: line present but its value isn't none/inherit/a,b: '<value>'"`

**Actual** — its guard is `if mcp_line and mcp_spec is None`, but `normalize_mcp_spec`
(`lib/lead_guard.py:2081-2098`) is **total**: any unrecognised string falls through to
`normalize_mcp_spec(s.split(","))`, which returns a list of names. `packet_mcp_spec` therefore
returns `None` only when there is no `MCP:` line at all — in which case `mcp_line` is `None` too.
The branch is dead.

Observable effect: `MCP: whatever you think best` is silently read as a **server name**. With
`known_servers` supplied the lead sees the misleading `mcp-unknown-server` ("not configured in
~/.claude.json / <worktree>/.mcp.json (spawn will refuse)"); with `known_servers=None`
(`bin/relay:591`, `:605` — whenever `cwd` is falsy) the lead sees nothing at all, and the spawn
later refuses for a reason the lint never mentioned.

**Repro test:** `TestPacketLint::test_an_unparsable_mcp_value_is_reported_as_unparsable`

**Proposed fix** — either is defensible, hence both are listed:
1. give the rule something to detect, e.g. warn when a declared name is not a plausible server
   identifier (`not re.fullmatch(r"[\w.@-]+", n)` for any `n` in the list); or
2. delete the branch and let `mcp-unknown-server` own the case — but then widen that rule to fire
   with `known_servers=None` too, so the silent path closes.

---

## BUG-lib-8 — `is_lead` reports True for a marker it cannot read

**Severity:** wrong-result (a session's own armed-state becomes self-contradictory)

**Contract** — `lib/lead_guard.py:437-445` (`is_lead` docstring):
> "The sole 'is this a lead session' test. **Marker absent (or any error) → not lead** → the hooks
> fast-exit-allow, which is the entire zero-impact path for non-lead/executor sessions."

**Actual** — `lib/lead_guard.py:446-451`:

```python
if not marker_path(state_root, session_id).exists():
    return False
return not is_tombstoned(read_marker(state_root, session_id))
```

`read_marker` swallows every error into `{}` (`lib/lead_guard.py:499-505`), so `is_lead` never sees
"any error": a marker that exists but is truncated, empty, `null`, or a JSON array reads as
**armed**. Meanwhile every other reader of the same file — `autonomous_state`, `wake_hook_state`,
`touch_lead`, `update_marker`, `tombstone_lead`, `list_leads` — treats it as absent. The session is
then gated and wake-polled off a marker that carries no cwd, no project, no colour, no timeout
stamp, and cannot be heartbeated or tombstoned.

Reachable because `write_marker` (`lib/lead_guard.py:454-497`) is a plain `write_text`, not
tmp+rename: a crash, a full disk, or a kill mid-write leaves exactly this file.

**Repro test:** `TestMarkerState::test_an_unreadable_marker_is_not_a_lead` (parametrised over
`"{ truncated"`, `""`, `"[1, 2]"`, `"null"`)

**Proposed fix:**

```python
def is_lead(state_root, session_id):
    try:
        if not marker_path(state_root, session_id).exists():
            return False
        m = read_marker(state_root, session_id)
        if not isinstance(m, dict) or not m:
            return False          # exists but unreadable → "any error" → not lead
        return not is_tombstoned(m)
    except Exception:
        return False
```
Worth pairing with an atomic marker write (`write_text` to `marker.json.tmp`, then `os.replace`),
which removes the whole torn-file class rather than tolerating it.

---

## BUG-lib-9 — a packet mentioning `~someuser/…` crashes `relay spawn` and `relay lint`

**Severity:** crash

**Contract** — `lib/lead_guard.py:1660-1663` (`packet_reading_bytes`):
> "Total size of the files a packet refers to (paths that exist — relative to `cwd` or
> absolute/home). Pure-mechanical … **Missing paths count nothing.**"

The per-candidate `try/except Exception: continue` at `lib/lead_guard.py:1671-1681` is there
precisely to absorb a candidate that cannot be resolved.

**Actual** — `lib/lead_guard.py:1667` builds the candidate list *outside* that try:

```python
cands = [Path(raw).expanduser()]
```

`Path("~nosuchuser42/x.py").expanduser()` raises `RuntimeError: Can't determine home directory for
'nosuchuser42'` (CPython `pathlib`), which escapes `packet_reading_bytes`. Neither caller catches
it: `bin/relay:1004-1009` catches only `ValueError`, and `cmd_lint` (`bin/relay:605`) has no
guard at all. A packet that merely *mentions* another user's home path — `~ops/deploy.sh`,
a path copied from another machine — kills the spawn with a traceback.

**Repro test:** `TestContextWindow::test_a_path_that_cannot_be_expanded_is_skipped_not_fatal`

**Proposed fix:**

```diff
-        cands = [Path(raw).expanduser()]
-        if cwd and not raw.startswith(("/", "~")):
-            cands.insert(0, Path(cwd) / raw)
+        cands = []
+        if cwd and not raw.startswith(("/", "~")):
+            cands.append(Path(cwd) / raw)
+        try:
+            cands.append(Path(raw).expanduser())
+        except Exception:
+            pass
```

---

## BUG-lib-10 — ordinary technical prose becomes claimed files (two live sub-cases)

**Severity:** wrong-result (false accusation — the class this module names as its expensive error)

Both sub-cases were produced by real `relay verify` runs on 2026-09-05, on real executor reports.
They share one filter (`plausible_claims`) but have two distinct root causes, so both are listed.

**Contract** — `plausible_claims` (`lib/report_verify.py:160-171`): it filters candidates down to
> "ones that could really be repo files … accusing an executor of not staging
> `bulleted/ornamented` would be a false MISMATCH — **the expensive error here**, since a false
> accusation costs more trust than a missed catch."

and the `_CLAIM_RE` comment (`lib/report_verify.py:151-152`), which states the defence directly:
> "The bare-filename branch caps the extension at 6 chars **so a dotted Python identifier**
> (`diff_render.parse_report_mentions`) **doesn't read as a file**."

### (a) a dotted Python identifier with a SHORT attribute name

**Actual** — the 6-char cap only excludes identifiers whose attribute name is longer than six
characters. `.model`, `.get` and the `.g` of "e.g." are all inside the cap, so the very class the
comment says is excluded matches. Live output on a report whose What changed bullet read
`via lead_guard.model_exceeds_ceiling); returns (ok, detail, info_lines)` and
`e.g. haiku 200k`, `r.get("model")`, `s.get("model")`:

```
✗ lead_guard.model — claimed under "What changed" but not staged, and not modified in the worktree
✗ e.g             — claimed …
✗ r.get           — claimed …
✗ s.get           — claimed …
```

Note `lead_guard.model`: the regex matched a PREFIX of `lead_guard.model_exceeds_ceiling`, exactly
the truncation mechanic behind BUG-lib-1, reached here by a different route.

### (b) a bare filename that is not a repo file at all

**Actual** — `lib/report_verify.py:175`: a candidate is kept when
`p in staged or _HAS_EXTENSION_RE.search(p) or (first in repo_entries and "/" in p)`. For a bare
name (no `/`) the extension test alone decides, and NOTHING checks that the name could be a repo
file. `tier_windows.json` lives under `~/.relay-tasks`, has never been in the repo, and was still
accused. The `repo_entries` set the caller already gathers (`bin/relay:3750`) makes this
decidable for free: a top-level file IS in that set.

**Repro tests:** `TestLiveFalsePositives::test_dotted_identifiers_in_prose_are_not_claims`,
`TestLiveFalsePositives::test_a_bare_filename_that_is_not_in_the_repo_is_not_claimed`

**Proposed fix** — one change to `plausible_claims` kills all five false claims at once, because a
bare name must then be a real top-level repo entry:

```diff
     kept = []
     for p in paths:
-        first = p.split("/")[0]
-        if p in staged or _HAS_EXTENSION_RE.search(p) or (first in repo_entries and "/" in p):
-            kept.append(p)
+        if p in staged:                 # confirmed, not accused
+            kept.append(p)
+        elif "/" in p:
+            if _HAS_EXTENSION_RE.search(p) or p.split("/")[0] in repo_entries:
+                kept.append(p)
+        elif p in repo_entries:         # a BARE filename must be a real top-level repo entry
+            kept.append(p)
     return kept
```

Verified against every case in the existing `TestClaimFalsePositives` before proposing it:
`["bin/relay", "lib/x.py"]` with no `repo_entries` still yields `["lib/x.py"]` (the documented
weaker, non-accusing direction), `bin/relay` stays checkable with `repo_entries={"bin","lib"}`, a
staged path is still never filtered out, `bulleted/ornamented` still drops, and `README.md` — a
top-level entry — stays checkable. No special-casing of `e.g`/`i.e` is needed; they fall out.

Residual not covered by that one change, listed so it is not mistaken for fixed: a dotted attribute
on a path-shaped prefix (`lib/lead_guard.model_exceeds_ceiling`) still survives on the
`first in repo_entries` rule. A second, narrower guard would be to refuse a candidate immediately
followed by `(` — `(?![\w.]*\()` — since that is a call, not a path. It is listed as secondary
because it is a heuristic, and the bare-name rule above is not.

---

## AMBIGUOUS-lib-4 — paths named in a NEGATED sentence are accused

**Severity if a bug:** wrong-result (false accusation). **Classification: ambiguous — the fix may
not belong to this tool at all.**

**Observed live**, 2026-09-05, on a report whose What changed section opened:

> New files only — `bin/relay`, `lib/*.py`, `hooks/*.py`, `skills/`, `README.md` and every existing
> test file are **untouched**, per the shared boundaries.

`relay verify` reported `bin/relay` and `README.md` as claimed-but-not-staged. Reproduced exactly.

**Why this is not filed as a bug.** The two accused strings are real repo paths in a scoped
"What changed" section — by shape they are indistinguishable from genuine claims. Only the English
word *untouched* separates them, and reading that requires negation detection, which:

- is fragile in both directions (`untouched`, `not modified`, `left alone`, `nothing under hooks/
  changed`, and the inverse trap: `lib/a.py — removed the untouched flag`), and
- contradicts this module's own stated design rule (module docstring, `lib/report_verify.py:23-24`):
  "prefers the CHEAP DETERMINISTIC check and says so in its own output rather than guessing
  cleverly". A negation heuristic that suppresses a REAL claim is strictly worse than this false
  positive — it converts a noisy accusation into a silent miss, which is the failure mode the whole
  module exists to prevent.

A second tool-side option was considered and rejected: `agents/executor.md` asks for "file:line for
each substantive change", and the accused paths carry no `:line` while the real claim did. Requiring
`:line` would fix this case, but `_CLAIM_RE` makes `:N` optional on purpose and plenty of honest
reports name a new file without a line number — that trade buys a false-positive fix with a
false-negative class, the wrong direction for this module.

**So the likely correct fix is documentation, not code:** one line in `agents/executor.md`'s REPORT
FORMAT under "What changed", e.g. *"name only files you actually changed here — say what you did
NOT touch outside this section, or the verifier will read it as a claim."* That is deterministic,
costs nothing, and removes the input rather than guessing about it. It is out of this hunt's
boundaries to apply (`agents/` and the docs are not this packet's to edit), and choosing between
"tool bug" and "doc gap" is a judgement the lead owns — hence AMBIGUOUS.

**Test:** `TestLiveFalsePositives::test_globs_and_bare_directories_never_become_claims` pins only
the decidable half — that `lib/*.py`, `hooks/*.py` and `skills/` never become claims (a glob and a
bare directory are not repo FILES), and that the section's one genuine claim is still matched. The
two bare paths are deliberately left unasserted. That test is a regression guard, not a bug repro:
it passes today, and its job is to stop a future negation/prose fix from quietly breaking the part
that already works.

---

## AMBIGUOUS-lib-1 — duplicated TL;DR fields are silently first-wins

`parse_tldr` (`lib/report_verify.py:114-122`) records only the first occurrence of each field
(`if key not in seen`) and never flags the duplicate. The REPORT FORMAT (`agents/executor.md`,
"REQUIRED TL;DR block") says the four fields "must be present verbatim and in this order" but does
not say what a *second* copy means, so I cannot tell whether silence here is intended tolerance or
an oversight.

Why it matters: the realistic shape is an executor that pastes the format's template above its real
block, so the tool reads `Status: clean / clean-with-caveats / blocked / partial` and
`Risk flags: <…>` as the values. Checked live — that fails **safe** for the auto-commit gate
(condition 2 compares the whole string to `"clean"`, so it stops with `status-not-clean`) and the
template text is echoed loudly rather than absorbed. The verdict, however, is still `COUNTS-MATCH`,
and a later contradicting `Status: blocked` is invisible.

Pinned by `TestTldrContractEdges::test_a_template_echo_above_the_real_block_cannot_falsely_clear`
(asserts only the safe half). A decision is needed: flag a duplicate field as a `problems` entry,
or state in the format that duplicates are permitted and first-wins.

---

## AMBIGUOUS-lib-2 — an ill-typed config value silently disables the routing gate

`README.md:629`: "missing keys fall back to defaults; unknown keys are ignored". Nothing is said
about a **known** key with the wrong type. `load_config` (`lib/lead_guard.py:243-260`) copies such a
value through verbatim; `exceeds_gate` then raises `TypeError: '>=' not supported between instances
of 'int' and 'str'`, and `hooks/pretool_route_guard.py`'s documented hard fail-open catches it and
exits 0. Net effect of `{"edit_line_threshold": "40"}` in `lead/config.json`: the routing gate is
**off**, permanently and silently.

Both readings are defensible — the hook is behaving exactly as its contract demands, and the README
never promised type coercion — so this is filed as ambiguous rather than as a bug. Pinned by
`TestConfig::test_an_ill_typed_threshold_is_carried_through_verbatim`.

Suggested resolution: in `load_config`, keep a user value only when
`isinstance(user[k], type(LEAD_DEFAULTS[k]))` (special-casing `None` defaults), and ledger/print a
one-line note when a key is rejected. That preserves fail-open while making it visible.

---

## AMBIGUOUS-lib-3 — condition 4's sign-off list is narrower than the SKILL text

`skills/verify/SKILL.md` states condition 4 as "nothing sign-off-gated is touched (core logic,
**ledgers**, parity/golden tests, migrations, deploys — and for relay's own repo, `hooks/`,
`lib/lead_guard.py`, ledger formats)". `SIGNOFF_PATH_MARKERS` (`lib/report_verify.py:424-432`)
covers `hooks/`, `lib/lead_guard.py`, `migration`, `parity`, `golden`, `schema`, `deploy` — there is
no `ledger` path marker and no notion of "core logic". Ledgers are instead covered by a diff-shape
proxy (`_LEDGER_EDIT_RE`, `lib/report_verify.py:435`) that only fires when the staged diff adds or
removes an `append_ledger(` call.

So a staged change to, say, `src/ledger_writer.py` that does not touch an `append_ledger(` call
clears condition 4. Whether "ledgers" in the SKILL means the *format* (deliberately proxied) or the
*code path* (not covered) is not decidable from the docs, and the code comment argues explicitly for
the proxy — so this is recorded, not asserted, and no test was written for it.

---

## OBSERVATIONS (no test, no fix asked for)

- `report_verify.VERDICT_PRECEDENCE` (`lib/report_verify.py:52`) and `_STAGED_CLAIM_RE`
  (`lib/report_verify.py:284-285`) are defined and never used; `verify()` re-implements the
  precedence inline. Harmless, but the constant can drift from the code it documents.
- `new_reports_for`'s closed/superseded guard (`lib/lead_guard.py:1096-1097`) is unreachable:
  `executor_reports` (`lib/lead_guard.py:778`) already filters those statuses out. Defence in
  depth, not a defect — noted only because it is the one lead_guard line the new tests cannot
  reach.
- A report claiming a file that is **staged and also further modified** produces no finding. That
  matches `skills/verify/SKILL.md` (only "claimed but isn't staged" is a MISMATCH), so it is
  recorded as correct-by-contract, with a test pinning it
  (`TestClaimExtraction::test_a_file_both_staged_and_further_modified_is_not_a_mismatch`).

## SUSPECT TESTS

None found. `tests/test_report_verify.py`, `tests/test_diff_render.py`, `tests/test_board.py` and
`tests/test_lead_guard.py` were read for overlap and for assertions that contradict the docs above;
nothing in them asserts behaviour I believe to be wrong. This is a review of their assertions in
the areas this hunt touched, not a line-by-line audit of all 527 of them.
