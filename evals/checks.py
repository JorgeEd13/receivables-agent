"""Property checks for agent answers — pure, offline-testable.

We don't string-match the whole answer (an LLM phrases the same fact a hundred
ways). Instead each golden case asserts *properties* of a good answer:

- it called the right tool(s)            -> `used_tool`
- it cites the governing rule's keywords -> `mentions_all`
- it states the right number             -> `contains_number` (tolerant, format-agnostic)

These functions have no LLM/DB dependency, so they're unit-tested directly
(`tests/test_evals.py`); the live runner composes them per case.
"""

from __future__ import annotations

import re

# Matches integers/decimals with optional thousands separators: 13,000 / 77.7 / 180
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def used_tool(tools_used: list[str], name: str) -> bool:
    """Whether the agent invoked the tool `name` this turn."""
    return name in tools_used


def mentions_all(text: str, terms: list[str]) -> bool:
    """Whether every term appears in `text` (case-insensitive substring)."""
    low = text.lower()
    return all(term.lower() in low for term in terms)


def extract_numbers(text: str) -> list[float]:
    """All numbers in `text` as floats, tolerating thousands separators."""
    out: list[float] = []
    for token in _NUMBER_RE.findall(text):
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return out


def contains_number(text: str, expected: float, tol: float = 0.0) -> bool:
    """Whether any number in `text` equals `expected` within absolute `tol`."""
    return any(abs(n - expected) <= tol for n in extract_numbers(text))
