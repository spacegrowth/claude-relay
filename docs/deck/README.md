# Relay deck

`relay-deck.html` — an 18-slide talk about the plugin. Single file, no build, opens from disk.

## Presenting

Open the file in a browser and go full screen. Keys: `→` / `space` reveal the next step, then the
next slide; `←` back; `Home` / `End`; `n` toggles speaker notes; `?` shows this. Deep link with
`#/9`. Reduced-motion users see every slide fully revealed.

## Timing (30 min + demo)

| slides | minutes | what |
|---|---|---|
| 1–2 | 3 | the picture, and how it differs from built-in subagents |
| 3 | 2 | the thesis: strong lead, bounded tasks for the rest |
| 4–5 | 4 | the loop, the tabs |
| 6–7 | 5 | the two contracts: packet, report/diff/verify |
| 8–9 | 4 | choosing a model, effort |
| 10–12 | 6 | caching (two slides), the bill |
| 13–14 | 4 | reuse/rotate/hand off, how executors close |
| 15–18 | 4 | guardrails, six steps, skills, what's next |
| demo | 10 | below |

## Demo script (10 min)

Before the talk: a repo with a green test suite, `terminal-notifier` installed, iTerm open.

1. `/relay:mode` — read the model check out loud.
2. Show the packet (already written, 8 lines):
   ```
   Add a one-line docstring to every public function in src/util.py that lacks one.
   REPO: <path> — files: src/util.py
   ACCEPTANCE: python3 -m pytest -q tests/test_util.py green; `grep -c '"""' src/util.py` increases.
   BOUNDARIES: stage only, never commit. Touch no other file.
   ```
3. `/relay:spawn <path> docstrings <packet> --model haiku` — point at the new tab.
4. `/relay:list` while it works — CTX, TOKENS ·warm.
5. Wait for the wake (`🚦 [relay] — review needed`). If it hasn't landed in 3 minutes, `/relay:check`.
6. `/relay:verify <sid>` — show COUNTS-MATCH; say why it isn't "PASS".
7. `/relay:diff <sid>` — read the diff on screen.
8. `git commit`. Two minutes later `relay list` shows it auto-closed (landed).
9. `relay stats --since 1` — the row for the packet you just ran.
