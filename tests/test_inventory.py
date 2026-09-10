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


def test_the_foil_line_is_read_off_the_word():
    """TCGplayer names printings, not finishes — Pokemon uses Holofoil and
    Reverse Holofoil, and a new game will invent its own. Matching on the word
    rather than an exhaustive list means a game we have not seen yet still
    lands on the right side of the only distinction a seller can make in bulk.

    This is the half of the picker that is still a pure function. Which
    printing a row is *priced* at is `inventory.priced_printing`, a lateral, and
    its cases are in `tests/test_inventory_scale.py` — including the ones that
    used to live here.
    """
    from foilstack.inventory import finish_of

    assert finish_of("Foil") == "foil"
    assert finish_of("Reverse Holofoil") == "foil"
    assert finish_of("1st Edition Holofoil") == "foil"
    assert finish_of("Normal") == "nonfoil"
    assert finish_of("Unlimited") == "nonfoil"


def _priced(**markets):
    """A price map, as `_prices_for` returns one. `None` is an unpriced printing."""
    from types import SimpleNamespace

    return {
        name.replace("_", " "): SimpleNamespace(market=market, low=None)
        for name, market in markets.items()
    }


def test_priced_finishes_reports_what_the_catalogue_actually_has():
    """More than a third of the catalogue has no foil printing at all, so
    "which finishes are real for this card" is a question the screens have to
    be able to ask before they offer both."""
    from foilstack.inventory import priced_finishes

    assert priced_finishes(_priced(Normal=2.0)) == {"nonfoil"}
    assert priced_finishes(_priced(Holofoil=9.0, Reverse_Holofoil=4.0)) == {"foil"}
    assert priced_finishes(_priced(Normal=2.0, Foil=9.0)) == {"nonfoil", "foil"}
    # Nothing known is not the same as nothing available.
    assert priced_finishes({}) == set()


def test_a_printing_with_no_price_is_not_a_finish_the_catalogue_prices():
    """The distinction this whole pair of functions exists to make.

    `ingest` keeps a printing whose market price is null on purpose — it still
    names the sub-type — so "the catalogue has a foil printing" and "the
    catalogue will pay for a foil" are different facts about 2,700 cards here.
    Reading the first as the second is what let one scan of one card become a
    foil down the auto-accept path and a non-foil down the review queue.
    """
    from foilstack.inventory import priced_finishes, priced_printings

    both_catalogued = _priced(Normal=2.10, Holofoil=None)
    assert priced_printings(both_catalogued) == ["Normal"]
    assert priced_finishes(both_catalogued) == {"nonfoil"}
    # And a card with nothing priced offers nothing, rather than offering both.
    assert priced_finishes(_priced(Normal=None, Holofoil=None)) == set()


def test_a_default_finish_gives_way_to_the_card_that_matched():
    """The seller answers "foil or not" once for a whole batch, and the batch
    is not all one card. Where the catalogue prices a card on one side of the
    foil line only, that side wins: the default was never a decision about
    that card, and marking it as a deviation asked the seller to click away
    something they had no other answer to."""
    from foilstack.inventory import resolve_finish

    assert resolve_finish("nonfoil", _priced(Holofoil=9.0)) == "foil"
    assert resolve_finish("foil", _priced(Normal=2.0)) == "nonfoil"
    # And an unpriced printing does not hold the default in place. This is the
    # divergence itself: the batch says foil, the card has a foil printing, and
    # nobody will pay for it — so the row is a non-foil, whichever path asks.
    assert resolve_finish("foil", _priced(Normal=2.10, Holofoil=None)) == "nonfoil"


def test_a_default_finish_stands_wherever_the_catalogue_is_ambiguous():
    """The rule only fires where there is exactly one honest answer. A card
    priced in both finishes, or in none, is the seller's call — overriding
    there would be guessing rather than deferring."""
    from foilstack.inventory import resolve_finish

    assert resolve_finish("nonfoil", _priced(Normal=2.0, Holofoil=9.0)) == "nonfoil"
    assert resolve_finish("foil", _priced(Normal=2.0, Holofoil=9.0)) == "foil"
    # No prices at all is not evidence of anything, and a catalogue that names
    # two printings while pricing neither is no more evidence than an empty one.
    assert resolve_finish("foil", {}) == "foil"
    assert resolve_finish("nonfoil", {}) == "nonfoil"
    assert resolve_finish("foil", _priced(Normal=None)) == "foil"
    assert resolve_finish("nonfoil", _priced(Holofoil=None)) == "nonfoil"


