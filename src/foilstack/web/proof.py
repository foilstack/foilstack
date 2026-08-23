"""The two cards the landing page argues with.

They are looked up rather than hardcoded, and that is the point. A card id is a
row number in whichever database this instance happens to have built, so the
`37` that means Base Set Charizard here means nothing on a fresh clone. The page
finds them by name and set, and shows no thumbnails at all when the catalogue
has not been ingested — which is the honest state for an install that cannot yet
identify anything.

Nothing here ships an image. The reference art is fetched by this instance, from
upstream, on the machine running it — the same path every other card takes, and
the reason the project can say it redistributes no card data.
"""

from __future__ import annotations

from sqlalchemy import select

from foilstack import db

# Name and set of each row in the table, in the order they appear. The prices
# beside them in the template are the catalogue's own, so these two have to be
# the cards those numbers came from.
PROOF_CARDS: tuple[tuple[str, str], ...] = (
    ("Charizard", "Base Set"),
    ("Charizard", "Base Set (Shadowless)"),
)


def proof_card_ids(session) -> list[int | None]:
    """The catalogue id for each proof card, or None where it is not ingested."""
    ids: list[int | None] = []
    for name, set_name in PROOF_CARDS:
        ids.append(
            session.scalar(
                select(db.Card.id)
                .where(db.Card.name == name, db.Card.set_name == set_name)
                .where(db.Card.image_url.is_not(None))
                .order_by(db.Card.id)
                .limit(1)
            )
        )
    return ids


def is_proof_card(session, card_id: int) -> bool:
    """Whether this id is one the landing page is allowed to show anonymously.

    The catalogue is public data, but the route that serves it fetches from
    upstream on a miss and caches to disk. Opening that to anyone would make a
    stranger able to walk a hundred thousand ids and have this server pull every
    one of them from somebody else's CDN. Two cards is not that.
    """
    return card_id in {i for i in proof_card_ids(session) if i is not None}
