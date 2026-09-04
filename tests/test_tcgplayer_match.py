"""The round trip through a seller's own TCGplayer pricing export.

Every fixture row here is copied from a real 800,344-row export rather than
invented, because the whole point of this module is that our idea of a card
and TCGplayer's have to agree exactly.
"""

from __future__ import annotations

import csv
import io

import pytest

from foilstack import tcgplayer

HEADER = ",".join(tcgplayer.HEADER)

# Real rows. Abundance is the worked example in the module docstring: one
# product, two printings, its own SKU id for each.
THEIRS = [
    '"4519","Magic","10th Edition","Abundance","","249","R","Near Mint","1.68","","2.9000","1.4100","","0","",""',
    '"4521","Magic","10th Edition","Abundance","","249","R","Near Mint Foil","","","","","","0","",""',
    '"376071","Magic","10th Edition","Abundance","","249","R","Lightly Played","1.60","2.69","3.0800","1.5900","","0","",""',
    # The apostrophe is the point of this one. Our `name` column holds
    # upstream's cleaned spelling, "Ancestors Chosen", which does not appear
    # anywhere in TCGplayer's file.
    '"4591","Magic","10th Edition","Ancestor\'s Chosen","","1","U","Near Mint","0.16","","1.6000","0.1100","","0","",""',
]


def _upload(lines: list[str]) -> list[bytes]:
    """The file as it arrives: arbitrary blocks, not lines.

    Deliberately cut at 64 bytes so a block boundary lands mid-row, which is
    what happens on a real upload and what `_lines` exists to survive.
    """
    body = (HEADER + "\n" + "\n".join(lines) + "\n").encode()
    return [body[i : i + 64] for i in range(0, len(body), 64)]


def _ours(**over):
    row = {
        "tcg_product_line": "Magic",
        "set_name": "10th Edition",
        "tcg_name": "Abundance",
        "number": "249",
        "tcg_condition": "Near Mint",
        "quantity": 3,
        "list_price": 1.5,
    }
    row.update(over)
    return row


def test_their_row_comes_back_with_our_quantity_and_price_and_their_id():
    body, report = tcgplayer.fill(_upload(THEIRS), [_ours()])

    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == tcgplayer.HEADER
    assert len(rows) == 2, "only the matched row belongs in the file"
    row = dict(zip(tcgplayer.HEADER, rows[1], strict=True))

    assert row["TCGplayer Id"] == "4519", "the SKU id is the whole reason for the round trip"
    assert row["Add to Quantity"] == "3"
    assert row["TCG Marketplace Price"] == "1.50"
    assert report.matched == 1


def test_the_columns_we_did_not_come_to_write_are_carried_through_untouched():
    """Rarity and the price columns are theirs, and better than ours."""
    body, _ = tcgplayer.fill(_upload(THEIRS), [_ours()])
    row = dict(zip(tcgplayer.HEADER, list(csv.reader(io.StringIO(body)))[1], strict=True))

    assert row["Rarity"] == "R"
    assert row["TCG Market Price"] == "1.68"
    assert row["TCG Low Price"] == "1.4100"


def test_condition_and_printing_pick_different_skus_of_one_card():
    """Foil and non-foil are one product and two listings."""
    body, _ = tcgplayer.fill(
        _upload(THEIRS),
        [_ours(), _ours(tcg_condition="Near Mint Foil", quantity=1, list_price=90.0)],
    )
    ids = {r[0]: r for r in list(csv.reader(io.StringIO(body)))[1:]}
    assert set(ids) == {"4519", "4521"}
    assert ids["4521"][tcgplayer.HEADER.index("TCG Marketplace Price")] == "90.00"


def test_the_join_is_on_the_raw_name_not_the_cleaned_one():
    """The bug this column was added for: `cleanName` strips punctuation.

    `cards.name` holds "Ancestors Chosen" and TCGplayer's file says
    "Ancestor's Chosen". Matching on the cleaned spelling loses every card with
    an apostrophe, colon or bracket in it — about one in ten, silently.
    """
    cleaned = _ours(tcg_name="Ancestors Chosen", number="1")
    _, report = tcgplayer.fill(_upload(THEIRS), [cleaned])
    assert report.matched == 0

    raw = _ours(tcg_name="Ancestor's Chosen", number="1")
    body, report = tcgplayer.fill(_upload(THEIRS), [raw])
    assert report.matched == 1
    assert list(csv.reader(io.StringIO(body)))[1][0] == "4591"


