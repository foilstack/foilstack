from foilstack.inventory import FLOOR, list_price


def test_condition_discounts_apply():
    assert list_price(10.0, "NM") == 10.0
    assert list_price(10.0, "LP") == 8.5
    assert list_price(10.0, "DMG") == 3.5


def test_floor_is_enforced():
    """Below the floor a listing costs more in fees and postage than it returns."""
    assert list_price(0.01, "NM") == FLOOR


def test_unpriced_card_has_no_list_price():
    """Never invent a number for a card the catalogue has no price for."""
    assert list_price(None, "NM") is None


def test_rules_apply_on_top_of_condition():
    """The two adjustments answer different questions and both must land."""
    assert list_price(10.0, "NM", "market") == 10.0
    assert list_price(10.0, "NM", "under") == 9.5
    # LP is worth 85% of market before the rule takes its 5% off.
    assert list_price(10.0, "LP", "under") == 8.07


def test_premium_only_applies_to_near_mint():
    """A premium on a played card is just an overpriced played card."""
    assert list_price(10.0, "NM", "premium") == 10.8
    assert list_price(10.0, "MP", "premium") == 7.0


def test_unknown_rule_falls_back_to_market():
    assert list_price(10.0, "NM", "nonsense") == list_price(10.0, "NM", "market")


def test_floor_survives_the_rules():
    assert list_price(0.10, "DMG", "lowplus") == FLOOR


def test_sku_is_stable_and_distinct_per_row():
    from foilstack.inventory import sku
    assert sku(1) == "FS-10001"
    assert sku(1) != sku(2)


def _row(**kw):
    base = {
        "card_id": 1, "condition": "NM", "finish": "nonfoil",
        "market": 10.0, "cost": None, "list_price": 10.0,
        "sold": False, "sold_price": None,
    }
    base.update(kw)
    return base


def test_sold_rows_are_excluded_from_position():
    """A sold card left in inventory value is a number a seller would act on."""
    from foilstack.inventory import totals

    t = totals([
        _row(market=10.0), _row(market=10.0),
        _row(card_id=2, market=50.0, sold=True, sold_price=45.0),
    ])
    assert t["count"] == 2          # the sold one is gone from stock
    assert t["market"] == 20.0      # and gone from the value
    assert t["sold_rows"] == 1
    assert t["realised"] == 45.0


def test_realised_profit_needs_a_cost_basis():
    """Without a recorded cost, 'profit' is just the sale price renamed."""
    from foilstack.inventory import totals

    without = totals([_row(sold=True, sold_price=45.0)])
    assert without["realised_profit"] is None

    with_cost = totals([_row(sold=True, sold_price=45.0, cost=20.0)])
    assert with_cost["realised_profit"] == 25.0


def test_finishes_are_the_two_a_seller_declares():
    from foilstack.inventory import FINISHES

    assert FINISHES == ["nonfoil", "foil"]


def test_lowplus_undercuts_the_real_lowest_listing():
    """This rule was an invented multiplier until `card_prices.low` existed.

    It claimed to undercut the cheapest copy on the market while actually
    taking 18% off market and hoping — which is a different number, in an
    unpredictable direction, on every card.
    """
    from foilstack.inventory import list_price

    assert list_price(10.0, "NM", "lowplus", low=6.40) == 6.41


def test_lowplus_still_discounts_for_condition():
    """Undercutting a near-mint listing by a cent with a played card is not
    undercutting, it is overcharging."""
    from foilstack.inventory import list_price

    assert list_price(10.0, "LP", "lowplus", low=6.40) == round(6.40 * 0.85 + 0.01, 2)


def test_lowplus_falls_back_when_nothing_is_listed():
    """A printing nobody is selling has no lowest listing to undercut."""
    from foilstack.inventory import list_price

    assert list_price(10.0, "NM", "lowplus", low=None) == 8.21


def test_other_rules_ignore_the_lowest_listing():
    """What a card is worth and what the cheapest seller wants are different
    questions; only one rule asks the second one."""
    from foilstack.inventory import list_price

    assert list_price(10.0, "NM", "market", low=1.00) == 10.0
    assert list_price(10.0, "NM", "under", low=1.00) == 9.5


