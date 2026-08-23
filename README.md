# Foilstack

Open-source card scanning, inventory and listing export. Drop in a `.zip` of
card scans, get back an identified, priced inventory and a CSV your marketplace
will accept.

Runs on your own machine. **Your scans never leave the host.**

> Status: early. The matching pipeline works end to end; expect rough edges.

![A scan of the whole workflow: reviewing matched cards, confirming one into
inventory, opening a card to see its price trend, and exporting the selection
as a marketplace CSV](src/foilstack/web/static/demo/foilstack.gif)

*Scans in, priced CSV out. Your scan on the left, the catalogue's guess beside
it, and the score between them — because the top match is evidence, not an
answer.*

## What it does

1. **Import** — a `.zip` of card images, one image per card, any filenames.
2. **Match** — every image is encoded and searched against a catalogue of
   reference card images. High-confidence matches are accepted automatically;
   everything else waits in a review queue with its runners-up.
3. **Inventory** — condition, quantity, cost, market price, suggested list
   price, margin.
4. **Export** — a CSV for TCGplayer or eBay, which you upload yourself.

## Why the review queue matters

Reprints share artwork. A vector search over card images gives you a **name you
can trust and a printing you cannot** — the same illustration may appear in a
dozen sets at wildly different prices, separated on the physical card only by a
set symbol a few pixels wide.

So the top match is shown next to its rivals with their scores, rather than
presented as an answer. The auto-accept threshold defaults to `0.92` and is
deliberately conservative: a missed auto-accept costs you one click, a wrong one
puts a real card on sale at another card's price.

## Quick start

```bash
git clone https://github.com/foilstack/foilstack.git
cd foilstack
cp .env.example .env
docker compose up -d
```

That starts Postgres, the encoder and the web app. Migrations run automatically
on every start, so an upgrade is `git pull && docker compose up -d --build` and
nothing else.

> **The default encoder is a gated model.** Accept the terms for
> [DINOv3](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m) and
> put an `HF_TOKEN` in your `.env` before the first run, or the encoder fails to
> load with a 401 that looks like a network problem.

Then build a catalogue. Start small — one game, a few hundred cards — to check
the whole path works before committing hours to encoding:

```bash
docker compose exec web foilstack ingest --source tcgcsv --game pokemon --limit 300
docker compose exec web foilstack embed
```

`ingest` pulls catalogue rows and image URLs; `embed` downloads each reference
image and encodes it. Two commands because they fail for different reasons and
take very different amounts of time — a network blip during encoding should not
cost you the ingest.

**Ingest every game you intend to scan.** Nearest-neighbour search can only
answer with a card that is in the catalogue, so scanning Magic against a
Pokemon-only catalogue returns Pokemon. `foilstack plugins` lists the games a
source can fetch.

Open <http://localhost:8090>.

## Running it for other people

There is no login screen by default: one implicit owner holds everything, and
you never invent a password for a tool only you can reach. Set
`FOILSTACK_MULTI_USER=true` and a real secret key and every scan, job and
inventory row belongs to exactly one account — enforced by a `NOT NULL` column
and a test suite that drives the real app and asserts a stranger gets nothing.

**[docs/accounts.md](docs/accounts.md)** — registration control, invite codes,
storage quotas, sign-in rate limiting.

## Backups

A service, not a cron job you have to remember. The `backup` sidecar starts with
everything else, dumps the database on a schedule, verifies what it wrote,
mirrors your scans beside it, and drops a `BACKUP_FAILING` file when a run
produces nothing usable. Checking for that file is the whole of your monitoring.

**[docs/backups.md](docs/backups.md)** — retention, copying off the machine,
restoring.

## Prices

Prices sync daily from [TCGCSV](https://tcgcsv.com/docs) and are stored per
printing, so a foil is priced as a foil — which matters more than it sounds.
Base Set Charizard is 1st Edition Holofoil at \$10,000, Unlimited Holofoil at
\$2,146 and Holofoil at \$855; ticking "foil" chooses between none of them.

Price history is the one thing here that cannot be rebuilt: upstream mirrors
only the current day, so a day the sync does not run is a day of history gone
for good.

**[docs/prices.md](docs/prices.md)** — the sync protocol, how history is stored,
naming a printing.

## Extending it

Source plugins fetch the catalogue and run code, so you add one deliberately and
there is no registry. Export plugins are TOML column mappings — adding a
marketplace means writing a file a reviewer can read in ten seconds and be
certain does nothing else.

**[docs/plugins.md](docs/plugins.md)**

## Encoding the catalogue

The encoder runs on CPU by default so the stack starts anywhere — about one card
a second, which is more than a day for a full Magic catalogue. If you have an
NVIDIA GPU, an overlay turns it on:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

**[docs/encoder.md](docs/encoder.md)** — picking the right CUDA wheels, checking
which device it actually loaded on, and filling the catalogue.

## This project redistributes no card data

The catalogue is fetched from upstream, on your machine, by a plugin you chose.
We ship the code that knows how to ask — not the prices, and not the images.

The one exception is the animation above, which shows a handful of card
thumbnails because a demo of a card tool with no cards in it would be useless.
No card data ships in any form the application reads.

## Contributing

[AGENTS.md](AGENTS.md) has the working notes: how the pieces fit, the
conventions, and the handful of mistakes this codebase has already made once.

```bash
uv sync --extra dev
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
uv run pytest -q
```

Most of the suite needs nothing but Python. `tests/test_isolation.py` builds a
throwaway Postgres database and drives the real app against it — it skips
cleanly if there is no server, so start one before trusting a green run on
anything touching accounts:

```bash
docker compose up -d postgres && uv run pytest -q
```

The pre-push hook runs the suite and refuses a push if anything **skipped**, for
the same reason.

## Supporting this

foilstack is free, AGPL, and costs you nothing to run. If it saves you an
afternoon of typing card names into a spreadsheet:

**[Buy me a coffee](https://buymeacoffee.com/foilstack)**

The link is in the sidebar of every install, self-hosted ones included.

## Licence

[AGPL-3.0](LICENSE). The bundled JetBrains Mono subsets under
`src/foilstack/web/static/fonts/` are licensed separately, under the SIL Open
Font License 1.1 — see `OFL.txt` beside them.

The network clause is the point: if you host this as a service for other people,
those people are entitled to the source, including your changes.

**Running it for yourself or inside your own business triggers nothing.** A shop
using this in the back office owes no one anything. The obligation begins when
you offer it as a service to others.
