import pytest

from foilstack.plugins import source_plugins
from foilstack.plugins.base import CardRecord


def test_tcgcsv_is_discovered_as_a_plugin():
    """The primary source is a plugin, not a special case. If this breaks, the
    plugin interface has stopped being able to express our own data source."""
    assert "tcgcsv" in source_plugins()


def test_card_record_requires_an_image():
    """No image, no matching — so fail when the plugin is written."""
    with pytest.raises(ValueError, match="image_url is required"):
        CardRecord(source_id="1", name="Pikachu", game="pokemon", image_url="")


def test_card_record_requires_identity():
    with pytest.raises(ValueError):
        CardRecord(source_id="", name="", game="", image_url="http://x/y.jpg")
