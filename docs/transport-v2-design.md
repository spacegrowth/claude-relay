# Transport v2 — native cross-session messaging replaces the Stop-hook wake stack

Status: DESIGN, approved direction by the user 2026-08-02 ("move away from all our hacks").
Prereq: Claude Code ≥ 2.1.224 (machine is on 2.1.226). Docs: code.claude.com/docs/en/cross-session-messaging.

## Why (one paragraph of scar tissue)

Every wake incident in the backlog — #17 (duplicate wakes), #22 (mark-on-attempt), #23 (forged
delivery receipts), #27 (self-diff self-suppression) — was relay hand-building message-delivery
semantics out of Stop hooks, exit-2 side channels, ledger stamps, and transcript grep. Claude Code
now ships those semantics natively: `SendMessage` queues between the target's tool calls when busy,
starts a new turn when idle, and reports the delivery outcome to the sender. The transport layer of
relay should be that, and nothing else.

## What DIES (phase 3 deletes, phases 1–2 bypass)

- hooks/stop_lead_watch.py's announce/pending/claim machinery: mark_pending, promote_pending,
  announce_claim, WAKE_DELIVERY_NEEDLE transcript grep, the background poller, WAKE_RETRY_CAP.
- hooks/executor_escalation.py's one-shot push + re-arm logic (its queue-delivery trigger moves).
- surfaced_reports.json as a wake-dedup store (it remains only if something still needs
  "has the lead reviewed this" state — the auto-commit clearance does NOT: it reads verify).
- Post-launch keystroke typing: cmd_send's iterm.send path, nudge-lead's tab typing,
  _deliver-queued's typed delivery. (#18's queue persists as a thin shim or dies — see Phase 2.)

## What STAYS (not transport, or explicitly not covered by messaging)

- Spawn: "independent sessions are user-started, not programmatically spawned" — tab creation,
  §15c bootstrap-via-file, launch honesty (#20/#21), identity-aware tab ops (#2), retitles (#4),
  colors. One keystroke moment at birth (the short `sh` line), then the wire goes silent forever.
- All non-transport machinery: markers, ledger, sessions.jsonl, verifier (#7), clearance (#16),
  Bash gate (d1), GATES/report format, relay list, retire/seed, handoff.

## Message envelope (plain text only, by platform constraint)

One-line, grep-able, versioned prefix — same philosophy as the TL;DR block:
  `[relay-v2] report <sid> <packet> — <first line of report>`
  `[relay-v2] packet <sid> <packet> — <goal first line>` (lead→executor send)
  `[relay-v2] nudge <sid> — <text>`
Receivers treat the envelope as a POINTER (read the real file at the known path), never as the
payload — same discipline as spawn's pointer message. No conversation history, no file contents.

## Addressing (CORRECTED by phase-1 evidence, 2026-08-02)

PRIMARY: the `uds:` inbox socket, resolved AT SEND TIME from the messaging registry
(~/.claude/sessions/<pid>.json) by the Claude session id relay already records (owner_lead /
claude_session) — see lead_guard.peer_address (skips dead pids; newest live entry wins).
FALLBACK: ListAgents name+ref. Phase 1 proved bare names HARD-FAIL on collision (a twice-resumed
lead collides with itself; refs are only printed at list/error time, so they cannot be recorded
at spawn). Never resolve at spawn time — a resumed session gets a new pid and socket.

## Phases

1. PROTOTYPE (no existing code touched): a spawned executor's report step sends
   `[relay-v2] report …` to the lead via SendMessage; measure: delivery when lead idle, queueing
   when lead busy, outcome notification, inbound-control default for a spawned executor
   (acceptance criteria: no `hold` dialog in the hot path — if `hold` is the default, find the
   settings shape that makes executor→lead `accept` and document it). GATES text gains the send
   step behind a config flag (`transport_v2: "prototype"`).
2. MIGRATE WAKE + SEND (old machinery becomes fallback): report delivery and lead→executor packet
   pointers ride messaging when available; Stop-hook stack still runs for pre-2.1.224 or when
   ListAgents can't resolve. `--when-idle` queue: delivery trigger becomes native queueing (a
   busy target queues the message itself); the persisted queue file remains only as the
   crash-survival record. Ledger keeps recording queued/delivered from the sender's outcome
   notifications — provable delivery stays.
3. DELETE: remove the fallback after N clean field days (user's call), delete the dead wake
   machinery, close the backlog items as superseded-by-v2 in §17 of the backlog doc.

## Open questions phase 1 MUST answer (do not design around guesses)

> **ANSWERED — phase 1 complete (rl-msg packet 001, 2026-08-08, landed v0.3.39).** Full evidence:
> `~/.relay-tasks/rl-msg/packets/001-report.md`. Verdict: GO for phase 2, with two corrections that
> OVERRIDE this doc's assumptions wherever they conflict:
> 1. **Address by socket, not by name.** Bare-name SendMessage hard-fails on collision (observed
>    live: a twice-resumed lead collides with itself), and the disambiguating ref is not recordable
>    at spawn. Primary address = `uds:` socket resolved AT SEND TIME from Claude Code's
>    `~/.claude/sessions/<pid>.json` registry by the session id relay already stores
>    (`lead_guard.peer_address`); ListAgents name+ref is the fallback. This doc's `lead_msg_name`
>    primitive is dead.
> 2. **Delivery is turn-driven, not hook-driven.** `CLAUDE_CODE_MESSAGING_SOCKET` works only for a
>    session's OWN socket; a foreign process writing to another session's socket is dropped by
>    sender-class verification (reparented-sender control probe, same socket/settings: dropped).
>    The report send must be a GATES instruction, never a Stop-hook push.
> Also load-bearing: default inbound delivery requires sender/receiver permission classes to
> MATCH — phase 2 gates on `crossSessionInbound: "accept"` at the lead (user/project settings; relay
> can inject it only into the executor `--settings` file), and the `[relay-v2] report <sid> <pkt>`
> envelope prefix is now what defeats the identical-repeat dedup — keep it verbatim. Per-question
> detail below stays for the record; the report's evidence table answers each row.

- ANSWERED (phase 1): default delivery requires sender/receiver permission classes to MATCH
  (bypass<->bypass or prompt<->prompt); mismatches are HELD invisibly. Phase 2 is GATED on
  `crossSessionInbound: "accept"` in the lead's user settings, and adds the key to the executor
  --settings file for the reverse direction.
- ANSWERED (phase 1): scripts can write only to their OWN session's socket (sender process-class
  verification; a reparented control probe was dropped). Delivery is TURN-DRIVEN: the report send
  is a GATES instruction, never a Stop-hook push.
- Name collisions across projects (two leads each with an `[Exec] fix` executor).
- The 50-queued-messages cap under a 5-executor fan-out reporting into a busy lead.
- Message arrival mid-turn: confirm "between tool calls" injection doesn't disrupt an executor
  mid-implementation (the lead's announce-act posture handles it; executors need the packet-cold
  discipline to not derail).

## Risks / rollback

Phase 2 keeps the entire old stack live as fallback — rollback is a config flag
(`transport_v2: off`). Phase 3 is the only destructive step and waits for field proof. The
release rule applies at every phase (bump or it silently no-ops).