def test_a_resolved_finish_is_never_the_one_that_falls_back():
    """The point of resolving is that the warning stops firing on rows nobody
    chose. If these two disagreed, the queue would seed a row onto a finish
    and then mark it as priced off the other one."""
    from foilstack.inventory import priced_finishes, resolve_finish

    maps = [
        _priced(Normal=2.0),
        _priced(Holofoil=9.0),
        _priced(Normal=2.0, Foil=9.0),
        _priced(Normal=2.10, Holofoil=None),
        _priced(Normal=None, Holofoil=9.0),
        _priced(Normal=None),
        {},
    ]
    for by_sub in maps:
        for default in ("foil", "nonfoil"):
            picked = resolve_finish(default, by_sub)
            available = priced_finishes(by_sub)
            assert not available or picked in available


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


# --------------------------------------------------------------------------
# The value threshold: which cards count towards a position. A reporting
# choice and nothing else — it must never move a price, a sale or a card.
# --------------------------------------------------------------------------


def test_threshold_rejects_values_that_are_not_numbers():
    from foilstack.inventory import parse_threshold

    for bad in ("", "abc", None, "nan", "inf", "-inf"):
        with pytest.raises(ValueError):
            parse_threshold(bad)


def test_threshold_is_bounded_at_both_ends():
    """Unbounded, one hand-edited `?min=` reports a whole shelf as worth $0."""
    from foilstack.inventory import MAX_THRESHOLD, parse_threshold

    with pytest.raises(ValueError):
        parse_threshold(-1)
    with pytest.raises(ValueError):
        parse_threshold(MAX_THRESHOLD + 0.01)
    assert parse_threshold(MAX_THRESHOLD) == MAX_THRESHOLD
    assert parse_threshold("1.005") == 1.0


def test_a_card_worth_exactly_the_threshold_counts():
    """ "At least" is the label on the field, so the bar has to be inclusive."""
    from foilstack.inventory import counts_towards_value

    assert counts_towards_value(_row(market=1.00), 1.00)
    assert not counts_towards_value(_row(market=0.99), 1.00)


def test_an_unpriced_card_does_not_clear_a_threshold():
    """It cannot be shown to be worth a dollar, and it adds nothing either way.

    All this decides is whether the screen counts it as left out and says so,
    which is the side that hides nothing.
    """
    from foilstack.inventory import counts_towards_value

    assert not counts_towards_value(_row(market=None), 1.00)
    # ...but at no threshold at all, nothing is excluded from anything.
    assert counts_towards_value(_row(market=None), 0.0)


def test_zero_threshold_counts_every_card():
    from foilstack.inventory import split_by_value

    rows = [_row(market=0.01), _row(market=None), _row(market=500.0)]
    counted, excluded = split_by_value(rows, 0.0)
    assert len(counted) == 3
    assert excluded == []


def test_split_keeps_every_row_on_exactly_one_side():
    """The 'left out' figure explains the total, so the two must partition."""
    from foilstack.inventory import split_by_value

    rows = [_row(market=m) for m in (0.10, 0.10, 1.00, 12.5, None)]
    counted, excluded = split_by_value(rows, 1.00)
    assert len(counted) + len(excluded) == len(rows)
    assert sum(r["market"] or 0 for r in counted) == 13.5
    assert len(excluded) == 3


class _FakeUser:
    def __init__(self, value_threshold):
        self.value_threshold = value_threshold


def test_a_bad_query_threshold_falls_back_to_the_sellers_own():
    """Not to the shipped zero. A stale bookmark reports the position the way
    they set it up rather than silently widening it back to everything."""
    from foilstack.inventory import threshold_for

    assert threshold_for(_FakeUser(1.0), "abc") == 1.0
    assert threshold_for(_FakeUser(1.0), None) == 1.0
    assert threshold_for(_FakeUser(1.0), "5") == 5.0
    # And a column edited outside the bounds reads as "count everything",
    # which is the answer that hides nothing.
    assert threshold_for(_FakeUser(-3.0)) == 0.0
