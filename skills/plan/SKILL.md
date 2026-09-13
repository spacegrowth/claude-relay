---
name: plan
description: >-
  Show or edit this lead's ordered work queue (plan.json): one table of every planned packet with
  its target, model and derived status (queued / in-flight / reported / done), plus add / rm / done /
  bind and the exact command for the next queued item. Invoke with /relay:plan, or when asked
  "what's left", "show the queue", "add this packet to the plan", "what's next".
arguments: [status|add <packet> [--target fresh|<sid>] [--model m] [--note "..."] [--before N]|rm N|done N|next|bind N <sid>]
---

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.
Always pass `--session "$CLAUDE_CODE_SESSION_ID"`; this only works in a lead session (`/relay:mode`
first) and exits with an error otherwise.

Run the one matching what the user asked for (default to the bare table):

```
${CLAUDE_PLUGIN_ROOT}/bin/relay plan                                   --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay plan add <packet.md> [--target fresh|<sid>] [--model m] [--note "..."] [--before N] --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay plan rm N                              --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay plan done N [--note "..."]             --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay plan bind N <executor sid> [--packet P] --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay plan next                              --session "$CLAUDE_CODE_SESSION_ID"
```

- **Status is derived, never typed in**: `queued` = not yet picked up by any spawn/send; `in-flight`
  = a spawn/send of that exact packet path by one of your executors was seen in the ledger (bound
  automatically) and no report yet; `reported` = its report file exists; `done` = the packet landed,
  or you ran `plan done N` for work landed outside relay. `plan bind` covers what the automatic
  match can't see (e.g. a packet sent from a different path).
- **`plan next` never sends** (v0). It prints the exact `relay spawn …` / `relay send …` command for
  the first queued item, or exits 1 with no output when nothing is queued. Running that command is
  still an ordinary spawn/send decision — announce it and wait for the human's go as usual (or
  proceed under `/relay:auto` if it is routine and in-plan).

**Then answer the user with one `🚦 [relay]` line per queued item** — `#N packet → target (model):
note` — followed by one line summarising the footer counts (in flight / reported / done). If nothing
is queued, say so in one `🚦 [relay]` line.
