---
name: tier
description: >-
  Flip this lead session's model-tier posture: who decides which model an executor runs on — auto
  (the lead decides per packet by the model rubric, default), manual (the human decides at EVERY
  spawn/rotate/upgrade), or lead (executors mirror this lead's own model class). Invoke with
  /relay:tier, or when asked "who picks the executor's model", "make me choose every model",
  "match executors to my own model", "go back to automatic model choice", "what tier am I in".
arguments: [auto|manual|lead|status]
---

Call relay as `${CLAUDE_PLUGIN_ROOT}/bin/relay` (Claude Code substitutes the plugin's absolute path
when this skill loads) — not bare `relay`, which often isn't on the Bash tool's non-interactive PATH.

Run ONE of these, matching what the user asked for (default to `status` if they only asked what
posture you're in):

```
${CLAUDE_PLUGIN_ROOT}/bin/relay tier auto     --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay tier manual   --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay tier lead     --session "$CLAUDE_CODE_SESSION_ID"
${CLAUDE_PLUGIN_ROOT}/bin/relay tier status   --session "$CLAUDE_CODE_SESSION_ID"
```

This only works in a lead session (`/relay:mode` first); it exits with an error otherwise.

**Then tell the user, in one line, which posture you now hold and what that changes** — the command
prints it, but the user needs to hear it from you, because the posture governs how *you* pick an
executor's model from here on. Use the `🚦 [relay]` marker like every other lead message.

- **`auto`** (default) → you decide each executor's model per packet, by the spawn model rubric
  (unknown-root-cause / cross-cutting / core-logic → opus; bounded features and bugfixes → sonnet,
  the workhorse; mechanical, fully-specified, verifiable-by-command → haiku). Today's behaviour.
- **`manual`** → the human decides at EVERY `relay spawn` / `relay send --rotate` / `relay send
  --upgrade`. **This is the one posture that ADDS A STOP — it is never overridden by autonomous
  mode.** Omitting `--model` on any of those three now refuses instead of picking one for you.
- **`lead`** → executors with no explicit `--model` mirror YOUR OWN model class (Haiku/Sonnet/
  Opus/Fable), resolved to a concrete id through the same alias resolver spawn uses.
  `executor_model_ceiling` still caps it exactly like any other spawn; an explicit `--model` still
  wins. If you run `/model` mid-session, executors follow starting at the next spawn.

**The manual protocol — when tier is `manual` and you're about to spawn/rotate/upgrade with no
`--model` in hand, in exactly these three steps:**

1. Run `${CLAUDE_PLUGIN_ROOT}/bin/relay tier ask --session "$CLAUDE_CODE_SESSION_ID" --packet <the
   packet you're about to send> --json` (omit `--packet` for a bare "this spawn").
2. Put its `options` UNCHANGED into `AskUserQuestion` — the recommended option first, exactly as
   the JSON lists it; never re-order, re-word, or drop one yourself.
3. Pass the human's pick as `--model` on the `spawn` / `send --rotate` / `send --upgrade` you were
   about to run.

If `relay tier ask` exits non-zero (`no executor model resolves on this machine — run relay
doctor`), that IS the answer — surface it and don't ask a question with no options.

**Layering, so nothing gets confused for something else:** config `executor_default_model` /
`executor_model_ceiling` (the machine default, and its ceiling on everything) < `relay tier` (this
lead session, resets to `auto` on every arm exactly like the autonomous posture does) < `--model`
on one spawn (always wins, one packet only).

The posture is **scoped to this session**: it resets to `auto` the next time lead mode is armed
(`/relay:mode`), so it can't silently outlive the session it was set for.
