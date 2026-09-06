import pytest

from foilstack.inventory import (
    DEFAULT_FLOOR,
    MAX_FLOOR,
    Pricing,
    list_price,
    parse_floor,
)


def test_condition_discounts_apply():
    assert list_price(10.0, "NM", Pricing()) == 10.0
    assert list_price(10.0, "LP", Pricing()) == 8.5
    assert list_price(10.0, "DMG", Pricing()) == 3.5


def test_floor_is_enforced():
    """Below the floor a listing costs more in fees and postage than it returns."""
    assert list_price(0.01, "NM", Pricing()) == DEFAULT_FLOOR


def test_unpriced_card_has_no_list_price():
    """Never invent a number for a card the catalogue has no price for."""
    assert list_price(None, "NM", Pricing()) is None


def test_rules_apply_on_top_of_condition():
    """The two adjustments answer different questions and both must land."""
    assert list_price(10.0, "NM", Pricing("market")) == 10.0
    assert list_price(10.0, "NM", Pricing("under")) == 9.5
    # LP is worth 85% of market before the rule takes its 5% off.
    assert list_price(10.0, "LP", Pricing("under")) == 8.07


def test_premium_only_applies_to_near_mint():
    """A premium on a played card is just an overpriced played card."""
    assert list_price(10.0, "NM", Pricing("premium")) == 10.8
    assert list_price(10.0, "MP", Pricing("premium")) == 7.0


def test_unknown_rule_falls_back_to_market():
    assert list_price(10.0, "NM", Pricing.of("nonsense")) == list_price(
        10.0, "NM", Pricing("market")
    )


def test_floor_survives_the_rules():
    assert list_price(0.10, "DMG", Pricing("lowplus")) == DEFAULT_FLOOR


def test_sku_is_stable_and_distinct_per_row():
    from foilstack.inventory import sku

    assert sku(1) == "FS-10001"
    assert sku(1) != sku(2)


def _row(**kw):
    base = {
        "card_id": 1,
        "condition": "NM",
        "finish": "nonfoil",
        "market": 10.0,
        "cost": None,
        "list_price": 10.0,
        "sold": False,
        "sold_price": None,
    }
    base.update(kw)
    return base


def test_sold_rows_are_excluded_from_position():
    """A sold card left in inventory value is a number a seller would act on."""
    from foilstack.inventory import totals

    t = totals(
        [
            _row(market=10.0),
            _row(market=10.0),
            _row(card_id=2, market=50.0, sold=True, sold_price=45.0),
        ]
    )
    assert t["count"] == 2  # the sold one is gone from stock
    assert t["market"] == 20.0  # and gone from the value
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
    assert list_price(10.0, "NM", Pricing("lowplus"), low=6.40) == 6.41


def test_lowplus_still_discounts_for_condition():
    """Undercutting a near-mint listing by a cent with a played card is not
    undercutting, it is overcharging."""
    assert list_price(10.0, "LP", Pricing("lowplus"), low=6.40) == round(6.40 * 0.85 + 0.01, 2)


def test_lowplus_falls_back_when_nothing_is_listed():
    """A printing nobody is selling has no lowest listing to undercut."""
    assert list_price(10.0, "NM", Pricing("lowplus"), low=None) == 8.21


def test_other_rules_ignore_the_lowest_listing():
    """What a card is worth and what the cheapest seller wants are different
    questions; only one rule asks the second one."""
    assert list_price(10.0, "NM", Pricing("market"), low=1.00) == 10.0
    assert list_price(10.0, "NM", Pricing("under"), low=1.00) == 9.5


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


def test_priced_finishes_reports_what_the_catalogue_actually_has():
    """More than a third of the catalogue has no foil printing at all, so
    "which finishes are real for this card" is a question the screens have to
    be able to ask before they offer both."""
    from foilstack.inventory import priced_finishes

    assert priced_finishes(["Normal"]) == {"nonfoil"}
    assert priced_finishes(["Holofoil", "Reverse Holofoil"]) == {"foil"}
    assert priced_finishes(["Normal", "Foil"]) == {"nonfoil", "foil"}
    # Nothing known is not the same as nothing available.
    assert priced_finishes([]) == set()


