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

A marketplace's own vocabulary is not a transform. `Condition` on a TCGplayer
upload is "Near Mint Foil", one string carrying both the condition and the
printing; the transform list in `exports.py` is deliberately too small to build
that, and growing it would end in an expression language nobody designed. The
translation belongs in `inventory.export_rows`, which is why the row carries
`tcg_condition` and `tcg_product_line` alongside the plain `condition` and
`game` every other exporter reads.

### The TCGplayer file

Two things about it are worth knowing before you edit `tcgplayer.toml`.

**The header row is validated as a whole.** The uploader compares it against
the one its own export writes and answers `Headers are not valid!` — not to a
bad value in a row, and with no hint at which column is at fault. All sixteen
columns have to be present, in order, including the four this catalogue has
nothing to put in. `tests/test_exports.py` pins the row character for
character against a real export.

**`TCGplayer Id` is a SKU id, and we do not have one.** It identifies a
product *and* a condition *and* a printing: 10th Edition Abundance is product
15023, but SKU 4519 near mint and 4521 near mint foil. TCGCSV says outright
that it does not publish SKUs, so this exporter writes the column blank.
Writing the product id there instead is the tempting fix and the dangerous one
— the two id spaces overlap numerically, so it does not fail, it edits an
unrelated listing.

A file with a blank id column is the right shape and not yet uploadable, which
is why the listings screen offers a round trip instead.

### The round trip

`foilstack.tcgplayer` takes the seller's own **Export Filtered CSV** from the
TCGplayer pricing screen, finds their stock among its rows, writes `Add to
Quantity` and `TCG Marketplace Price`, and returns those rows and nothing else.
The ids come back because they were theirs to begin with, and so does every
informative column — Rarity, Photo URL, the three price columns — which the
exporter above can only leave empty. The header row is theirs by construction.

There is no shared id to join on, so the join is on five descriptive columns:
Product Line, Set Name, Product Name, Number and Condition. Measured on a real
800,344-row export, those are unique for all but 24 keys; the ambiguous ones
are dropped rather than guessed.

`Product Name` is the reason `cards.source_name` exists. TCGplayer's file
carries the raw spelling and TCGCSV's `cleanName` — what `cards.name` holds —
strips punctuation, so joining on `name` loses `Ancestor's Chosen`,
`Circle of Protection: Blue` and every other card with punctuation in it:
about one in ten, silently. On the same set the match rate is 89.7% joined on
`name` and 100% joined on `source_name`.

That column is nullable and only `foilstack ingest` fills it. **A catalogue
ingested before it existed needs a re-ingest**, or the round trip quietly falls
back to the cleaned spelling and reports every punctuated card as missing.
Re-ingest is a catalogue pull, not a re-encode — embeddings are untouched.

Nothing reads the quantities in the uploaded file. They are the seller's
positions on another marketplace, and the upload is never written to disk or
stored; it is streamed once, which is what keeps a 100 MB file to about 30 MB
of memory and a second and a half.