def test_finish_picks_the_matching_printing():
    """A foil priced at its non-foil market value is wrong by a multiple, and
    always wrong in the direction that loses money."""
    from foilstack.inventory import matching_printings

    assert matching_printings("foil", ["Foil", "Normal"]) == ["Foil"]
    assert matching_printings("nonfoil", ["Foil", "Normal"]) == ["Normal"]


def test_finish_matching_handles_games_we_have_not_seen():
    """TCGplayer names printings, not finishes — Pokemon uses Holofoil and
    Reverse Holofoil, and a new game will invent its own."""
    from foilstack.inventory import matching_printings

    assert matching_printings("foil", ["Normal", "Reverse Holofoil"]) == ["Reverse Holofoil"]
    assert matching_printings("nonfoil", ["1st Edition Holofoil", "Unlimited"]) == ["Unlimited"]
    assert matching_printings("foil", []) == []


def test_a_single_printing_serves_both_finishes():
    """Rather than returning nothing and pricing the card at zero."""
    from foilstack.inventory import matching_printings

    assert matching_printings("foil", ["Normal"]) == ["Normal"]
    assert matching_printings("nonfoil", ["Foil"]) == ["Foil"]


def test_ambiguous_foil_printings_price_high():
    """Base Set Blastoise is "1st Edition Holofoil" at $1300 and "Unlimited
    Holofoil" at $820. A seller who ticked "foil" has not said which.

    Guessing high leaves a card unsold and noticed; guessing low sells it
    immediately at a loss and the seller finds out from the payout.
    """
    from types import SimpleNamespace

    from foilstack.inventory import pick_printing

    by_sub = {
        "1st Edition Holofoil": SimpleNamespace(market=1300.0),
        "Unlimited Holofoil": SimpleNamespace(market=820.0),
        "Normal": SimpleNamespace(market=12.0),
    }
    assert pick_printing("foil", by_sub) == "1st Edition Holofoil"
    assert pick_printing("nonfoil", by_sub) == "Normal"


def test_printing_choice_survives_a_missing_price():
    from types import SimpleNamespace

    from foilstack.inventory import pick_printing

    by_sub = {"Foil": SimpleNamespace(market=None), "Normal": SimpleNamespace(market=2.0)}
    assert pick_printing("foil", by_sub) == "Foil"
    assert pick_printing("nonfoil", {}) is None


def _pt(days_ago, value):
    import datetime as dt

    from foilstack.prices import Point

    return Point(dt.date.today() - dt.timedelta(days=days_ago), value)


def test_a_single_reading_is_a_dot_not_a_line():
    """A one-point line is a line drawn through no information."""
    from foilstack.prices import spark

    out = spark([_pt(0, 4.25)])
    assert out["points"] == 1
    assert out["path"] == ""
    assert out["dot"]["x"] > 0 and out["dot"]["y"] > 0


def test_no_readings_draws_nothing():
    from foilstack.prices import spark

    assert spark([])["points"] == 0


def test_a_flat_price_is_a_flat_line():
    """Scaling a series with no span turns rounding noise into a mountain."""
    from foilstack.prices import spark

    out = spark([_pt(2, 3.0), _pt(1, 3.0), _pt(0, 3.0)])
    ys = {seg.split()[1] for seg in out["path"].replace("M ", "").split(" L ")}
    assert len(ys) == 1


def test_deltas_read_the_change_log_as_a_change_log():
    """Most dates have no row. The price on a day is the last one recorded on
    or before it — asking for an exact date would find nothing and report no
    change on a card that had moved."""
    from foilstack.prices import summarise

    out = summarise([_pt(40, 10.0), _pt(20, 12.0), _pt(0, 15.0)])
    # 7 days ago: nothing recorded that day, so the 20-day-old reading stands.
    assert out["d7"]["abs"] == 3.0
    assert out["d30"]["abs"] == 5.0


def test_a_delta_is_not_quoted_beyond_the_history_we_have():
    """With a week of readings, a "30-day change" would be the change since the
    first reading wearing a label that says otherwise."""
    from foilstack.prices import summarise

    out = summarise([_pt(5, 10.0), _pt(0, 12.0)])
    assert out["d30"] is None
    assert out["d7"] is None
