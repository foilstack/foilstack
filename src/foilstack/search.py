"""Nearest-neighbour search over the catalogue, in Postgres.

This replaced a numpy index held in memory. The swap was contained because the
old module had one public entry point, and so does this one.

Scores are **cosine similarity**, not distance, because that is what the rest
of the application already speaks: `auto_accept` is 0.94, candidates are shown
as percentages, and the margin rule compares two scores. pgvector's `<=>` is
cosine *distance*, so the conversion happens here, once, rather than in four
callers that would each get it right until one didn't.
"""

from __future__ import annotations

from sqlalchemy import text

# How hard the index looks before answering. The default (40) is tuned for
# recall on a corpus far larger than a typical self-hosted catalogue; the cost
# of raising it here is under a millisecond against a job that spends most of
# its time on image I/O, and the benefit is on exactly the hard scans — worn
# cards, bad lighting — that the review queue exists for.
EF_SEARCH = 100

_SEARCH = text(
    """
    SELECT e.card_id,
           1 - (e.embedding <=> CAST(:v AS halfvec)) AS score
      FROM card_embeddings e
     WHERE e.model = :model
     ORDER BY e.embedding <=> CAST(:v AS halfvec)
     LIMIT :k
    """
)


def as_literal(vector) -> str:
    """Format a vector the way Postgres wants to read it: `[0.1,-0.2,…]`.

    Every element is forced through `float()` first, and that is the whole
    point of this function. The encoder hands back a numpy array, and under
    NumPy 2 `str(list(array))` renders each element as `np.float32(-0.0210…)`
    — which is valid Python, unreadable to Postgres, and produces a
    `InvalidTextRepresentation` a thousand characters wide that names the
    parameter rather than the cause.
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def search(session, vector, model: str, k: int = 5) -> list[tuple[int, float]]:
    """Return (card_id, cosine similarity) for the k nearest references.

    Filtering by `model` is not optional. Vectors from two encoders occupy
    unrelated spaces, so a mixed table would return a confident ranking of
    nothing in particular — and a half-finished re-embed is exactly when
    someone is most likely to be scanning.
    """
    if vector is None or len(vector) == 0:
        return []
    literal = as_literal(vector)

    session.execute(text(f"SET LOCAL hnsw.ef_search = {int(EF_SEARCH)}"))
    rows = session.execute(
        _SEARCH, {"v": literal, "model": model, "k": int(k)}
    ).all()
    return [(int(card_id), float(score)) for card_id, score in rows]


def count(session, model: str | None = None) -> int:
    """How many reference images are encoded, for the status line."""
    if model is None:
        return int(session.execute(
            text("SELECT count(*) FROM card_embeddings")
        ).scalar_one())
    return int(session.execute(
        text("SELECT count(*) FROM card_embeddings WHERE model = :model"),
        {"model": model},
    ).scalar_one())
