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

## Addressing

Executors get `--name`-stable identities at spawn (relay already names sessions); the lead's
address is recorded in the executor's session.json at spawn (`lead_msg_name`), and vice versa.
ListAgents at send time resolves name→address; a resolution miss falls back to the old path
(phase 2) or errors honestly (phase 3). Collision behavior must be tested with 3+ executors.

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

- Inbound-control default for a `--settings`-spawned executor messaging its lead — accept or hold?
- Does SendMessage from a hook/script context (executor's Stop) work, or only from Claude's turn?
  (Docs mention CLAUDE_CODE_MESSAGING_SOCKET available to hooks — verify.)
- Name collisions across projects (two leads each with an `[Exec] fix` executor).
- The 50-queued-messages cap under a 5-executor fan-out reporting into a busy lead.
- Message arrival mid-turn: confirm "between tool calls" injection doesn't disrupt an executor
  mid-implementation (the lead's announce-act posture handles it; executors need the packet-cold
  discipline to not derail).

## Risks / rollback

Phase 2 keeps the entire old stack live as fallback — rollback is a config flag
(`transport_v2: off`). Phase 3 is the only destructive step and waits for field proof. The
release rule applies at every phase (bump or it silently no-ops).
