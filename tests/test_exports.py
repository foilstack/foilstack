"""Exporters are data, so the thing worth testing is that the data is obeyed."""

from pathlib import Path

import pytest

from foilstack.plugins import export_plugins
from foilstack.plugins.exports import ExportColumn, ExportSpec, load_export_specs

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_exporters_load():
    specs = export_plugins()
    assert {"tcgplayer", "ebay"} <= set(specs)


def test_render_respects_column_order_and_transforms():
    spec = ExportSpec(
        name="t",
        label="T",
        description="",
        filename="t.csv",
        columns=[
            ExportColumn(header="Name", field="name"),
            ExportColumn(header="Price", field="price", transform="money2"),
            ExportColumn(header="Qty", field="qty", transform="int"),
            ExportColumn(header="Action", const="Add"),
        ],
    )
    out = spec.render([{"name": "Pikachu", "price": 3.5, "qty": 2}])
    assert out.splitlines()[0] == "Name,Price,Qty,Action"
    assert out.splitlines()[1] == "Pikachu,3.50,2,Add"


def test_missing_field_renders_empty_not_none():
    """A literal 'None' in a marketplace upload becomes a listing titled None."""
    spec = ExportSpec(
        name="t",
        label="T",
        description="",
        filename="t.csv",
        columns=[ExportColumn(header="Cost", field="cost", transform="money2")],
    )
    # csv quotes a lone empty field as `""` to distinguish it from a blank
    # row. Either spelling is fine; what must never appear is the string None.
    out = spec.render([{}])
    assert out.splitlines()[1] in ("", '""')
    assert "None" not in out


def test_unknown_transform_is_rejected_at_load(tmp_path):
    (tmp_path / "bad.toml").write_text(
        'name = "bad"\n[[columns]]\nheader = "X"\nfield = "x"\ntransform = "exec"\n'
    )
    with pytest.raises(ValueError, match="unknown transform"):
        load_export_specs(tmp_path)


# TCGplayer's uploader validates the header row against the one its own export
# writes, and answers "Headers are not valid!" to anything else — not to a
# wrong value in a row, and not with a hint about which column is at fault. So
# the header row is pinned here, character for character, from a real export.
TCGPLAYER_HEADER = (
    "TCGplayer Id,Product Line,Set Name,Product Name,Title,Number,Rarity,Condition,"
    "TCG Market Price,TCG Direct Low,TCG Low Price With Shipping,TCG Low Price,"
    "Total Quantity,Add to Quantity,TCG Marketplace Price,Photo URL"
)


def test_tcgplayer_header_row_is_exactly_what_the_uploader_expects():
    out = export_plugins()["tcgplayer"].render([])
    assert out.splitlines()[0] == TCGPLAYER_HEADER


def test_tcgplayer_row_speaks_tcgplayers_vocabulary():
    """Condition carries the printing, quantity adds rather than sets."""
    out = export_plugins()["tcgplayer"].render(
        [
            {
                "tcg_product_line": "Magic",
                "set_name": "10th Edition",
                "name": "Abundance",
                "number": "249",
                "tcg_condition": "Near Mint Foil",
                "market": 1.68,
                "low": 1.41,
                "quantity": 2,
                "list_price": 1.6,
            }
        ]
    )
    assert out.splitlines()[1] == (
        ",Magic,10th Edition,Abundance,,249,,Near Mint Foil,1.68,,,1.41,,2,1.60,"
    )


def test_tcgplayer_id_is_left_blank_rather_than_filled_with_a_product_id():
    """A product id in that column is a SKU id for some unrelated card.

    The two spaces overlap — 10th Edition Abundance is product 15023 and SKU
    4519 — so a wrong id here does not fail the upload, it edits the wrong
    listing.
    """
    out = export_plugins()["tcgplayer"].render([{"source_ref": "15023", "quantity": 1}])
    assert out.splitlines()[1].split(",")[0] == ""