def test_a_card_missing_from_their_export_is_named_not_dropped():
    body, report = tcgplayer.fill(_upload(THEIRS), [_ours(tcg_name="Black Lotus")])
    assert len(list(csv.reader(io.StringIO(body)))) == 1
    assert report.matched == 0
    assert report.unmatched and "Black Lotus" in report.unmatched[0]


def test_a_card_we_hold_no_price_for_is_skipped_and_said_so():
    """TCGplayer rejects an import row with no marketplace price."""
    _, report = tcgplayer.fill(_upload(THEIRS), [_ours(list_price=None)])
    assert report.matched == 0
    assert report.unpriced and "Abundance" in report.unpriced[0]


def test_two_ids_for_one_description_are_dropped_rather_than_guessed():
    """24 keys of 800,318 in a real file. A wrong SKU edits a real listing."""
    twice = [
        *THEIRS,
        '"999999","Magic","10th Edition","Abundance","","249","R","Near Mint","1.68","","","","","0","",""',
    ]
    body, report = tcgplayer.fill(_upload(twice), [_ours()])
    assert len(list(csv.reader(io.StringIO(body)))) == 1, "neither id may be written"
    assert report.matched == 0
    assert report.ambiguous and "Abundance" in report.ambiguous[0]


def test_the_wrong_export_is_rejected_by_name():
    """The pricing screen offers several files and only one of them is this.

    Reading on would hand back a valid, empty CSV, and a seller with no idea
    why their inventory had vanished.
    """
    other = b"Order Number,Product Name,Quantity\n123,Abundance,1\n"
    with pytest.raises(tcgplayer.NotAPricingExport, match="Export Filtered CSV"):
        tcgplayer.fill([other], [_ours()])


def test_an_empty_file_is_rejected_rather_than_answered_with_a_header():
    with pytest.raises(tcgplayer.NotAPricingExport, match="empty"):
        tcgplayer.fill([b""], [_ours()])


def test_the_add_is_our_stock_minus_what_they_already_hold():
    """The bug this replaced: a whole stock line written into a delta column.

    `Add to Quantity` adds. Three copies with one already listed went out as a
    "3", landed on their "1", and offered four cards — which is an oversell,
    a cancellation and a mark against the seller, on every run after the first.
    """
    theirs = [
        '"4519","Magic","10th Edition","Abundance","","249","R","Near Mint","1.68","","2.9000","1.4100","1","0","",""'
    ]
    body, report = tcgplayer.fill(_upload(theirs), [_ours()])
    row = dict(zip(tcgplayer.HEADER, list(csv.reader(io.StringIO(body)))[1], strict=True))
    assert row["Add to Quantity"] == "2", "3 in stock, 1 already listed"
    assert row["Total Quantity"] == "1", "theirs, and the number the delta was measured against"
    assert row["TCG Marketplace Price"] == "1.50"
    assert not report.reduced


def test_a_line_already_at_our_level_stays_in_the_file_at_zero():
    """The price beside it is still ours to write.

    A listing at the right quantity and the wrong price is a thing the seller
    came here to fix, so the row is not dropped for having no cards to add.
    """
    theirs = [
        '"4519","Magic","10th Edition","Abundance","","249","R","Near Mint","1.68","","2.9000","1.4100","3","0","",""'
    ]
    body, report = tcgplayer.fill(_upload(theirs), [_ours()])
    row = dict(zip(tcgplayer.HEADER, list(csv.reader(io.StringIO(body)))[1], strict=True))
    assert row["Add to Quantity"] == "0"
    assert row["TCG Marketplace Price"] == "1.50"
    assert report.matched == 1


