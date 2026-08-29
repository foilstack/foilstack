# Prices

How the catalogue stays current, why the history is worth protecting, and what
to do about cards whose printings are priced an order of magnitude apart.

## Syncing

The `prices` service keeps the catalogue current and, more importantly, records
what changed.

```bash
docker compose up -d          # the sync runs with everything else
docker compose exec web foilstack sync-prices --game magic
```

It follows [TCGCSV's usage guidelines](https://tcgcsv.com/docs): their files
rebuild exactly once a day and they publish the build timestamp, so each run
reads that first and stops after a single request when nothing has changed.
Requests are paced at 100ms as they ask.

## History cannot be rebuilt

**This is the one thing here that cannot be recreated.** Upstream mirrors the
current day — there is no historical endpoint — so a day this does not run is a
day of history gone permanently, for every card, at any price. Everything else
can be recreated by running `ingest` again.

For Magic, and only Magic, there is one exception — see below. Do not let it
change how you treat a broken sync: it recovers ninety days, from one game, for
printings TCGplayer sells. It is a repair, not a backup.

## Backfilling Magic

[MTGJSON](https://mtgjson.com) publishes a rolling ninety days of daily prices.
Its TCGplayer series is the same figure our `market` column holds, so it can
fill in the months before you installed this, or the week a sync was quietly
broken:

```bash
docker compose exec web foilstack enrich --game magic --dry-run
docker compose exec web foilstack enrich --game magic
```

Run the dry run first. It reports what it would record and writes nothing.

On a pip install rather than compose, the streaming JSON parser this needs for
a 1.1 GB file is an extra: `pip install 'foilstack[mtgjson]'`. The compose
image already carries it.

That the two series are the same quantity was measured, not assumed. Against a
113k-card catalogue on the same day, 85.2% of 150,344 printings matched
`market` exactly and 96.9% came within 5%; the exact-match rate against `mid`
was 9.4%, `low` 0.9%, `high` 0.06%. The remaining gap is a clock rather than a
different figure — MTGJSON builds at 1:00 AM EST and publishes at 9:00 AM EST,
and two thirds of the inexact rows equal a price we had recorded on an earlier
day ourselves.

**It can only add days, never change one.** A day `sync-prices` already wrote
came from the source this treats as authoritative and carries the full
low/mid/high spread; a backfilled day carries a market price alone. Rows
already present are excluded before the insert is built, and the insert ends
`ON CONFLICT DO NOTHING` anyway — so running it twice is free, and an operator
unsure whether the last attempt finished can simply run it again.

Some things it declines to do, all of them deliberate:

* **It will not name a printing your catalogue has not named.** A backfilled
  row has to match a `sub_type` that `card_prices` already holds, so run
  `sync-prices` at least once first. Otherwise nothing upstream offers has
  anywhere to go and the run reports a clean, misleading zero — which is why it
  refuses to start instead.
* **It skips printings TCGplayer lists twice.** About 141 of 151,008 resolve to
  more than one product id, and a price written to the wrong card is invisible
  in the way a wrong category id is invisible.
* **It is Magic only.** MTGJSON is a Magic project and claims nothing else.

Downloads land in `data/cache/mtgjson` and are checked against the SHA-256
MTGJSON publishes beside each file — which also makes the cache free to reuse,
so a re-run after a failure costs one small request rather than 180 MB.

History is a **change log, not a daily snapshot**: a row is written only when a
number moves, so a card that has not changed in a month has one row rather than
thirty. That matters when you query it — `WHERE recorded_on = '2026-08-23'`
returns the printings that *moved* that day, not the prices in effect. To value
a card on a date, take the most recent row at or before it.

## Naming the printing

Prices are stored per printing, so a foil is priced as a foil.

Foil is a coarse answer, though, and some cards have several printings that all
answer to it — Base Set Charizard is 1st Edition Holofoil at \$10,000, Unlimited
Holofoil at \$2,146 and Holofoil at \$855. Ticking "foil" chooses between none of
them, so **name the printing**: open a copy on the card page and pick from the
list, which shows each printing's current price beside it.

Until you do, the dearest matching printing is used and the row is marked
`guessed`. That direction is deliberate — an overpriced card sits unsold and you
notice, an underpriced one sells immediately and you find out from the payout.
