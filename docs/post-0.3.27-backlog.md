# Post-0.3.27 backlog — findings, open decisions, and deliberate non-fixes

Written 2026-07-19, on branch `wake-push` at `067c50e` (0.3.27). Nothing here is fixed yet, and
nothing here should be rushed. This exists so the reasoning survives the session that produced it.

`main` is untouched at `4a8f4cc` (0.3.25). `wake-push` is pushed to `origin` as a non-default branch,
so the cloud install route still serves 0.3.25 to anyone installing from GitHub. The user is running
0.3.27 locally via a directory marketplace, trialling it for a few days before deciding on a merge.

---

## 0. What is already verified (so nobody re-litigates it)

The §9 push mechanism is not speculative any more. Observed live, in real work, not just in tests:

| Behaviour | Evidence |
|---|---|
| Push fires and delivers when the lead is unaware | `exec-race` — forced race, `escalation.json` = `sent`, message landed |
| Push wins naturally in real work | `exec-suite` packets 4 and 5 → `sent` |
| Lead's own check wins and push correctly stands down | `exec-check`, `exec-push` → `escalation_resolved` |
| Cross-backend nudge fixed | `TERM_PROGRAM=Apple_Terminal` repro: exit 1 before, exit 0 after |
| `_relaunch` re-arms the push hook | `exec-builder`'s tab vanished, resumed, hook intact with correct `argv[1]` |
| Ledger stops lying about delivery | `failed` status + `escalation_push_failed` event |
| Probe fallback works for pre-0.3.27 markers | every nudge to this session used it (`backend` absent) |

| Natural lead handoff on 0.3.27, incident-free | `alpha_service` `lead_handoff` 2026-07-21T08:35:49 → successor `lead_started` +15s, pre-armed, gate live from turn 1, predecessor stepped down clean (Fable; ledger-confirmed) |

Still unobserved: `owner-missing` (lead dies mid-flight), and a lead **exit→resume** cycle on
0.3.27. (The handoff half of the merge bar below is now satisfied — see the last row above. The
§10 drift incident was a scope-discipline gap, NOT a handoff-plumbing failure; the plumbing worked.)

**Merge-readiness bar (suggested).** Merge `wake-push` → `main` once the two unobserved paths above
have been hit *naturally* — a lead exit→resume on 0.3.27, and at least one executor death or handoff
— without incident. Everything else in §0 is already verified live. Until then the branch stays a
few-days trial via the local directory marketplace; `main` and the cloud install route keep serving
0.3.25. (The §1–§3 bugs below are pre-existing on `main` too, so they are not merge blockers — they
are the next work *after* merge, or fixed on the branch first if the trial surfaces them.)

---

## 0b. Canonical task index (the numbered list the rest of this doc references)

