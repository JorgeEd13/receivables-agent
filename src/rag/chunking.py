"""Split the collections policy into citable chunks.

The policy doc is written to be chunkable: each ``##`` section is a
self-contained rule (see the note at the top of ``collections_policy.md``). We
chunk on those level-2 headings — one chunk per section, heading included so the
text stays self-describing and the heading can be cited.

Chunk IDs are **deterministic**: ``"{source}::{heading-slug}"``. Re-running the
indexer over the same document therefore produces the same IDs, so an upsert
overwrites in place instead of appending duplicates (the indexer relies on this;
see ``index.build_index`` and ADR-006).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A level-2 heading on its own line. ``###`` (and the ``#`` title) don't match,
# because the char after ``##`` must be whitespace.
_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class PolicyChunk:
    """One citable policy section."""

    id: str
    heading: str
    text: str
    source: str
    index: int


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def chunk_policy(text: str, source: str) -> list[PolicyChunk]:
    """Split ``text`` into one chunk per ``##`` section.

    Anything before the first ``##`` (the title and the doc's self-description)
    is intentionally dropped — it is meta, not a rule. ``source`` is a short
    label (e.g. the file name) used for citations and to namespace IDs.
    """
    headings = list(_HEADING_RE.finditer(text))
    chunks: list[PolicyChunk] = []
    for i, match in enumerate(headings):
        heading = match.group(1).strip()
        start = match.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[start:end].strip()
        chunks.append(
            PolicyChunk(
                id=f"{source}::{_slug(heading)}",
                heading=heading,
                text=body,
                source=source,
                index=i,
            )
        )
    return chunks
