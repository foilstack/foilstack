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

# How hard the index looks before answering. Measured on 187 real scans against
# a 144k-vector catalogue, not guessed — an earlier 100 here was picked on the
# reasoning that pgvector's default of 40 is tuned for a corpus larger than a
# self-hosted catalogue, which got the direction right and the size wrong.
#
#   ef     true best in top 25     ms/query
#   40           173/187              1.9
#   100          178/187              2.4
#   400          184/187              3.7
#   800          187/187              4.5
#
# Nine scans in 187 did not have their nearest neighbour ranked low, they had
# it missing: a Dragon Ball card whose own printing sits at 0.83 came back as a
# Magic card at 0.73, with nothing from its set anywhere in fifty rows. Note
# that recall@1 and recall@25 fail together at every width — when the graph
# misses, the card is unreachable rather than deep, so asking for more rows is
# not the lever and `importing.COHORT_CANDIDATE_COUNT` cannot rescue this.
#
# Two milliseconds, against an encoder pass measured in hundreds of them. The
# miss it buys off is the expensive kind: the batch pass finds nothing of its
# own set among the candidates and strands the row on another game's card, so
# the seller sees a confident wrong answer rather than a weak right one. 1000
# is the most pgvector accepts, and the margin below it is for a catalogue that
# is still growing — recall falls as vectors are added, so this is a number to
# re-measure after a few more games are ingested, not one to set and forget.
EF_SEARCH = 800

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


_WITHIN = text(
    """
    WITH pool AS MATERIALIZED (
        SELECT e.card_id,
               e.embedding <=> CAST(:v AS halfvec) AS dist
          FROM card_embeddings e
          JOIN cards c ON c.id = e.card_id
         WHERE e.model = :model
           AND c.game = :game
           AND (:set_name = '' OR c.set_name = :set_name)
    )
    SELECT card_id, 1 - dist AS score
      FROM pool
     ORDER BY dist
     LIMIT :k
    """
)


def search_within(
    session,
    vector,
    model: str,
    *,
    game: str,
    set_name: str = "",
    k: int = 5,
) -> list[tuple[int, float]]:
    """The nearest references *inside one game or set*, exactly.

    `search` answers "what does this look like"; this answers "what in this set
    does this look like", and they are different questions. A card can be
    absent from the global top fifty and still be the best answer in its own
    set — that is not an index fault but arithmetic, because the set is a
    thousandth of the catalogue and the other 999 parts get to crowd it out.
    So the batch pass cannot be served by reading further down one global
    ranking, however far down it reads. It has to ask a narrower question.

    Two things about the SQL are load-bearing, and both look like noise:

    * **The distance is computed inside the CTE**, so the outer `ORDER BY`
      sorts a plain float column. Written the obvious way — filter in the
      `WHERE`, order by the vector operator — the planner is free to drive the
      query from the HNSW index and apply the filter afterwards, and on a
      low-selectivity filter it does. Measured: ordering by `<=>` with
      `game = 'magic'` walked forty index rows, found none of them Magic, and
      returned **nothing at all**. Silently empty is the worst possible answer
      here, because the caller reads it as "this set has no such card".
    * **`MATERIALIZED`** keeps the planner from inlining the CTE and arriving
      back at that same plan.

    Between them the index is never consulted and the answer is exact, which is
    the point: this is the fallback for when approximate search has already let
    a scan down. Cost is linear in the size of the cohort rather than in the
    catalogue — 3ms for a set of 161, 40ms for a 4,000-card game, 410ms for a
    113,000-card one. Only scans that need re-pointing pay it.
    """
    if vector is None or len(vector) == 0:
        return []
    rows = session.execute(
        _WITHIN,
        {
            "v": as_literal(vector),
            "model": model,
            "game": game,
            "set_name": set_name or "",
            "k": int(k),
        },
    ).all()
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
