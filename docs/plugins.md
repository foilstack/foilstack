# Plugins

Three kinds, with deliberately different amounts of power.

## Source plugins

These supply the catalogue. They run code and reach the network, so nothing is
installed automatically and there is no registry — you add one deliberately.

`tcgcsv` ships as the reference implementation, and it is a plugin rather than a
special case on purpose: if the primary data source cannot be expressed through
the interface every other plugin uses, the interface is wrong.

`foilstack plugins` lists what is installed and the games each source can fetch.

The **Plugins** screen in the web interface answers the other half of that
question: not what could run, but what has. It reads `cards`,
`card_embeddings` and `sync_state` to show, per game actually ingested, how
many cards are held, how many of them are encoded for the configured model —
cards without a vector cannot be matched, and the failure is silent at scan
time — when prices last synced, and whether a backfill has run. The manifest
of installed plugins follows underneath it.

## Enrichment plugins

These add to a catalogue somebody else ingested. They own no rows: they join
onto cards a source already wrote, by the upstream id it recorded, which is why
an enricher has to name the source it can speak to.

The asymmetry with a source is the point. A source *is* a catalogue — it
decides which cards exist and must supply an image for every one of them,
because a row without an image is a card this application structurally cannot
match. An enricher is allowed to know nothing about images at all.

That is what lets `mtgjson` in. It publishes no card imagery — its own
documentation sends you to Scryfall — so it could never satisfy the source
contract, and what it has instead is three months of daily Magic prices for a
catalogue that otherwise remembers only the days since you installed this. See
[prices](prices.md#backfilling-magic).

## Export plugins

These are TOML column mappings, not code:

```toml
name = "tcgplayer"
label = "TCGplayer"
filename = "tcgplayer-listings.csv"

[[columns]]
header = "Product Name"
field = "name"

[[columns]]
header = "TCG Marketplace Price"
field = "list_price"
transform = "money2"
```

Adding a marketplace means writing a file a reviewer can read in ten seconds and
be certain does nothing else. See `src/foilstack/plugins/`.