def test_more_listed_than_we_hold_comes_back_down():
    """The column takes a negative, which is what makes this a sync.

    A copy sold or discarded here has to come down there, and the alternative
    — clamping at zero — leaves a listing for a card that cannot be shipped,
    which is the same failure the delta was introduced to stop.
    """
    theirs = [
        '"4519","Magic","10th Edition","Abundance","","249","R","Near Mint","1.68","","2.9000","1.4100","5","0","",""'
    ]
    body, report = tcgplayer.fill(_upload(theirs), [_ours()])
    row = dict(zip(tcgplayer.HEADER, list(csv.reader(io.StringIO(body)))[1], strict=True))
    assert row["Add to Quantity"] == "-2"
    assert report.reduced and "Abundance" in report.reduced[0], "a listing coming down is said"


def test_uploading_the_same_file_twice_is_a_no_op_the_second_time():
    """Their export after the first upload says what the first upload made it
    say, so the second run has nothing left to add."""
    first = [
        '"4519","Magic","10th Edition","Abundance","","249","R","Near Mint","1.68","","2.9000","1.4100","","0","",""'
    ]
    body, _ = tcgplayer.fill(_upload(first), [_ours()])
    row = dict(zip(tcgplayer.HEADER, list(csv.reader(io.StringIO(body)))[1], strict=True))
    assert row["Add to Quantity"] == "3"

    theirs_after = row["Total Quantity"] = str(int(row["Total Quantity"]) + 3)
    assert theirs_after == "3"
    again, _ = tcgplayer.fill(
        _upload([",".join(f'"{row[h]}"' for h in tcgplayer.HEADER)]), [_ours()]
    )
    assert (
        dict(zip(tcgplayer.HEADER, list(csv.reader(io.StringIO(again)))[1], strict=True))[
            "Add to Quantity"
        ]
        == "0"
    )


def test_a_blank_total_quantity_becomes_an_explicit_zero():
    """The uploader rejects a blank total on a row it is importing.

    Their export leaves it blank on every card they hold none of, which is
    most of them, so the file it produces is not one it will take back
    unaltered. Zero says exactly what blank said, in the spelling it accepts.
    """
    body, _ = tcgplayer.fill(_upload(THEIRS), [_ours()])
    row = dict(zip(tcgplayer.HEADER, list(csv.reader(io.StringIO(body)))[1], strict=True))
    assert row["Total Quantity"] == "0"
    assert row["Add to Quantity"] == "3", "nothing listed yet, so the delta is the whole line"


def test_a_total_that_has_been_through_a_spreadsheet_still_reads():
    """`2.0` and `1,024` are what Excel does to a column of integers."""
    theirs = [
        '"4519","Magic","10th Edition","Abundance","","249","R","Near Mint","1.68","","2.9000","1.4100"," 2.0 ","0","",""'
    ]
    body, _ = tcgplayer.fill(_upload(theirs), [_ours()])
    row = dict(zip(tcgplayer.HEADER, list(csv.reader(io.StringIO(body)))[1], strict=True))
    assert row["Total Quantity"] == "2"
    assert row["Add to Quantity"] == "1"


def test_a_total_that_is_not_a_number_drops_the_row_and_names_it():
    """Reading it as zero is the oversell the delta exists to prevent."""
    theirs = [
        '"4519","Magic","10th Edition","Abundance","","249","R","Near Mint","1.68","","2.9000","1.4100","lots","0","",""'
    ]
    body, report = tcgplayer.fill(_upload(theirs), [_ours()])
    assert len(list(csv.reader(io.StringIO(body)))) == 1
    assert report.matched == 0
    assert report.unreadable and "Abundance" in report.unreadable[0]


def test_the_file_is_shaped_like_the_one_it_came_from():
    """CRLF, every data field quoted, the header row bare.

    None of that is required by CSV. It is free, and the validator on the far
    end is one that cannot be tested against from here, so the file it gets is
    shaped exactly like the file it produced.
    """
    body, _ = tcgplayer.fill(_upload(THEIRS), [_ours()])
    lines = body.split("\r\n")
    assert lines[0] == HEADER, "the header is unquoted, as theirs is"
    assert lines[1].startswith('"4519","Magic"'), "data rows are fully quoted, as theirs are"
    assert "\n" not in body.replace("\r\n", "")
