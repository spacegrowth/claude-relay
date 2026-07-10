# calc — build brief

**Goal.** Build `calc`: a small **standard-library-only** Python (3.9+) CLI that evaluates arithmetic
expressions — `calc "2 + 3 * 4"` → `14`, `calc "(2 + 3) * 4"` → `20`, `calc "sqrt(16) + pi"` → `7.14…`,
`calc "10 % 3"` → `1`. Supports `+ - * /`, parentheses, named functions, and named constants.

**This is a relay orchestration demo — delegate it, don't build it inline.** The task is small enough
that you *could* just write it yourself, but that skips the entire point: take the lead role and drive
it through relay (spawn/send/review/integrate). Don't offer to build it directly.

**Why this shape.** Unlike a pure parallel fan-out, a calculator has real *dependencies*, so it's a
**serial → parallel → serial** build — exactly the mix relay is for. The three phases:

## Phase 1 — SERIAL (foundation; each step depends on the previous)

1. **`calc/tokenize.py`** — the base everything needs. Build this first, alone.
2. **`calc/evaluate.py`** — depends on the tokenizer. Build it *after* step 1 is reviewed (reuse the
   same executor via `/relay:send`, or a fresh one).

## Phase 2 — PARALLEL (three independent extensions, built at once)

Once `evaluate` exists, these three are independent of each other and of the core — each just
provides a dict the evaluator consults. Spawn all three together:

3. **`calc/functions.py`** — `FUNCTIONS = {...}` (named functions)
4. **`calc/constants.py`** — `CONSTANTS = {...}` (named constants)
5. **`calc/ops.py`** — `EXTRA_OPS = {...}` (extra binary operators)

## Phase 3 — SERIAL (integration; the lead does this)

6. **`calc/cli.py`** — the lead wires tokenize + evaluate(FUNCTIONS, CONSTANTS, EXTRA_OPS) into the
   CLI, then runs the full `pytest`.

## Interfaces (the contract — honor exactly so the pieces compose)

```python
# calc/tokenize.py
def tokenize(expr: str) -> list:
    """Expression → list of (kind, value) tokens; kind in {"num","op","lparen","rparen","name"}.
    tokenize("2 + sqrt(16)") ==
      [("num",2.0),("op","+"),("name","sqrt"),("lparen","("),("num",16.0),("rparen",")")]
    Numbers parse as float. Operator tokens: + - * / % and the TWO-character // — longest match
    wins, so tokenize("10 // 3") yields ONE ("op","//") token, never two divisions. (% and // are
    tokenized here even though only Phase 2 wires them into evaluation — the tokenizer must accept
    them from day one or the fan-in breaks.) Raise ValueError on any other character."""

# calc/evaluate.py
def evaluate(tokens, funcs=None, consts=None, extra_ops=None) -> float:
    """Tokens → number, with correct precedence and parentheses (shunting-yard or recursive descent),
    for `+ - * /`. funcs={name: callable}, consts={name: number}, extra_ops={symbol: callable(a,b)},
    all optional (default empty). A `name` token is a constant (in consts) or a function call
    `name( … )` (in funcs). Raise ValueError on malformed input or an unknown name/op.
    evaluate(tokenize("2 + 3 * 4")) == 14.0 ; evaluate(tokenize("(2+3)*4")) == 20.0"""

# calc/functions.py   (parallel)  — FUNCTIONS: dict[str, callable]; include at least sqrt, abs, round, min, max
# calc/constants.py   (parallel)  — CONSTANTS: dict[str, float]; include at least pi, e, tau
# calc/ops.py         (parallel)  — EXTRA_OPS: dict[str, callable]; include at least "%" (mod), "//" (floordiv)

# calc/cli.py   (LEAD integrates)
def main(argv: list) -> int:
    """`calc "<expr>"` — tokenize + evaluate with FUNCTIONS/CONSTANTS/EXTRA_OPS, print the result,
    return 0. `python -m calc "2 + 3 * 4"` prints 14. The lead also writes the two-line
    calc/__main__.py (and an empty calc/__init__.py if needed) so `python -m calc` works."""
```

## Acceptance

- **Standard library only** (`math`, `operator`, `re` are fine).
- Each module ships its own tests (`tests/test_<name>.py`).
- `python -m pytest` is green (unit tests + the integrated CLI).
- These all hold: `2 + 3 * 4`→14, `(2 + 3) * 4`→20, `sqrt(16)`→4.0, `pi`→~3.14159, `10 % 3`→1.

## Per-executor rules

Stdlib only; touch only your module + its test; your tests pass before you report; stage (don't
commit) for the lead to review and integrate.
