"""The inventory screen's facets, filters and sort, over plain dicts.

Grouped lines and copies, not database rows: everything under test here is a
pure fold over what `groups` produces, and building it by hand is what lets a
case like "a line with one LP copy and one NM copy" be one readable literal
rather than six inserts.
"""

from foilstack.inventory import (
    FACET_LIMIT,
    facet_options,
    filter_groups,
    line_values,
    present_values,
    sort_groups,
)


def copy(**over):
    base = {
        "game": "magic",
        "set_name": "Base Set",
        "condition": "NM",
        "finish": "nonfoil",
        "listed": False,
    }
    return {**base, **over}


def line(name="Bolt", copies=None, **over):
    copies = copies or [copy()]
    first = copies[0]
    base = {
        "name": name,
        "game": first["game"],
        "set_name": first["set_name"],
        "number": "1",
        "quantity": len(copies),
        "cost": 1.0,
        "market": 2.0,
        "margin_pct": 50,
        "listed_label": "—",
        "copies": copies,
    }
    return {**base, **over}


def test_a_mixed_line_sits_under_every_value_it_holds():
    """Condition belongs to a copy, so one line can be both NM and LP."""
    row = line(copies=[copy(condition="NM"), copy(condition="LP")])
    assert line_values(row, "condition") == {"NM", "LP"}
    assert filter_groups([row], {"condition": {"LP"}}) == [row]
    assert filter_groups([row], {"condition": {"NM"}}) == [row]
    assert filter_groups([row], {"condition": {"MP"}}) == []


def test_a_partly_listed_line_is_both_listed_and_unlisted():
    """One copy on a marketplace and one in the box is honestly both."""
    row = line(copies=[copy(listed=True), copy(listed=False)])
    assert line_values(row, "listed") == {"listed", "unlisted"}


def test_values_within_a_facet_widen_and_facets_narrow():
    """Two games means either; a game and a condition means both at once."""
    mtg = line("Bolt", [copy(game="magic", condition="NM")])
    pkm = line("Pikachu", [copy(game="pokemon", condition="LP")])
    assert filter_groups([mtg, pkm], {"game": {"magic", "pokemon"}}) == [mtg, pkm]
    assert filter_groups([mtg, pkm], {"game": {"magic"}, "condition": {"LP"}}) == []


def test_an_empty_facet_does_not_filter():
    rows = [line("Bolt"), line("Counterspell")]
    assert filter_groups(rows, {"game": set(), "condition": set()}) == rows


def test_a_facet_is_counted_against_what_the_others_allow():
    """Counted against the final result, every unpicked chip would read zero —
    which says clicking it empties the screen when it would add rows."""
    nm = line("Bolt", [copy(condition="NM")])
    lp = line("Counterspell", [copy(condition="LP")])
    facets = {f["key"]: f for f in facet_options([nm, lp], {"condition": {"NM"}})}
    counts = {o["value"]: o["count"] for o in facets["condition"]["options"]}
    assert counts == {"NM": 1, "LP": 1}
    assert [o["on"] for o in facets["condition"]["options"]] == [True, False]


def test_a_picked_value_is_painted_even_at_zero():
    """It is the control for taking the filter off, so it has to be on screen."""
    rows = [line("Bolt", [copy(condition="NM")])]
    facets = {f["key"]: f for f in facet_options(rows, {"condition": {"LP"}})}
    options = {o["value"]: o for o in facets["condition"]["options"]}
    assert options["LP"]["count"] == 0
    assert options["LP"]["on"] is True


def test_a_facet_with_one_value_and_no_pick_is_dropped():
    """A chip that cannot change the result is chrome over the table."""
    rows = [line("Bolt"), line("Counterspell")]
    assert "game" not in {f["key"] for f in facet_options(rows, {})}


def test_a_long_facet_is_capped_and_says_how_many_it_hid():
    """A filter that quietly omits values is one a seller thinks lost their cards."""
    rows = [
        line(f"Card {i}", [copy(set_name=f"Set {i:02d}")] * (1 if i else 5))
        for i in range(FACET_LIMIT + 4)
    ]
    facets = {f["key"]: f for f in facet_options(rows, {})}
    assert len(facets["set"]["options"]) == FACET_LIMIT
    assert facets["set"]["hidden"] == 4
    # The most-held set survives the cap, and the options stay in name order.
    values = [o["value"] for o in facets["set"]["options"]]
    assert "Set 00" in values
    assert values == sorted(values)


