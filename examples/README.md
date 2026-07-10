# relay examples

Runnable examples that double as end-to-end tests and teaching material for the
lead → delegate → report → review → integrate loop. Each hands the lead a `BRIEF.md` (goal +
interface contract + acceptance); the lead writes the packets — nothing is pre-scaffolded.

| Example | Shape | Teaches | Rough cost* |
|---|---|---|---|
| [`mini/`](mini/) | serial → parallel → serial, tiny | the cheapest full-shape run: serial foundation, reuse + fresh-spawn in parallel, lead fan-in — haiku executors, ~15-line modules | ~80–150k tokens |
| [`textkit/`](textkit/) | flat parallel fan-out | the minimal loop: 3 independent modules built at once, lead integrates `cli.py`, one `pytest` proves they compose | ~250–450k tokens |
| [`calc/`](calc/) ⭐ | serial → parallel → serial | real dependencies: sequencing, session reuse (`/relay:send`), parallel spawn, fan-in — and a calculator you'd actually use | ~400–700k tokens |

Start with `mini` to smoke-test an install cheaply; `calc` exercises the whole toolkit.

\* Order-of-magnitude — swings with how much each executor iterates.
