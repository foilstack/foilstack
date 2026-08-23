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
        name="t", label="T", description="", filename="t.csv",
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
        name="t", label="T", description="", filename="t.csv",
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