def test_a_picked_value_survives_the_cap():
    rows = [line(f"Card {i}", [copy(set_name=f"Set {i:02d}")]) for i in range(FACET_LIMIT + 4)]
    facets = {f["key"]: f for f in facet_options(rows, {"set": {"Set 13"}})}
    assert "Set 13" in {o["value"] for o in facets["set"]["options"]}


def test_present_values_reads_the_whole_inventory_not_the_screen():
    """The guard against a stale URL must not become a filter that self-cancels.

    Narrowed to the visible rows instead, filtering to LP and then searching for
    a card with no LP copy makes the LP pick "absent", drops it, and answers
    with the near-mint copies under a chip that has disappeared.
    """
    copies = [copy(condition="NM"), copy(condition="LP")]
    assert present_values(copies, "condition") == {"NM", "LP"}
    assert "DMG" not in present_values(copies, "condition")


def test_missing_values_sort_last_in_both_directions():
    """A card with no cost recorded is not the cheapest card."""
    priced = line("Bolt", cost=5.0)
    dearer = line("Ancestral", cost=9.0)
    unknown = line("Mystery", cost=None)
    rows = [unknown, priced, dearer]
    assert [r["name"] for r in sort_groups(rows, "cost", "asc")] == ["Bolt", "Ancestral", "Mystery"]
    assert [r["name"] for r in sort_groups(rows, "cost", "desc")] == [
        "Ancestral",
        "Bolt",
        "Mystery",
    ]


def test_collector_numbers_sort_as_numbers_where_they_are_numbers():
    """Otherwise a set reads 1, 10, 100, 11 — which is not how it was printed."""
    rows = [line("c", number=n) for n in ("100", "2", "11", "1")]
    assert [r["number"] for r in sort_groups(rows, "set", "asc")] == ["1", "2", "11", "100"]


def test_a_non_numeric_collector_number_sorts_after_the_numbered_run():
    """A digit-prefix key alone puts "H12" above "12", which no set is printed in."""
    rows = [line("c", number=n) for n in ("SV49", "12", "H12", None, "233a")]
    assert [r["number"] for r in sort_groups(rows, "set", "asc")] == [
        "12",
        "233a",
        None,
        "H12",
        "SV49",
    ]


def test_an_unknown_sort_key_falls_back_rather_than_raising():
    """It arrives from the querystring, so it is not this function's to trust."""
    rows = [line("Zed"), line("Ana")]
    assert [r["name"] for r in sort_groups(rows, "nonsense", "asc")] == ["Ana", "Zed"]


# --- The paging control ------------------------------------------------------
#
# The screen is a window on the result now, and every number it prints about
# itself is a chance to report the window as the answer. These are about the
# control, not the query: which page numbers get painted, and whether the ends
# of the run stay reachable.


def test_one_page_offers_only_itself():
    from foilstack.web.routes.inventory import _pages

    assert _pages(1, 1) == [1]


def test_a_short_run_is_painted_whole():
    """No gaps until there is something worth eliding."""
    from foilstack.web.routes.inventory import _pages

    assert _pages(3, 5) == [1, 2, 3, 4, 5]


def test_both_ends_stay_reachable_from_the_middle():
    """The first and last page are always offered.

    Without them, getting back to the start of a large inventory is three
    hundred clicks on `Prev` or a hand-edited URL, and the seller who paged
    too far has no way back to their own cards.
    """
    from foilstack.web.routes.inventory import _pages

    assert _pages(12, 40) == [1, None, 10, 11, 12, 13, 14, None, 40]


def test_a_gap_is_only_marked_where_something_is_missing():
    """`1 … 2` would be a lie about an elision that did not happen.

    Page 5 of six really is missing from the run below, so the gap belongs
    there — the rule is that the mark tracks the omission, not that a long
    result always gets one.
    """
    from foilstack.web.routes.inventory import _pages

    assert _pages(2, 6) == [1, 2, 3, 4, None, 6]
    assert _pages(1, 40) == [1, 2, 3, None, 40]
    assert _pages(40, 40) == [1, None, 38, 39, 40]


def test_the_current_page_is_always_in_the_run():
    """Including at the ends, where the window is clipped on one side.

    Only pages that exist: the route clamps to the last page before it gets
    here, precisely so that a stale bookmark cannot ask for a page the control
    would then have to leave unmarked.
    """
    from foilstack.web.routes.inventory import _pages

    for last in (1, 2, 7, 40, 641):
        for page in {1, 2, last // 2, last - 1, last}:
            if 1 <= page <= last:
                assert page in _pages(page, last), (page, last)