def test_a_default_finish_gives_way_to_the_card_that_matched():
    """The seller answers "foil or not" once for a whole batch, and the batch
    is not all one card. Where the catalogue prices a card on one side of the
    foil line only, that side wins: the default was never a decision about
    that card, and marking it as a deviation asked the seller to click away
    something they had no other answer to."""
    from foilstack.inventory import resolve_finish

    assert resolve_finish("nonfoil", ["Holofoil"]) == "foil"
    assert resolve_finish("foil", ["Normal"]) == "nonfoil"


def test_a_default_finish_stands_wherever_the_catalogue_is_ambiguous():
    """The rule only fires where there is exactly one honest answer. A card
    priced in both finishes, or in none, is the seller's call — overriding
    there would be guessing rather than deferring."""
    from foilstack.inventory import resolve_finish

    assert resolve_finish("nonfoil", ["Normal", "Holofoil"]) == "nonfoil"
    assert resolve_finish("foil", ["Normal", "Holofoil"]) == "foil"
    # No prices at all is not evidence of anything.
    assert resolve_finish("foil", []) == "foil"
    assert resolve_finish("nonfoil", []) == "nonfoil"


def test_a_resolved_finish_is_never_the_one_that_falls_back():
    """The point of resolving is that the warning stops firing on rows nobody
    chose. If these two disagreed, the queue would seed a row onto a finish
    and then mark it as priced off the other one."""
    from foilstack.inventory import priced_finishes, resolve_finish

    for names in [["Normal"], ["Holofoil"], ["Normal", "Foil"], []]:
        for default in ("foil", "nonfoil"):
            picked = resolve_finish(default, names)
            assert not names or picked in priced_finishes(names)


def test_the_fallback_is_reported_not_just_taken():
    """`matching_printings` crossing the foil line is the one path that prices
    a card off the wrong side of the seller's own answer. The two functions
    have to agree about when that happened, or the warning appears on the
    wrong rows."""
    from foilstack.inventory import matching_printings, priced_finishes

    for finish, names in [("foil", ["Normal"]), ("nonfoil", ["Holofoil"])]:
        picked = matching_printings(finish, names)
        crossed = finish not in priced_finishes(names)
        assert crossed and picked == names


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


# --- the floor, which is now the seller's number rather than the software's ---


class _Seller:
    """Just the one field `Pricing.for_user` reads."""

    def __init__(self, price_floor: float) -> None:
        self.price_floor = price_floor


def test_the_account_floor_is_what_prices_a_card():
    """The whole point of the column: two sellers, same card, two answers."""
    bulk = Pricing.for_user(_Seller(0.10))
    shop = Pricing.for_user(_Seller(1.00))
    assert list_price(0.05, "NM", bulk) == 0.10
    assert list_price(0.05, "NM", shop) == 1.00


def test_a_floor_never_lowers_a_price():
    """It raises what came out under it and touches nothing else."""
    assert list_price(10.0, "NM", Pricing(floor=1.00)) == 10.0


def test_zero_is_a_floor_and_not_an_absent_one():
    """A seller who wants no floor gets no floor, not the shipped default."""
    assert Pricing.of(floor="0").floor == 0.0
    assert list_price(0.01, "NM", Pricing(floor=0.0)) == 0.01


def test_a_run_floor_overrides_the_account_one():
    assert Pricing.for_user(_Seller(1.00), "market", "0.25").floor == 0.25


def test_an_unusable_run_floor_falls_back_to_the_account_floor():
    """Not to the shipped one. A mangled URL must not reprice a run at 35c."""
    for bad in ("", "  ", "abc", "nan", "1e9", "-1"):
        assert Pricing.for_user(_Seller(1.00), "market", bad).floor == 1.00


def test_floor_is_rounded_to_money():
    """A floor that prints as $1.00 and prices as 0.999 disagrees with itself."""
    assert parse_floor("0.999") == 1.00
    assert parse_floor(0.994) == 0.99


def test_parse_floor_rejects_rather_than_correcting():
    """The save path needs a refusal; `Pricing.of` is what turns one into a fallback."""
    for bad in ("abc", "", "nan", "inf", "-0.01", str(MAX_FLOOR + 1)):
        with pytest.raises(ValueError):
            parse_floor(bad)


def test_a_bad_rule_does_not_discard_a_good_floor():
    """The two halves of a policy fail independently, or one guard cancels the other."""
    assert Pricing.of("nonsense", "2.00") == Pricing(rule="market", floor=2.00)
