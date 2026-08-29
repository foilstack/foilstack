"""The extractor's job is to refuse hostile archives, not just to unpack."""

import zipfile

import pytest

from foilstack.importing import ImportError_, extract_archive


def _zip(tmp_path, entries):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_extracts_images_and_ignores_other_files(tmp_path):
    archive = _zip(tmp_path, {"a.jpg": b"x", "notes.txt": b"y", "b.PNG": b"z"})
    out = extract_archive(archive, tmp_path / "out")
    assert sorted(p.name for p in out) == ["a.jpg", "b.PNG"]


def test_keeps_the_archives_own_order(tmp_path):
    """The queue shows scans in the order they were imported, and this is where
    that order is decided.

    Deliberately not alphabetical, and deliberately not reverse-alphabetical
    either: a sort in here would have to actively scramble these to pass, and
    the previous implementation sorted.
    """
    names = ["c.jpg", "a.jpg", "b.jpg"]
    archive = _zip(tmp_path, dict.fromkeys(names, b"x"))
    out = extract_archive(archive, tmp_path / "out")
    assert [p.name for p in out] == names


def test_refuses_path_traversal(tmp_path):
    """Zip-slip: an entry named ../../evil.jpg must not land outside the target."""
    archive = _zip(tmp_path, {"../../evil.jpg": b"x"})
    with pytest.raises(ImportError_, match="unsafe archive entry"):
        extract_archive(archive, tmp_path / "out")


def test_duplicate_filenames_do_not_overwrite(tmp_path):
    """Two folders in one archive routinely both contain `IMG_0001.jpg`."""
    archive = _zip(tmp_path, {"one/IMG.jpg": b"a", "two/IMG.jpg": b"b"})
    out = extract_archive(archive, tmp_path / "out")
    assert len(out) == 2
    assert len({p.read_bytes() for p in out}) == 2


class _FakeCard:
    def __init__(self, name, number, variant, market=None):
        self.name, self.number, self.variant = name, number, variant
        self.market = market


class _FakeSession:
    def __init__(self, cards):
        self._cards = cards

    def get(self, _model, card_id):
        return self._cards.get(card_id)


class _S:
    auto_accept = 0.94
    auto_accept_margin = 0.04


def test_same_card_different_printing_with_a_real_price_gap_goes_to_review():
    """The case a photograph cannot settle: same art, same number, 30x price.

    Measured on Base Set — a clean Machop scan scores 1.000 against Normal and
    0.946 against 1st Edition, which is $0.71 against $21.12.
    """
    from foilstack.importing import _may_auto_accept

    session = _FakeSession(
        {
            1: _PricedCard("Machop", "052/102", "Normal", 0.71),
            2: _PricedCard("Machop", "052/102", "1st Edition", 21.12),
        }
    )
    assert _may_auto_accept(session, [(1, 1.000), (2, 0.946)], _S()) is False


def test_close_runner_up_is_not_auto_accepted_even_when_a_different_card():
    from foilstack.importing import _may_auto_accept

    session = _FakeSession(
        {
            1: _FakeCard("Charizard", "004/102", "Holofoil"),
            2: _FakeCard("Charizard Black Dot Error", "004/102", "Holofoil"),
        }
    )
    assert _may_auto_accept(session, [(1, 0.952), (2, 0.947)], _S()) is False


def test_clear_winner_is_auto_accepted():
    from foilstack.importing import _may_auto_accept

    session = _FakeSession(
        {
            1: _FakeCard("Gyarados", "006/102", "Holofoil"),
            2: _FakeCard("Squirtle", "063/102", "Normal"),
        }
    )
    assert _may_auto_accept(session, [(1, 0.990), (2, 0.800)], _S()) is True


def test_below_threshold_is_never_auto_accepted():
    from foilstack.importing import _may_auto_accept

    session = _FakeSession({1: _FakeCard("A", "1", "Normal")})
    assert _may_auto_accept(session, [(1, 0.90)], _S()) is False


class _PricedCard(_FakeCard):
    def __init__(self, name, number, variant, market):
        super().__init__(name, number, variant)
        self.market = market


def test_ambiguous_printing_with_similar_prices_still_auto_accepts():
    """Two printings worth the same money: picking either costs nothing, and
    blocking them would send a dealer's whole archive to review for no gain."""
    from foilstack.importing import _may_auto_accept

    session = _FakeSession(
        {
            1: _PricedCard("Rattata", "061/102", "Normal", 0.40),
            2: _PricedCard("Rattata", "061/102", "1st Edition", 0.45),
        }
    )
    assert _may_auto_accept(session, [(1, 0.995), (2, 0.930)], _S()) is True


def test_ambiguous_printing_with_a_large_price_gap_goes_to_review():
    from foilstack.importing import _may_auto_accept

    session = _FakeSession(
        {
            1: _PricedCard("Charizard", "004/102", "Holofoil", 855.52),
            2: _PricedCard("Charizard", "004/102", "1st Edition Holofoil", 10000.0),
        }
    )
    assert _may_auto_accept(session, [(1, 0.999), (2, 0.930)], _S()) is False


def test_unknown_price_on_a_rival_printing_goes_to_review():
    """We cannot show the gap is safe, so we do not assume it is."""
    from foilstack.importing import _may_auto_accept

    session = _FakeSession(
        {
            1: _PricedCard("Machop", "052/102", "Normal", 0.71),
            2: _PricedCard("Machop", "052/102", "1st Edition", None),
        }
    )
    assert _may_auto_accept(session, [(1, 0.999), (2, 0.930)], _S()) is False


def test_image_count_is_capped(tmp_path, monkeypatch):
    """An unbounded archive is an unbounded amount of encoder time."""
    import foilstack.importing as imp

    monkeypatch.setattr(imp, "MAX_IMAGES", 3)
    archive = _zip(tmp_path, {f"c{i}.jpg": b"x" for i in range(10)})
    assert len(imp.extract_archive(archive, tmp_path / "out")) == 3


def test_scan_path_resolves_relative_to_the_scans_dir(tmp_path):
    """A scan is found through the current scans directory, not a stored absolute."""
    from foilstack.importing import scan_path

    scans = tmp_path / "scans"
    (scans / "1").mkdir(parents=True)
    image = scans / "1" / "card.jpg"
    image.write_bytes(b"x")

    assert scan_path("1/card.jpg", scans) == image.resolve()


def test_scan_path_rehomes_a_legacy_absolute_path(tmp_path):
    """The bind-mount case: the row says /elsewhere, the file is under /data.

    This is the bug that emptied every thumbnail — an import run on the host
    recorded a host path, and the container holding the same file at a
    different mount point could not resolve it.
    """
    from foilstack.importing import scan_path

    scans = tmp_path / "data" / "scans"
    (scans / "2").mkdir(parents=True)
    image = scans / "2" / "card.jpg"
    image.write_bytes(b"x")

    stale = "/home/someone/project/data/scans/2/card.jpg"
    assert scan_path(stale, scans) == image.resolve()


def test_scan_path_refuses_to_escape_the_scans_dir(tmp_path):
    """The route turns a database value into a filesystem read."""
    from foilstack.importing import scan_path

    scans = tmp_path / "scans"
    scans.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("no")

    assert scan_path("../secret.txt", scans) is None


def test_scan_path_is_none_when_the_file_is_gone(tmp_path):
    from foilstack.importing import scan_path

    scans = tmp_path / "scans"
    scans.mkdir()
    assert scan_path("1/missing.jpg", scans) is None
