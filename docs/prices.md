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
