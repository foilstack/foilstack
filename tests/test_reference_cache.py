"""The catalogue reference cache, and the row-id collision it used to have.

`tests/test_images.py` has the same argument about display copies. Both caches
live in a data directory that is deliberately shared with a throwaway database,
so neither may be keyed on anything that only means something inside one.
"""

from foilstack.web.routes import media

CHOMP = "https://tcgplayer-cdn.tcgplayer.com/product/525357_200w.jpg"
ANIM_PAKAL = "https://tcgplayer-cdn.tcgplayer.com/product/526135_200w.jpg"


def test_two_databases_sharing_refs_do_not_collide():
    """The bug this keying replaced.

    Reference art was cached as `{card_id}-lg.img`. `scripts/preview.py`
    symlinks a preview's `refs/` at the real one, and `_widen_catalogue`
    reassigns card ids from 1 — so preview card 582 was Anim Pakal while this
    install's card 582 is Triumphant Chomp, and browsing the preview wrote the
    first into the cache slot the second reads. 264 of 1177 cached images were
    serving another card's art under the right name.

    Two installs disagreeing about which row is 582 is now simply not
    expressible: what is cached is an image, and the key is the image.
    """
    assert media._cache_key(CHOMP) != media._cache_key(ANIM_PAKAL)


def test_the_same_image_is_cached_once_whatever_row_points_at_it():
    """The other half. Keying per card would re-download one image per row that
    names it, which is the cost the shared cache exists to avoid."""
    assert media._cache_key(CHOMP) == media._cache_key(CHOMP)


def test_the_key_is_filename_safe():
    """It is concatenated into a path. A key carrying `/` from the URL would
    write outside `refs/`, and one carrying `?` or `:` would be unportable."""
    key = media._cache_key(CHOMP)
    assert key.isalnum() and key.isascii()


def test_repointing_a_card_does_not_serve_the_old_art():
    """`ingest` refreshes `image_url` on update. Keyed by card id, a product
    whose art upstream replaced stayed pinned to the old file forever; there is
    no invalidation step anywhere and a cache hit skips the fetch entirely."""
    before = media._cache_key(CHOMP)
    after = media._cache_key(CHOMP.replace("525357", "999999"))
    assert before != after
