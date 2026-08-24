"""The vector search SQL.

There is no Postgres in the test environment, so what is checked here is the
part that was wrong twice while this was a numpy index: the direction of the
score, and the model filter.
"""

import pytest

from foilstack import search


def test_score_is_similarity_not_distance():
    """`auto_accept` is 0.94 and candidates render as percentages.

    pgvector's `<=>` is cosine *distance*, where 0 is identical. Every caller
    in this codebase expects 1 to be identical, so the conversion happens in
    the query. Getting this backwards ranks the worst match first and
    auto-accepts nothing, which reads as "the encoder is broken".
    """
    sql = str(search._SEARCH)
    assert "1 - (e.embedding <=> CAST(:v AS halfvec))" in sql
    assert "AS score" in sql


def test_search_is_ordered_by_distance_ascending():
    """Ordering by the similarity alias would need DESC; ordering by the
    distance expression is what lets the HNSW index answer the query at all."""
    sql = str(search._SEARCH)
    assert "ORDER BY e.embedding <=> CAST(:v AS halfvec)" in sql
    assert "ORDER BY score" not in sql


def test_model_is_always_filtered():
    """Vectors from two encoders occupy unrelated spaces. A mixed table without
    this filter returns a confident ranking of nothing in particular."""
    assert "WHERE e.model = :model" in str(search._SEARCH)


def test_empty_vector_short_circuits():
    """No session touched, so this would raise if it did not return early."""
    assert search.search(None, [], "some-model") == []


def test_vector_literal_is_plain_floats():
    """numpy scalars must not reach Postgres.

    Under NumPy 2, `str(list(array))` renders `np.float32(-0.021)` per element.
    That is valid Python and meaningless to Postgres, and the error it raises
    is a thousand characters of parameter dump that names everything except
    the cause. Caught in production only because a search was tried by hand.
    """
    np = pytest.importorskip("numpy")
    literal = search.as_literal(np.array([-0.25, 0.5], dtype=np.float32))
    assert "np.float32" not in literal
    assert literal.startswith("[") and literal.endswith("]")
    assert literal == "[-0.25,0.5]"


def test_vector_literal_accepts_a_plain_list():
    assert search.as_literal([1.0, -2.5]) == "[1.0,-2.5]"


# ---------------------------------------------------------------------------
# Catalogue search by name — the way out of a bad match.
# ---------------------------------------------------------------------------


def test_by_name_ignores_a_query_too_short_to_mean_anything():
    """One character matches most of the catalogue, so it is not a search.

    Guarded here rather than in the browser because the endpoint is reachable
    without it, and `%a%` over 150k rows is a table scan per keystroke.
    """
    assert search.by_name(None, "a") == []
    assert search.by_name(None, " ") == []
    assert search.by_name(None, "") == []


def test_by_name_ranks_exact_then_prefix_then_shortest():
    """The whole value of this function is the ordering.

    A substring search for "goku" against a real catalogue returns hundreds of
    rows, and the one in the seller's hand is never the longest. Without this
    clause `Son Goku` sorts below `Son Goku Judge Pack Store Judge` and the
    picker is useless exactly when it is needed.
    """
    sql = str(search._BY_NAME)
    order = sql.index("ORDER BY")
    assert sql.index("lower(c.name) = lower(:exact)") > order
    assert sql.index("lower(c.name) LIKE lower(:prefix)") > order
    assert sql.index("length(c.name)") > order
    assert sql.index("lower(c.name) = lower(:exact)") < sql.index("length(c.name)")


def test_by_name_can_narrow_to_one_game():
    """The failure this exists to fix is a Dragon Ball scan matching a Magic
    card, so filtering by game is the filter that pays."""
    sql = str(search._BY_NAME)
    assert ":game = ''" in sql and "c.game = :game" in sql
