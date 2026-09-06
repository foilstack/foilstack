"""One scan of one card must get one finish, whichever path handles it.

A scan reaches inventory two ways: `importing._accept` puts it there with
nobody looking, and the review queue renders a finish for a person to confirm.
Both ask `inventory.resolve_finish` the same question, and for a while they
handed it different evidence — `_accept` passed the raw price map and the queue
passed one with the unpriced printings stripped out. So a foil batch containing
a card whose only foil printing carries no market price auto-accepted as a foil
and was confirmed by hand as a non-foil. Same photograph, same match, same
batch default, two different rows, two different prices, two different
`Condition` columns in the upload file. Nothing raised and nothing on either
screen said which had happened.

Against the live catalogue that was 2,736 of 142,202 cards.

The queue's evidence is now derived from the server's rather than filtered
again beside it, and these are the crossing: the auto path against the map the
queue and the browser actually read.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from foilstack import inventory
from foilstack.importing import _accept
from foilstack.web.routes.scans import _price_map


def _row(card_id, sub_type, market):
    return SimpleNamespace(card_id=card_id, sub_type=sub_type, market=market, low=None)


class _Session:
    """Enough of a session for `_prices_for` and one insert."""

    def __init__(self, prices):
        self._prices = prices
        self.added = []

    def scalars(self, _stmt):
        return SimpleNamespace(all=lambda: list(self._prices))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


# Every shape where "the catalogue has this printing" and "the catalogue prices
# this printing" can come apart, with the finish both paths owe the seller.
CARDS = [
    # The divergence itself: a foil printing nothing will pay for.
    ([("Normal", 2.10), ("Holofoil", None)], "foil", "nonfoil"),
    ([("Normal", 2.10), ("Holofoil", None)], "nonfoil", "nonfoil"),
    # The mirror, which is rarer and just as wrong.
    ([("Normal", None), ("Holofoil", 30.00)], "nonfoil", "foil"),
    # Priced on both sides: the seller's answer stands, as it always did.
    ([("Normal", 2.10), ("Holofoil", 30.00)], "foil", "foil"),
    ([("Normal", 2.10), ("Holofoil", 30.00)], "nonfoil", "nonfoil"),
    # Priced on one side only, which is the rule's original job.
    ([("Holofoil", 30.00)], "nonfoil", "foil"),
    ([("Normal", 2.10)], "foil", "nonfoil"),
    # Nothing priced at all is not evidence of anything, so the batch default
    # survives — a catalogue naming two printings and pricing neither is no
    # more informative than one naming none.
    ([("Normal", None), ("Holofoil", None)], "foil", "foil"),
    ([], "foil", "foil"),
    ([], "nonfoil", "nonfoil"),
]


@pytest.mark.parametrize(("printings", "default", "expected"), CARDS)
def test_auto_accept_and_the_review_queue_reach_the_same_finish(printings, default, expected):
    """The crossing. Neither number here is interesting on its own."""
    rows = [_row(7, sub, market) for sub, market in printings]

    session = _Session(rows)
    scan = SimpleNamespace(id=1, status="matched", auto_accepted=0, job_id=1)
    _accept(
        session, scan, 7, SimpleNamespace(default_condition="NM", default_finish=default, user_id=3)
    )
    (item,) = session.added
    assert item.finish == expected

    # What the queue renders, off the same map: `_queue_rows` resolves over the
    # printings `_prices_for` returned, which is what `_accept` was given.
    by_sub = {row.sub_type: row for row in rows}
    assert inventory.resolve_finish(default, by_sub) == expected


@pytest.mark.parametrize(("printings", "default", "expected"), CARDS)
def test_the_browsers_map_cannot_imply_a_different_finish(printings, default, expected):
    """And the third copy of the rule, which is JavaScript.

    `import.html` re-resolves a row the seller re-points after load, over
    `data-prices` — the map `_price_map` builds. If that map disagreed with the
    server about which printings exist, correcting a match would move a row
    somewhere a reload puts back. It cannot now, because `_price_map` is
    `priced_printings` rather than the same condition written again beside it.
    """
    by_sub = {sub: _row(7, sub, market) for sub, market in printings}
    shipped = _price_map(by_sub)

    assert set(shipped) == set(inventory.priced_printings(by_sub))
    # The browser's `resolveFinish`, which sees prices and never a null.
    assert all(market is not None for market in shipped.values())
    assert inventory.resolve_finish(default, {k: _row(7, k, v) for k, v in shipped.items()}) == (
        expected
    )
