# Plugins

Two kinds, with deliberately different amounts of power.

## Source plugins

These supply the catalogue. They run code and reach the network, so nothing is
installed automatically and there is no registry — you add one deliberately.

`tcgcsv` ships as the reference implementation, and it is a plugin rather than a
special case on purpose: if the primary data source cannot be expressed through
the interface every other plugin uses, the interface is wrong.

`foilstack plugins` lists what is installed and the games each source can fetch.

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
