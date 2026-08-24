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
    rows = session.execute(_SEARCH, {"v": literal, "model": model, "k": int(k)}).all()
    return [(int(card_id), float(score)) for card_id, score in rows]


def count(session, model: str | None = None) -> int:
    """How many reference images are encoded, for the status line."""
    if model is None:
        return int(session.execute(text("SELECT count(*) FROM card_embeddings")).scalar_one())
    return int(
        session.execute(
            text("SELECT count(*) FROM card_embeddings WHERE model = :model"),
            {"model": model},
        ).scalar_one()
    )


_BY_NAME = text(
    """
    SELECT c.id, c.name, c.game, c.set_name, c.number, c.variant, c.market
      FROM cards c
     WHERE c.name ILIKE :q
       AND (:game = '' OR c.game = :game)
     ORDER BY (lower(c.name) = lower(:exact)) DESC,
              (lower(c.name) LIKE lower(:prefix)) DESC,
              length(c.name),
              c.name,
              c.set_name
     LIMIT :k
    """
)


def by_name(session, q: str, *, game: str = "", k: int = 24) -> list[dict]:
    """Find catalogue cards by name, for correcting a bad match.

    The ordering is the whole value of this function. A substring search for
    "goku" against a real catalogue returns hundreds of rows, and the one the
    seller wants is almost never the longest. Exact matches come first, then
    prefix matches, then shortest name — which puts `Son Goku` above
    `Son Goku Judge Pack Store Judge` without either being special-cased.

    Names are matched, not sets or numbers: the seller is holding the card and
    reading the name off it. Narrowing by game is the one filter that pays,
    because the failure this exists to fix is a Dragon Ball scan that matched
    a Magic card.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []
    rows = session.execute(
        _BY_NAME,
        {
            "q": f"%{q}%",
            "exact": q,
            "prefix": f"{q}%",
            "game": game or "",
            "k": int(k),
        },
    ).all()
    return [
        {
            "card_id": int(r.id),
            "name": r.name,
            "game": r.game,
            "set_name": r.set_name or "",
            "number": r.number or "",
            "variant": r.variant or "",
            "market": float(r.market) if r.market is not None else None,
        }
        for r in rows
    ]


def games(session) -> list[str]:
    """Every game with cards in this catalogue.

    Here rather than in a route module because two of them need it — the match
    panel and the inventory panel both offer it as a search filter — and a
    catalogue query is what this module is.
    """
    return [
        g for (g,) in session.execute(text("SELECT DISTINCT game FROM cards ORDER BY game")).all()
    ]