The `#N` pointers throughout this doc refer to these. This table is the durable copy — the live
harness task tracker that generated it dies with the authoring session, so a fresh implementing
session should treat THIS as authoritative (per Fable's punchlist item 1). Kind: **BUG** =
something broken today; **CAP** = capability/enhancement; **DEC** = blocked on the user's decision;
**DOC** = doctrine/skill text, no code.

| # | Kind | Task | Section |
|---|---|---|---|
| 1 | BUG | Stamp `backend` at the 3 `write_marker` sites that miss it ✅ LANDED v0.3.30 | §1 |
| 2 | BUG | Make tab-label assertion identity-aware, not label-aware (repro first) ✅ LANDED v0.3.30 | §2 |
| 3 | BUG | Lead status in `relay list` (live/unreachable/ghost) + own transcript-MB column ✅ LANDED v0.3.31 | §3, §9 |
| 4 | CAP | Retitle predecessor tab `[ex-Lead]` on step-down — dissolves the suffix question — ✅ LANDED v0.3.32 ([ex-Lead]/[closed] retitles) | §4 |
| 5 | — | *Superseded by #17* — the "woken twice" investigation; §5b confirmed the bug | §5 |
| 6 | CAP | Required TL;DR block in report format (UNVERIFIED list mandatory) ✅ LANDED v0.3.29 | §6a, §9 |
| 7 | CAP | Plugin-side verifier — with the "counts-match ≠ true" caveat ✅ LANDED v0.3.32 (`relay verify`, INCONCLUSIVE 4th verdict) | §6b, §9 |
| 8 | DEC | Approved-plan autopilot `relay plan approve` (subset of #16) — *superseded by #16 phase 1* | §6c |
| 9 | CAP | `relay land` deploy pipeline — ❌ DROPPED by the user 2026-07-22 (out of scope; commit-push-merge + restart-app skills already cover it) | §6d |
| 10 | CAP | Executor context/heaviness awareness (price/gate/escape e1–e2) ✅ LANDED v0.3.31 | §6e |
| 11 | CAP | `relay retire` + `successor-seed.md` (the sleeper) ✅ LANDED v0.3.31 | §6e-e3 |
| 12 | DOC | "Treat this packet cold" GATES line ✅ LANDED v0.3.30 | §6e-e4 |
| 13 | CAP | `relay send` Preconditions nag ✅ LANDED v0.3.31 | §7-h1 |
| 14 | DOC | STOP-and-report GATES paragraph (broaden to ALL blocking questions) ✅ LANDED v0.3.30 | §7-h2, §9 |
| 15 | DOC | Packet self-sufficiency doctrine ✅ LANDED v0.3.30 | §8 |
| 16 | DEC | Autonomous/"confident" mode (hard-deps on #7 + #6) — ✅ COMPLETE: phase 1 v0.3.29, phase 2 (five-condition auto-commit clearance) v0.3.33 | §6f, §9 |
| 17 | BUG | **Asymmetric surfaced_reports dedup** → re-wake after review (high priority) ✅ LANDED v0.3.30 | §5b |
| 18 | CAP | `relay send --when-idle` queue (replaces unsafe until-loop) ✅ LANDED v0.3.32 | §9 |
| 19 | BUG | Handoff double SUCCESSOR AFTERCARE section ✅ LANDED v0.3.31 | §9 |
| 20 | BUG | Spawn writes a live marker when the launch never happened (no PID + no title) ✅ LANDED v0.3.30 | §12 |
| 21 | BUG | Resume loops on a never-created conversation id; claims success on a dead tab ✅ LANDED v0.3.30 | §12 |
| 22 | BUG | Wake dedup stamps surfaced on announce ATTEMPT, not delivery — busy-lead wake lost forever ✅ LANDED v0.3.32 (two-phase stamp; §13 diagnosis code-confirmed) | §13 |
| 23 | BUG | #22's delivery proof read the GLOBAL stop_hook_active flag — a foreign blocking Stop hook (rules-check) silenced wakes for hours ✅ LANDED v0.3.34 (relay-owned claim + transcript evidence) | §14 |
| d1 | CAP | Bash gate for leads on custody-vs-implementation lines (dry-run first) ✅ PHASE 1 (logging-only) LANDED v0.3.32 — blocking mode waits on tuned logs | §10 |
| d2 | DOC | Mutation-budget tripwire line in `/relay:mode` ✅ LANDED v0.3.30 | §10 |
| d3 | DOC | Standing ops-hands pattern (spawn an ops executor up front) ✅ LANDED v0.3.30 | §10 |
| d4 | BUG | Discipline markers must survive handoff + handoff-linter ✅ LANDED v0.3.31 | §10 |

**Definition of done for every behavioral fix (#1, #2, #17, d1, d4):** land it with a §0-style
*evidence table* — observed-live rows, not just a green suite. That evidence discipline is the best
property of this doc; make it the bar, not a one-time §0 artifact (Fable punchlist item 4).

---

## 1. `backend` is stamped at only one of four `write_marker` sites

**Finding.** `bin/relay:1792` (`cmd_lead_start`) passes `backend=iterm.NAME`. Three other sites do
not:

- `bin/relay:832` — `resume_lead` / lead restore
- `bin/relay:1850` — handoff successor pre-arm
- `bin/relay:1898` — handoff successor

The comment at 1850 says *"same stamping as `cmd_lead_start`"*. That is now **false**, and a false
comment is worse than no comment.

**Consequence.** A resumed or handed-off lead never records a backend, so every nudge to it goes
through `_probe_backend_for_tab`. That fallback works — proven repeatedly today — but only resolves
when exactly one backend has a tab matching the label. On 0 or 2+ matches it degrades to the caller's
ambient guess, which is precisely the Defect A bug 0.3.27 fixed.

**Evidence.** After a restart, this session's marker has no `backend` key at all, while
`alpha_service` (armed fresh via `/relay:mode` on 0.3.27) correctly has `backend: iterm`.
`revive_lead` is explicit that it restores everything untouched, so the resume path structurally
cannot stamp it.

**Not yet decided:** whether `revive_lead` should re-stamp backend on every revive (a lead *could*
resume in a different terminal app) or whether stamping at the three write sites is enough.

---

## 2. Tab-label assertion is label-aware, not identity-aware

**Finding.** `_background_label_loop` decides whether its work is done with:

```python
iterm_backend.title_is_live(label, iterm_backend.live_session_names())
```

That asks *"does **any** live tab carry this label?"* — not *"does **my** tab carry it?"*

**Why it matters.** Handoff inherits the predecessor's project name verbatim (see §4), so the
successor's `tab_label` is byte-identical, and handoff writes the successor's marker **before**
stepping the caller down. During that window two live leads share a label. The successor's label
loop sees the predecessor's tab, concludes the title already holds, and never asserts its own.

**Suspected real-world instance.** `alpha_service` was found with its tab titled
`[Lead] claude-relay` while its marker expected `[Lead] alpha-service` → `is_alive` False → its
wake had been silently dead for ~a day. The ledger shows `lead_handoff` on that project at
2026-07-18T09:12:14, with the marker last written 11:51 the same day. This is a hypothesis with
strong circumstantial support, **not** a reproduction. Reproducing it before fixing would be wise.

**Fix direction.** Check the tab by its stable handle (`iterm_session`), which is already stored and
already used elsewhere. Same root cause as the `nudge-lead` backend bug: identifying a session by a
mutable label instead of a stable identity.

**Staged landing (Fable punchlist item 2).** Build the **reproduction harness FIRST** and treat it
as the acceptance test — drive a handoff, assert the successor's tab ends up mislabeled under the
old code, then assert the identity-aware fix makes it pass. Do not fix before you can reproduce;
this is a hypothesis (see above), and a fix without a failing repro is a fix for a bug you haven't
proven exists.

---

## 3. Leads have no liveness check; executors do

**Finding.** `relay list`'s LEADS table has no status column — only `LAST ACTIVE`, which is just the
age of a `last_active` timestamp. There is no process probe and no tab probe. A dead lead is
indistinguishable from an idle one.

Executors, by contrast, get real status through `_check_one` (process + tab + report-file probes):
`busy` / `reported` / `stalled` / `dead` / `closed`.

**Evidence.** `beta_view` has been listed as a normal lead for 11 days with its tab gone
(`is_alive=False`, never stepped down, never tombstoned; its label `relay-lead-beta-view` predates
the current `[Lead] ` format). A proper step-down *does* clean up correctly — `clear_lead` removes
the whole subtree, and the four markers on disk match the four rows listed. The problem is only
leads that die *without* a clean exit: crash, closed tab, reboot.

**The ingredients already exist.** `iterm_session` + `tab_label` → `iterm.is_alive()` detected the
ghost in one call. And relay *already computes lead liveness*:

```python
LEAD_LIVE_WINDOW_SECONDS = 900   # "...treated as a ghost and never reserves its name"
```

`unique_lead_project` uses it to decide whether a name is taken — so relay knows how to call
`beta_view` a ghost, it just never tells the user.

**Proposed states.** `live` (tab alive, stamp fresh) / `unreachable` (stamp fresh but `is_alive`
False) / `ghost` (tab gone, stamp beyond window). **`unreachable` is the valuable one** — it is the
`alpha_service` failure, and today it was invisible until someone went looking by hand.

---

## 4. OPEN DECISION — suffix the project name on handoff?

`unique_lead_project` (auto-suffix) is called from exactly one place: `cmd_lead_start` (1766).
Handoff inherits the caller's name verbatim, by design — succession is continuity of the same
project, and it deliberately passes the predecessor's id in `exclude_sids` so the name is *not*
treated as taken.

**The user's argument for suffixing:** unique labels would eliminate the duplicate-label window in §2
outright.

**Argument against:** it makes labels *accidentally* unique rather than fixing the by-label
addressing that is the actual defect — the next thing that duplicates a label breaks identically.
It also costs identity: after N handoffs the project reads `claude-relay-10` in `relay list` and in
tab titles, and the name stops meaning "the project" and starts meaning "the generation".

**Not mutually exclusive.** §2 fixes the mechanism without renaming. The decision is whether to also
suffix as defence in depth.

**REFRAMED 2026-07-21 (the user's counter, accepted by Fable):** the suffix motivation was never
relay's addressing — it was the HUMAN's. Labels have two consumers, and §2/the field-vote only
addressed the machine one. Lived instance: post-handoff, the tab bar held TWO tabs titled
`[Lead] alpha-service` (stepped-down predecessor + live successor) among several other leads —
no internal fix helps a human glance at iTerm and know which is which.

**Synthesis (proposed — suffix NEITHER name):** retitle the PREDECESSOR's tab at step-down
(`[ex-Lead] <project>`), as one more action in handoff's final act — relay already owns tab titles
via the label loop. Result: exactly ONE tab ever says `[Lead] X` (the live one, clean canonical
name); the husk self-identifies until closed; the project name never inflates; and the §2
duplicate-label window also closes from the predecessor's side as a bonus. Generalize: `relay
close`/`retire` retitle lingering tabs to `[closed] <name>`. → fold into task #4 (now CAP, not
just DEC: the retitle-on-stepdown is buildable regardless of the suffix decision, and likely
dissolves it).

---

## 5. Notified twice — PART deliberate, PART a now-CONFIRMED bug

Two different things look like "notified twice." The first is by design; the second is a real dedup
gap **confirmed by code + field data (Fable, 2026-07-21).** Keep them separate.

**5a — DELIBERATE (do not "fix" by deleting a wake path).** Both wake paths are kept on purpose
(design §9.6 #2), on the user's explicit call — *"it is nice to see the lead saying I found
something"* — with the double-notify risk stated and accepted. It also saved a report today:
`exec-term`'s push failed silently (cross-backend bug) and the lead's own at-Stop check surfaced it
anyway. A genuine race between the two paths, or the lead announcing after the push nudged it, is
this accepted case.

**5b — CONFIRMED BUG: asymmetric dedup (→ task #17).** Field data from Fable (the "other lead"): all
four duplicate wakes were **post-0.3.27**, and the pattern was consistent — first notification while
idle → announce/act/sometimes commit across turns → then a *second* wake, minutes later, with the
same report key, after the report was already fully reviewed. Verified against the code
2026-07-21:

- `mark_surfaced` (the ONLY writer of `surfaced_reports.json`) is called in exactly one place:
  `hooks/stop_lead_watch.py:106`, inside the at-Stop wake path. It appears **nowhere in `bin/relay`**.
- So no *review* channel stamps the dedup — not `relay check`, not `relay diff`, not a commit, and
  **not the push** (`executor_escalation.py` writes only the executor's own `escalation.json`, never
  the lead's `surfaced_reports.json`).
- Result — an **asymmetric** dedup: at-Stop marks → push correctly sees `resolved` ✓; but push
  nudges → lead reviews in a user-prompted turn → nothing marks surfaced → the lead's next at-Stop
  check runs `new_reports_for`, still sees the key as new, and **re-announces an already-handled
  report.** ✗

This is exactly Fable's "category 2." Fix direction: stamp `surfaced_reports.json` whenever a report
is demonstrably surfaced/handled by *any* channel — most importantly when the push nudges the lead,
and/or when `relay check`/`relay diff`/commit touches a reported executor — so the at-Stop check
can't re-announce something already dealt with.

> **DO NOT "fix" this by marking surfaced on the mere NUDGE.** Marking before the lead actually
> reviews would suppress a legitimate re-announce whenever the lead ignores or misses the nudge —
> reintroducing silent loss, the exact thing the two-path design exists to prevent. The trigger MUST
> be "**handled**" (reviewed/committed/diffed), not "notified." This is the three-line shortcut a
> fast implementer will reach for; it is wrong. (Fable punchlist item 3.)

See task #17.

---

## 6. Ideas from another lead's retrospective (unassessed by that lead's own admission)

Captured verbatim in intent, with this project's perspective added.

**a. Required TL;DR block in the report format.** ≤10 lines: claims, counts, risk flags, UNVERIFIED
list. Lead reads the TL;DR always, the full report only when a risk flag is up. Reports run ~100
dense lines and get read with an expensive model.
*Assessment: best effort-to-payoff ratio of the set.* It is a text change to the REPORT FORMAT relay
already appends — no code paths touched — and relay already mandates a one-plain-sentence first line
for the notification, so this extends an existing idea rather than inventing one.

**b. Plugin-side verifier on report.** When an executor reports, the plugin (not the lead) spawns a
cheap verifier that re-runs the report's declared suite commands and diffs `git status` against its
claims, stamping `VERIFIED counts-match` or `MISMATCH: …`.
*Assessment: most interesting for this repo specifically.* Today produced several reports whose
claims were plausible and wrong — an unverified premise handed to an executor, a "cleaned up" that
had deleted nothing, and historically a green suite hiding a feature that had never once worked in
production. **Design caution: the verifier must not become another silent-failure surface** — "did
not run" must look different from "ran and matched", which is the §9.6a lesson.

**c. Approved-plan autopilot (`relay plan approve "<phase>"`).** After a phase is approved, auto-send
the next packet when a report lands clean; keep the confirm-gate for the first spawn and anything
marked needs-signoff.
*Assessment: this is a policy change, not a feature.* It directly relaxes `/relay:mode`'s
confirm-before-spawn gate and announce-and-wait rule, which exist to keep the human in the loop. The
ceremony is the point right up until it isn't. Needs an explicit decision about **where the line
sits** — suggested framing: autopilot within an approved phase, never across phase boundaries, never
for core logic / ledgers / parity tests.

**d. `relay land` — deterministic deploy pipeline.** Pure bash: push → FF check → ssh restart →
health poll → one-line report.
*Assessment: probably out of scope for relay.* The `commit-push-merge` and `restart-app` skills
already cover this chain, including the d2c specifics (systemd over ssh, no sudo via polkit).
Relay's scope is lead/executor orchestration, not deployment.

**e. Executor context-limit / turn-reuse awareness (the user, 2026-07-21).** Relay reuses an executor
across many packets (`exec-builder` served six tonight) with no awareness of how full that
executor's context is getting. Wanted: a **soft** signal, explicitly **not** a hard turn cap — when
the lead is about to `/relay:send` a follow-up into an existing exec, and when it sizes a packet at
spawn, it should have some sense of how heavy that exec already is, so reuse-vs-fresh-spawn is an
informed call rather than a blind one.

*Finding — the proxy exists, and the exec's transcript IS reachable by the lead (corrected
2026-07-21).* The transcript-weight machinery (`lead_guard.transcript_mb`, `handoff_nudge_mb`
default 5MB, calibrated at ~3MB per working day / ~6MB heaviest) drives the lead's own handoff
nudge via `transcript_path` from the `--statusline` payload — so a session can weigh *itself*. An
executor's `transcript_path` is not *stored* in its `session.json`, but it is **derivable**: every
exec's `session.json` carries `claude_session`, and the transcript lives at
`~/.claude/projects/*/<claude_session>.jsonl`. Verified: located a real exec's transcript by its
`claude_session` uuid and read its size in one glob. So the lead CAN price an exec's weight — no new
plumbing, just `glob(~/.claude/projects/*/<claude_session>.jsonl)` + `transcript_mb`.

*Organizing philosophy (the user / the multi-packet lead, 2026-07-21):* you can't prevent context rot,
but you can **price it** (visible in `relay list`), **gate it** (override-to-continue), and **make
the escape cheap** (seeded respawn). The four sub-items below map onto those three.

*Design notes for later.* (1) Soft/advisory first — surface an estimate, never refuse (that's the
"price it" layer). (2) The MB figure is a proxy for *transcript size*, not context-window
occupancy — good enough for "this exec is getting heavy," not a real token count. Say so wherever
it's shown. (3) Reuse the existing handoff-nudge thresholds rather than inventing new ones. (4)
Packet count alone lies (packet sizes vary wildly); transcript bytes is the honest proxy — show
both but threshold on bytes.

*Sub-items (from the multi-packet lead's retrospective, 2026-07-21):*

- **e1 — heaviness column in `relay list`.** Reuse the existing warning-stamp pattern
  (`ver?`/`stale-hooks`): per-executor `⚠ heavy: 9 pkts / 4.2MB — consider retiring`. Feasible now
  that the transcript is derivable. *(This is the "price it" layer; subsumes the original §6e ask.)*

- **e2 — soft gate on `relay send`, ceiling-style.** Mirror the model-ceiling pattern that already
  exists (`executor_model_ceiling` refuses unless `--model-override "<reason>"`, `bin/relay:526-535`):
  sending into a session past the heaviness threshold refuses with `session heavy — retire and
  respawn, or --heavy-override "<reason>"`, and the reason lands in the ledger. Converts "the lead
  should remember to rotate" into "the lead must consciously choose not to." *("gate it")*

- **e3 — `relay retire <session>`.** Close the session AND auto-write a `successor-seed.md` into its
  dir: an index of its packets/reports with their one-line summaries, so respawning a fresh executor
  for the same territory costs one packet-read instead of archaeology. *("make the escape cheap" —
  the item most likely to make rotation actually happen, because today retirement is expensive so
  leads reuse a heavy session instead.)*

- **e4 — one line appended to the auto-GATES text in every packet:** *"Treat this packet cold:
  re-read every file you touch; trust no memory of prior packets — earlier state may have been
  landed, reverted, or superseded since."* Cheapest of the four (one line of text in `bin/relay`'s
  appended GATES), and it removes the dependence on an executor's *instinct* to re-read. Directly
  targets the silent-drift class this whole session kept hitting; a fresh-context executor stays
  sharp precisely because it distrusts its own memory.

---

## 6f. Autonomous / "confident" mode — auto-continue, wait only when the lead genuinely needs the human (the user, 2026-07-21)

**The ask.** A way to tell the lead: *proceed on your own — commit clean reports, send follow-up
packets, spawn within the approved plan — and interrupt me ONLY when you actually need me.* the user
wants this **situationally**, "sometimes when I'm confident about the plan," not as a permanent
default. So it needs a **runtime toggle**, not only a static config key.

**Concretely, what this eliminates (the user, 2026-07-21) — the routine approval ceremony, nothing
more.** The exact round-trips to remove are the reflexive yes/no beats where the answer is almost
always yes:
- report comes back clean → today "ready to review, ok?" → **auto: review + commit, announce it**;
- next packet is the obvious next step in the approved plan → today "shall I send it?" → **auto: send**;
- an executor is needed for an already-planned piece → today "spawn X?" → **auto: spawn**.
These are the "(review → ok?) (spawn → do it) (commit → ok)" beats the user named — pure ceremony when
you already trust the plan. Everything on the stop-list below still stops. The dividing line: routine
*yes* beats get automated; *judgement* beats stay manual.

**Crucial framing (the user, 2026-07-21): this is NOT the lead making unilateral decisions.** It does
not mean "the lead autonomously decides things." It means the *default posture* inverts — today the
lead **waits by default**; here it **proceeds by default but still asks the moment it judges it
needs the human.** Same judgement, burden flipped. The lead keeps escalating exactly the calls it
would have flagged anyway (the stop-list below); it just stops asking permission for the clear,
in-plan, low-risk steps. "Autonomous" describes the default, not a licence to decide the hard
things alone.

**What it relaxes (be honest — this is the biggest policy departure in the backlog).** The
`/relay:mode` skill currently HARD-gates three things, deliberately, to keep the human in the loop:
- the **announce-and-wait** rule on every auto-wake (SKILL.md:99) — announce, then WAIT, never act;
- **confirm-before-spawn** (SKILL.md:159, 183) — propose the decomposition and WAIT for explicit go;
- **own sign-off gates** (SKILL.md:147) — core logic / ledgers / parity tests need explicit approval.

Autonomous mode flips the *default posture* of the first two from "wait" to "proceed," while the
third stays a hard stop. This is strictly broader than §6c (`relay plan approve "<phase>"`), which
only autopilots the *next-packet send within one approved phase*; this is a session-wide "just go."
§6c can be seen as the conservative subset — decide whether both exist or this supersedes it.

**The hard part is not the toggle — it's "genuinely needs the human."** A config flag is trivial;
the judgement of when to still stop is the whole feature. Proposed NON-negotiable stops even in
autonomous mode (the lead must pause regardless):
- a report with a **risk flag / failing tests / UNVERIFIED claim** that bears on correctness;
- anything touching **core logic, ledgers, parity/golden tests, migrations, deploys** (the existing
  sign-off gate — unchanged);
- an **irreversible or outward-facing** action (push to a shared branch, delete, external send);
- a **new piece of work not in the approved plan** (autonomy is *within* the plan, never expands it);
- genuine **ambiguity** the packet/plan can't resolve — i.e. exactly the exec→lead-question class of
  §7/§8, escalated one level up to lead→human.
Everything else — clean report → review → commit → send next packet → spawn the next planned
executor — proceeds without a round-trip.

**Shape — command-driven, NOT config-first (the user, 2026-07-21).** Because this is situational
("sometimes when I'm confident about THIS plan"), the primary interface is a **runtime relay
command**, not a config key. Config only sets the fallback default.
- Primary: `relay auto on` / `relay auto off` (and a `/relay:auto` skill alias so it's typeable in
  the lead session). `relay auto status` reports the current posture. State lives in the lead's
  marker (per-session), so it is scoped to *this* lead and resets on a fresh arm — you opt in each
  time you're confident, rather than it silently persisting.
- Scoping bound worth considering: `relay auto on --until-phase X` or `--for N` (packet count) so
  "confident about THIS plan" can't silently outlive the plan it was scoped to; auto-reverts to
  wait-for-human at the bound.
- Config is ALSO a first-class option, not just a fallback (the user, 2026-07-21): `autonomous_mode`
  sets the posture new leads arm with, defaulting to `false` (the safe default must stay
  wait-for-human). The user personally will use the command, but someone who *always* works this way
  can set the config once and every lead starts in auto — the command still flips it mid-session
  either way. Two audiences, both valid: the command is for "this plan, right now"; the config is
  for "this is how I always run."
- Every autonomous action still **logs to the ledger and surfaces in the wake/announce** — the
  human isn't *asked*, but must be able to reconstruct what happened. Autonomy must not become
  silence; a proceeded-without-you action should be *more* visible in `relay list`, not less.
- The auto-wake still fires; it just changes from "announce and WAIT" to "announce, act, and record"
  unless a stop condition above is hit.

**Risk to name plainly.** This removes the friction that has repeatedly caught real problems this
session (a report claiming a cleanup that deleted nothing; a green suite hiding a broken feature).
Autonomous mode is only as safe as the stop-conditions and the report-verification (§6b) behind it —
which is an argument for building §6b (plugin-side verifier) and §6/#6 (TL;DR risk flags) *before or
with* this, so "clean report" is machine-checked, not just claimed. Sequencing note, not a blocker.

**Needs the user's sign-off on where the line sits** — same as §6c, but higher stakes because it is
session-wide. This doc captures the proposal and the stop-list; the exact boundary is the user's.

---

## 7. Precondition / world-state hygiene (from a lead whose exec asked an interactive question mid-packet, 2026-07-21)

**The trigger.** An executor raised an interactive question *in its tab* partway through a packet.
That is wrong on two counts: packets are supposed to be self-sufficient, and — the part that
actually annoyed the user — the question interrupted **the human**, when its correct recipient was the
**lead**. The root cause the lead diagnosed: the packet silently assumed a world-state that wasn't
true (un-applied DDL / an unpulled checkout / staged-but-unlanded code not yet live on a port), and
the executor hit a step it couldn't perform against *currently deployed/committed* state.

**The philosophy:** an executor should never improvise a step it can't run against real current
state, and should never raise an interactive question for it — it should write a **partial report**
naming the missing dependency and let the **lead** (woken via the normal report→wake channel) fix
the world and resend. The human never sees it.

- **h1 — `relay send` warns when a packet has no `## Preconditions` section.** Pure grep, same
  warning-stamp style as `ver?`/`stale-hooks`/the proposed heaviness stamp. The section itself isn't
  the point — *authoring* it forces the walk the lead skipped ("engine table exists ✓, checkout
  pulled ✓, staged code NOT yet live on :8003 → so step 6 must…"). Both of that lead's misses would
  have died at authoring time, because you can't write the precondition list without noticing the
  step that violates it. Enforced-by-nag, not blocked — same philosophy as the heaviness gate (e2).

- **h2 — a paragraph appended to the auto-GATES text** (same mechanism as e4, `bin/relay:104`):
  *"If a step cannot be executed against currently deployed/committed/applied state with your
  permissions (needs a restart, un-applied DDL, an unpulled checkout, an unlanded commit), STOP at
  that point and report the dependency in your report file — never improvise the step, and never
  raise an interactive question for it."* This standardizes what good executors already do by
  instinct, but routes it through **report → lead wake** instead of an `AskUserQuestion` blocking in
  a tab the human happens to glance at. h2 is the one that directly removes tonight's annoyance;
  pairs naturally with e4 (both are GATES additions, could ship as one edit).

- **h3 (optional, only if wanted later) — `relay send --check "<shell cmd>"`** that must exit 0
  before the send goes through, for *machine-checkable* preconditions (`test -d path`, a curl
  health check, …). Judgement-shaped work; the h1 nag captures ~90% at zero complexity, so h3 is a
  "someday if the nag proves insufficient," not now.

## 8. Packet self-sufficiency check — the gap behind exec→lead round-trips (2026-07-21)

**Why this exists.** §7's h1/h2 handle *preconditions* (world-state) and *containment* (route a
blocker to the lead, not the human). But the deepest driver of exec→lead back-and-forth is neither:
it is **packet quality** — a vague or underspecified packet produces a stream of questions no
grep-able gate can catch. Mapping the causes:

| Cause of an exec question | Covered by |
|---|---|
| World-state precondition wrong | h1 (#13) — prevents at authoring |
| Stale-memory confusion | e4 (#12) — exec re-reads cold |
| Genuine surprise / something actually went wrong | h2 (#14) — redirects to lead, spares the human |
| **Packet is ambiguous / underspecified** | **nothing yet — this section** |

The user's own framing: *some* questions are legitimate and unavoidable, and the goal is not zero
questions — it is that the human stops being the one interrupted, and that the *avoidable* ones
never get authored in the first place. h1/h2/e4 shrink the first three rows; the fourth is the
largest lever and is currently untouched.

**Nature of the fix — different from h1.** Self-sufficiency is *judgement-shaped*, not
machine-checkable. h1 can grep for a `## Preconditions` heading; nothing can grep "is this packet
unambiguous." So this is a GATES-style instruction to the **LEAD** (at authoring / `relay send`
time), not to the executor, and not a hard gate:

> Before sending, read the packet as if you were an executor with **zero prior context and no access
> to this conversation**. Could you complete it — goal, files, acceptance criteria, and the exact
> end-state — from the packet text alone, without asking anything? If not, the missing piece is a
> bug in the packet, not a question for the executor. Fix it before sending.

**Placement options (decide at build time):**
- As lead-facing guidance in the `/relay:mode` skill (where the "define packets" discipline already
  lives) — no code, just doctrine. Cheapest.
- As a soft `relay send` nudge that prints the self-sufficiency prompt as a reminder before the
  first send of a packet — visible, but can't *verify* the answer (unlike h1's grep).
- (Rejected for now) an LLM-based "grade this packet" check — real tokens, and it re-introduces the
  verifier's silent-failure risk. Not worth it against a one-paragraph doctrine that costs nothing.

**Honest ceiling.** This will not eliminate round-trips — it cannot, since the real driver is how
carefully the lead writes, which is exactly why `/relay:mode` insists on a strong reasoning model in
the lead seat. It raises the floor on the *avoidable* fraction. Pairs with h1 (preconditions) and
e4 (cold re-read) as the three-part "packet is trustworthy on arrival" story.

## 9. Field review incorporations (Fable — relay's heaviest user, 2026-07-21)

Fable reviewed this backlog as the primary field user and gave evidence-based verdicts. Refinements
we ACCEPTED into the existing items (each verified or judged sound):

- **§5b — the asymmetric dedup bug** (above) — Fable's field observation, code-confirmed. → task #17.
  The single highest-value item in the review.
- **§4 (suffix decision) — field vote: DON'T suffix.** Fable's argument: the project name is
  load-bearing in human communication ("check the alpha lead" must stay meaningful), and
  suffixing treats the symptom §2 cures. Still the user's call, but the heaviest user now recommends
  against. Recorded in §4.
- **h2 (STOP-and-report, #14) — BROADEN to ALL blocking questions, not just world-state.** Fable's
  execs raised three interactive questions; only two were world-state, the third was judgement-shaped
  and *also* belonged with the lead, not in a tab. The interrupt-the-human channel was the problem,
  never the round-trip latency. → widen #14's GATES text to cover any blocker, not only
  deploy/DDL/checkout ones.
- **§6a (TL;DR, #6) — UNVERIFIED list is MANDATORY in the TL;DR, not optional.** Fable: across ten
  packets the honest UNVERIFIED section was consistently the single most valuable line — it's where
  the lead decides whether to deep-read. → pin in #6.
- **§6b (verifier, #7) — TEMPER, loudly.** Field data: across ~15 reports, a counts-verifier would
  have caught exactly one thing (a lint miss). Every *dangerous* problem was premise-level — wrong
  oracle, wrong write-side, suite green in the wrong venv — invisible to "re-run the declared
  commands." Build it, but the doc/stamp must say a `VERIFIED counts-match` **must never be read as
  "report is true"**, or it displaces the lead judgement that caught the real ones. §9.6a is right
  but not sufficient here. → add this caveat to #7.
- **e2 (heavy-gate) — copy must NUDGE, not scold.** Heaviness ≠ degradation: a disciplined executor
  can do its best work deep in a session (Fable's exec-seam packets 9–10). The override will be used
  legitimately and often, so word the refusal accordingly. → note in #10.
- **e3 (`relay retire`, #11) — the sleeper.** Fable retired executors late purely because respawn
  felt expensive; the seed file changes the economics, which changes the behaviour. Endorsed as
  higher-value than its position suggests.
- **§6f (auto mode, #16) — two additions:** (1) the announce-and-record on an autonomous action
  should include *what would have been asked* ("proceeded: committed X — under manual mode this
  would have waited for your go"), so the human audits the judgement, not just the action; (2)
  upgrade the sequencing note to a **HARD dependency**: #7 (verifier) + #6 (TL;DR risk-flags) are the
  *definition* of "clean report" that #16's safety rests on — not merely nice-to-have-first. → fold
  into #16.
- **§3 (lead liveness) — add a cheap sibling: show the lead's OWN transcript MB in the LEADS table.**
  Fable's handoff nudge fired at 5.1MB as a surprise; a visible climbing number all day would have
  let it plan the handoff a phase earlier. → add to #3.

New items Fable surfaced (tasks below): **#17** (dedup bug), **#18** (`relay send --when-idle` queue),
**#19** (handoff aftercare double-append).

**Suggested ship order (Fable), endorsed — amended 2026-07-21 with §10:** §1+§2+#17 →
e4+h2(broadened)+d2+d3 (one skill/GATES text pass) → §6a → e1/e2/e3 + lead-MB column → h1+d4 →
§3 → #18 (`--when-idle`) → d1 (Bash gate, after its sibling gates are field-tested) → *then and
only then* the 6c/6f policy tier, with #7 as its hard prerequisite. Rationale: **bugs →
trust-infrastructure → autonomy**, because each tier is the safety case for the next; and
text-tier items batch into single passes so the doc-shaped work doesn't fragment across releases.

## 10. Lead-drift containment (from the alpha successor's incident, 2026-07-21)

**The incident.** A freshly handed-off lead drifted from verification (legitimately lead work) into
implementation (box provisioning: npm install/build, service setup) without noticing the line-cross.
The human caught it, not the tooling. The lead's own diagnosis: the drift ran entirely through Bash
and MCP tools, which the edit-gate structurally doesn't cover — a KNOWN hole (`/relay:mode`'s own
text documents it) with no backlog item until now. Contributing cause, owned by the predecessor
(Fable): the handoff doc dropped the `[ops-not-lead-work]` discipline marker AND phrased the ops
queue item as the lead's own task — so part of the drift was executing a mis-scoped instruction.
Four items fall out, three from the successor (assessed by Fable), one from Fable:

- **d1 — Bash gate for leads, on CUSTODY-vs-IMPLEMENTATION lines (the real fix, but the heuristic
  decides whether it survives).** Extend the PreToolUse gate to Bash for lead sessions, with the
  `retain "<reason>"` escape. **Do NOT gate by mutating-vs-read-only** — the lead's *assigned custody*
  is mutating: `git commit/push` of reviewed staged work, `systemctl restart` + health check after a
  landing, applying an executor-authored DDL file, running suites. Fable's session ran dozens of
  those legitimately; a naive mutating-gate fires on every one, `retain` becomes reflexive within an
  hour, and alarm fatigue kills the deliberate-judgment moment the gate exists to create. Gate the
  **implementation verbs** instead: `npm install`/`npm run build`/compilers on any box, cloning
  repos into place, writing/installing service files, `sed -i`/heredoc/`tee` file mutation, `rsync`.
  Free-pass the **custody verbs**: `git commit|push`, `systemctl restart|status`, `ssh ...
  clickhouse-client < *.sql`, test/suite invocations, and all reads. Under that split the gate
  catches this incident and stays silent through an entire healthy session (field-calibrated against
  Fable's full day). Expect the allowlist to need tuning; start permissive on custody, strict on
  provisioning. **Dry-run FIRST (Fable punchlist item 2):** ship the gate in a logging-only mode
  (`would-have-blocked: <cmd>`, never actually blocking) for a few real lead-days, then tune the
  custody allowlist against actual logs before it ever refuses anything. That converts "expect
  tuning" from a caveat into a mechanism, and avoids alarm fatigue killing the gate on day one.
- **d2 — mutation budget line in `/relay:mode`'s role text (zero-cost tripwire).** One sentence:
  *"More than ~3 mutating Bash commands in a row means you are implementing — stop and packet it."*
  Prose is weaker than d1's hook but ships today and fires before the hook exists. Same
  doctrine-tier as §8.
- **d3 — standing ops-hands pattern (doctrine, not code).** In `/relay:mode`: any queue containing
  box/deploy/env work gets a cheap ops executor (Haiku/Sonnet) spawned up front, and ALL
  ssh/build/service commands route through it by convention — the lead then has no reason to touch
  the box. This is the old `[ops-not-lead-work]` discipline re-derived from scratch by the lead who
  suffered its absence — decent evidence the rule was right all along.
- **d4 — discipline markers must survive handoff (the root-cause fix).** Both this incident and the
  dropped marker are one failure class: **discipline erosion at succession boundaries.** Add to the
  handoff skill: the successor doc must carry every `[discipline]` marker the predecessor's did,
  verbatim — and the cheapest enforcement is a handoff-time linter (grep the predecessor's handoff
  for `\[[a-z-]+\]` markers, warn on any missing from the new doc; same warning-stamp style as h1).
  Also instruct handoff authors to name an EXECUTOR (or "delegate this") on every queue item, never
  phrase ops work as the lead's own.

**Ship-order placement:** d2+d3 ride any skill-text edit (cheapest tier, with e4/h2); d4's linter
alongside h1 (same mechanism); d1 after the h1/e2 gate patterns exist to copy — its risk is not
code, it's heuristic tuning, so land it behind field-tested siblings.

## 11. Loose ends not covered above

- **`CLAUDE_PLUGIN_ROOT` in a `statusLine` command** — assumed unavailable when writing
  `examples/statusline.sh`; never verified. If it *is* available there is a simpler one-line
  resolver and the documented pattern is harder than it needs to be. Not a defect either way.
- **`beta_view` ghost lead** — 11 days stale, safe to prune once §3 makes it visible.
- **~80 closed/superseded executor sessions** — `relay prune --dry-run` to see what's clearable.
- **This session** was on 0.3.25 for most of the day and restarted onto 0.3.27; its marker still has
  no `backend` (see §1), so it continues to exercise the probe fallback.

## 12. Spawn/resume launch-race incident (Fable successor lead, 2026-07-21, observed live)

**The incident.** A `relay spawn` (rl-auto, opus) hit a launch race — plausibly because the user
was actively typing in iTerm during the AppleScript tab setup. Symptoms: "tab title didn't take"
+ "could not read PID". The claude process in the tab never started, so the pre-pinned
`claude_session` UUID (passed via `--session-id`) was never registered with Claude Code. Two
defects compounded from there:

- **#20 — spawn writes a live marker for a launch that never happened.** `cmd_spawn` treats
  missing PID + failed title as mere warnings and still writes `status: busy`. The session then
  shows dead only via later aliveness probes; nothing says "claude never launched." Spawn should
  detect the both-signals-failed case and either retry the launch or write the marker as
  launch-failed with a suggested `relay restart`.
- **#21 — resume trusts a pinned conversation id that points at nothing, and reports success on a
  dead tab.** `relay resume` ran `claude --resume <uuid>` on the never-created id; claude exited
  instantly (iTerm: "A session ended very soon after starting"), yet resume printed
  "resumed … pid N" — the pid was already gone. Every retry fails identically forever; the id can
  never become valid. Fix: before relaunching, verify the pinned conversation exists (or at
  minimum, verify the relaunched pid survives a short grace window); on the never-started case,
  say so and point at `relay restart` (which mints a fresh UUID — it recovered this incident on
  the first try).

Also more field evidence for **#2** (identity-aware tab tracking): the label was the only handle,
and it was the thing that failed.

## 13. Lost wake for a busy lead — mark-surfaced-on-attempt (#22, observed live 2026-07-22)

**The incident.** rl-flow's report landed at 22:37:54 while its lead was mid-turn. The executor's
one-shot escalation nudge fired at 22:38:00 against the busy tab (spent, undelivered). No lead
wake ever announced the report across ~95 minutes of subsequent idle windows — a later wake (~00:00)
announced a DIFFERENT executor's fresh report but not rl-flow's, proving `rl-flow:1` was already in
`surfaced_reports.json` by then, though nothing legitimate (check/diff/close/delivered-wake) had
stamped it. The human noticed the executors were done; the tooling didn't.

**Diagnosis (evidence-fit, not yet code-traced to the exact line).** The Stop-hook announce path
stamps `mark_surfaced` when it fires, then relies on its exit-2/stderr reaching the lead — but a
firing that cannot actually be delivered (lead busy / turn raced / harness dropped it) still keeps
the stamp. Mark-on-attempt, not mark-on-delivery. The mirror image of #17 (which stamped too little
→ duplicate wakes); this stamps too early → silently swallowed reports, strictly worse. The one-shot
escalation (wake-watch §9) is also spent against a busy tab, so both layers die on the same busy
window and nothing retries.

**Fix directions to evaluate (#22):** stamp surfaced only at a point that proves delivery (e.g. the
lead's own next-turn acknowledgment, or check/diff/close per #17); or make the announce idempotent
across polls until something #17-shaped confirms the lead saw it; and make the executor escalation
re-arm when the nudge landed on a demonstrably busy lead instead of burning its one shot.

## 14. The #22 fix's own regression — a foreign hook's continuation read as relay's (#23, field incident 2026-07-22)

Full incident write-up (evidence, mechanism, fixes): ~/.relay-tasks/incident-wake-miss-2026-07-22.md
— authored by the field lead who diagnosed it from the ledger and the hook source; the diagnosis was
verified correct line-for-line. Summary: v0.3.33's #22 fix used `stop_hook_active` as proof that
relay's wake was delivered, but Claude Code sets that flag for ANY blocking Stop hook's
continuation. In an environment running a personal rules-check Stop hook that blocks on edit turns,
every post-block turn masqueraded as relay's post-wake re-run: the sync announce was suppressed and
never-delivered pending wakes were promoted to surfaced. Two executor reports sat silent ~2 hours;
the human was the detector — again.

Fixed in v0.3.34 per the write-up's three recommendations: relay keeps its own announce claim
(nonce + transcript byte offset) and promotes only when its wake text is provably in the lead's
transcript past that offset; pending wakes re-announce on every Stop regardless of why the session
continued (promote-before-announce closes the loop the old skip feared); `relay list` footnotes
reports never proven delivered. Lesson for the record: a harness-global signal is never a
plugin-private receipt, and the missing test environment (a SECOND blocking Stop hook) is now
simulated permanently in the suite.

Loose end from the same cycle: `relay verify`'s claim-plausibility filter false-positives on bare
basenames and dotted identifiers in report prose (observed on rl-wake's own report — MISMATCH on
`lead_guard.py`, `lg.relay`, `e.g`). Tuning item, not urgent: the failure direction is a false
STOP, which costs a question, never trust.
