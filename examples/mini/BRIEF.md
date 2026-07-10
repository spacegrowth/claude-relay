# mini — build brief (the cheap serial → parallel → serial smoke test)

**Goal.** Build `textops`: a tiny **standard-library-only** Python (3.9+) CLI with two subcommands —
`textops stats "some text"` (word count) and `textops shout "some text"` (uppercase + "!") — both
built on one shared `clean()` core. Deliberately minimal: each module is ~10–20 lines plus a small
test file. **Executors should run on a cheap model** (`--model haiku`) — the modules are simple
enough that this whole run should cost a fraction of the bigger examples.

**This is a relay orchestration demo — delegate it, don't build it inline.** The point is the flow,
not the code: one serial foundation, then two parallel extensions (one via session REUSE, one via a
fresh spawn), then the lead integrates.

## Phase 1 — SERIAL (the foundation both extensions need)

1. **`textops/core.py`** — one executor builds this first, alone.

## Phase 2 — PARALLEL (two independent extensions, built at once)

2. **`textops/stats.py`** — **reuse the Phase-1 executor** via `/relay:send` (it already knows the
   codebase — this leg demonstrates session reuse).
3. **`textops/shout.py`** — **spawn a fresh executor** (this leg demonstrates parallel fan-out).
   Both run at the same time.

## Phase 3 — SERIAL (integration; the LEAD does this)

4. **`textops/cli.py`** + the two-line `textops/__main__.py` (and empty `__init__.py` if needed),
   then run the full `pytest`.

## Interfaces (the contract — honor exactly so the pieces compose)

```python
# textops/core.py   (Phase 1, serial)
def clean(text: str) -> str:
    """Trim, collapse all whitespace runs to single spaces, lowercase.
    clean("  Hello   WORLD ") == "hello world" ; clean("") == "" """

# textops/stats.py  (Phase 2, via /relay:send to the Phase-1 executor)
def word_count(text: str) -> int:
    """Number of words in clean(text). word_count("  Hello   WORLD ") == 2 ; word_count("") == 0"""

# textops/shout.py  (Phase 2, fresh executor, parallel)
def shout(text: str) -> str:
    """clean(text) uppercased with one trailing '!'. shout("  hey   there ") == "HEY THERE!" """

# textops/cli.py    (Phase 3, the LEAD integrates)
def main(argv: list) -> int:
    """Dispatcher: stats <text> | shout <text>. Prints the result, returns 0.
    `python -m textops stats "Hello   world"` prints 2."""
```

## Acceptance

- Standard library only; each module ships `tests/test_<name>.py` covering its docstring examples.
- `python -m pytest` green; `python -m textops stats "Hello   world"` → `2`;
  `python -m textops shout " hey  there "` → `HEY THERE!`.

## Per-executor rules

Stdlib only; touch only your module + its test; your tests pass before you report; stage (don't
commit) for the lead to review and integrate.
