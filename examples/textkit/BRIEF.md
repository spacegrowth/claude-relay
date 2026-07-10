# textkit — build brief

**Goal.** Build `textkit`: a tiny **standard-library-only** Python (3.9+) CLI with three subcommands
— `slug`, `count`, `palette` — each its own module, wired into a `cli` dispatcher, each with tests.

This is a relay **orchestration demo — delegate it, don't build it inline** (that skips the point).
The three subcommands are **independent**, so they're a natural parallel fan-out: **one executor per
module, built at the same time**, then the **lead integrates** the dispatcher (`cli.py`) and runs the
suite. That decomposition is the lead's to propose — this brief
gives it the goal, the interfaces the three modules must honor so they compose, and the acceptance
criteria.

## Interfaces (the contract — parallel modules must honor these exactly, or the fan-in won't compose)

```python
# textkit/slug.py
def slug(text: str) -> str:
    """URL slug: lowercase, ASCII, non-alphanumeric runs collapsed to a single '-', trimmed.
    slug("Hello, World!")        == "hello-world"
    slug("  Rock & Roll --  ")   == "rock-roll"
    slug("Café del Mar")         == "cafe-del-mar"   # strip accents to ASCII
    slug("")                     == ""
    """

# textkit/count.py
def count(text: str) -> dict:
    """Text stats. Keys EXACTLY: "chars", "words", "lines", "avg_word_len".
    - chars: len(text)
    - words: number of whitespace-separated non-empty tokens
    - lines: number of lines (0 for "", else text.count("\\n") + 1)
    - avg_word_len: mean word length, float rounded to 2 (0.0 when there are no words)
    count("hi there\\nyou") == {"chars": 12, "words": 3, "lines": 2, "avg_word_len": 3.33}
    # words hi(2) there(5) you(3) -> mean 10/3 = 3.33
    """

# textkit/palette.py
def palette(seed: str, n: int = 5) -> list[str]:
    """Deterministic list of n "#rrggbb" hex colors derived from `seed` (same seed → same palette).
    Use hashlib (e.g. sha256 of f"{seed}:{i}") so it's stable across runs and machines.
    len(palette("x", 3)) == 3; every item matches ^#[0-9a-f]{6}$; palette("x") == palette("x").
    """

# textkit/cli.py   (the LEAD builds this — the fan-in)
def main(argv: list[str]) -> int:
    """argparse dispatcher. Subcommands: slug <text> | count <text> | palette <seed> [--n N].
    Prints the result to stdout (slug→the string; count→the dict; palette→one hex per line).
    Returns 0 on success. `python -m textkit slug "Hello World"` prints "hello-world" — the lead
    also writes the two-line textkit/__main__.py (and an empty textkit/__init__.py if needed) so
    `python -m textkit` works."""
```

## Acceptance

- **Standard library only** — no third-party packages.
- Each module ships its **own tests** (`tests/test_slug.py`, `tests/test_count.py`,
  `tests/test_palette.py`) covering the docstring examples plus edge cases (empty input, etc.).
- The lead's `cli.py` composes the three; `python -m pytest` is **green** (module tests + the CLI).
- `python -m textkit slug "Hello, World!"` prints `hello-world`; `python -m textkit palette sunset
  --n 3` prints three `#rrggbb` lines.

## For each executor the lead delegates to

- Stdlib only; **touch only your module + its test file**; don't edit `cli.py` or another module.
- Your module's tests must pass on their own before you report.
- Stage/leave your work for the lead to review and integrate — don't commit.
