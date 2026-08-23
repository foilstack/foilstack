"""Display copies. The original is evidence and must survive untouched."""

from pathlib import Path

import pytest

from foilstack import images

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _photo(path: Path, size=(1458, 2016)) -> Path:
    Image.new("RGB", size, (120, 90, 60)).save(path, "JPEG", quality=95)
    return path


def test_display_copy_is_smaller_than_the_original(tmp_path):
    source = _photo(tmp_path / "X018.jpg")
    out = images.make_display_copy(source, tmp_path / "display", "1/X018.jpg")
    assert out is not None
    assert out.stat().st_size < source.stat().st_size


def test_display_copy_fits_the_long_edge(tmp_path):
    source = _photo(tmp_path / "big.jpg", (4000, 3000))
    out = images.make_display_copy(source, tmp_path / "display", "1/big.jpg")
    with Image.open(out) as im:
        assert max(im.size) == images.MAX_EDGE


def test_a_small_scan_is_not_enlarged(tmp_path):
    """`thumbnail` only ever shrinks. Upscaling would invent detail on exactly
    the images where detail is what the reviewer is looking for."""
    source = _photo(tmp_path / "small.jpg", (300, 420))
    out = images.make_display_copy(source, tmp_path / "display", "1/small.jpg")
    with Image.open(out) as im:
        assert im.size == (300, 420)


def test_the_original_is_never_modified(tmp_path):
    source = _photo(tmp_path / "keep.jpg")
    before = source.read_bytes()
    images.make_display_copy(source, tmp_path / "display", "1/keep.jpg")
    assert source.read_bytes() == before


def test_an_unreadable_file_returns_none_rather_than_raising(tmp_path):
    """A scan that cannot be resized is still a scan that matched."""
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"this is not an image")
    assert images.make_display_copy(broken, tmp_path / "display", "1/broken.jpg") is None


def test_an_existing_copy_is_reused(tmp_path):
    source = _photo(tmp_path / "again.jpg")
    first = images.make_display_copy(source, tmp_path / "display", "1/again.jpg")
    stamp = first.stat().st_mtime_ns
    second = images.make_display_copy(source, tmp_path / "display", "1/again.jpg")
    assert second == first and second.stat().st_mtime_ns == stamp


def test_two_databases_sharing_a_data_dir_do_not_collide(tmp_path):
    """The bug this keying replaced.

    Display copies were named by the scan's row id, which is unique inside one
    database and nowhere else. A second database over the same data directory —
    a preview, a restore, a staging copy — served its scan 7 from the cached
    copy of the other's scan 7: the wrong photograph under the right card name.
    """
    display = tmp_path / "display"
    a = _photo(tmp_path / "a.jpg", (400, 560))
    b = _photo(tmp_path / "b.jpg", (401, 561))

    first = images.make_display_copy(a, display, "1/card.jpg")
    second = images.make_display_copy(b, display, "2/card.jpg")

    assert first != second
    with Image.open(first) as im:
        assert im.size == (400, 560)
    with Image.open(second) as im:
        assert im.size == (401, 561)
