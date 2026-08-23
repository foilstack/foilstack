# Foilstack

Open-source card scanning, inventory and listing export. Drop in a `.zip` of
card scans, get back an identified, priced inventory and a CSV your marketplace
will accept.

Runs on your own machine. **Your scans never leave the host.**

> Status: early. The matching pipeline works end to end; expect rough edges.

![The review queue: each scan beside the catalogue card it matched, with its
confidence, condition and finish](docs/review-queue.png)

*The review queue. Your scan on the left, the catalogue's guess beside it, and
the score between them — because the top match is evidence, not an answer.*

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

That starts Postgres, the encoder and the web app. Database migrations run
automatically on every start, so an upgrade is `git pull && docker compose up -d
--build` and nothing else.

Then build a catalogue. Start small — one game, a few hundred cards — to check
the whole path works before committing hours to encoding:

```bash
docker compose exec web foilstack ingest --source tcgcsv --game pokemon --limit 300
docker compose exec web foilstack embed
```

`foilstack plugins` lists the games a source can fetch. **Ingest every game you
intend to scan.** Nearest-neighbour search can only answer with a card that is
in the catalogue, so scanning Magic against a Pokemon-only catalogue returns
Pokemon — see the note on the review queue below.

Open <http://localhost:8090>.

> **The default encoder is a gated model.** Accept the terms for
> [DINOv3](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m) and
> put an `HF_TOKEN` in your `.env` before the first run, or the encoder fails to
> load with a 401 that looks like a network problem.

`ingest` pulls catalogue rows and image URLs from the source. `embed` downloads
each reference image and encodes it. They are separate commands because they
fail for different reasons and take very different amounts of time — a network
blip during encoding should not cost you the ingest.

## Accounts, and not having one

**Self-hosted, there is no login screen.** `FOILSTACK_MULTI_USER` is off by
default: one implicit owner holds every scan, job and inventory row, and you
never create a password for a tool only you can reach.

Turn it on for a deployment other people can use:

```bash
FOILSTACK_MULTI_USER=true
FOILSTACK_SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')
```

Then everyone signs in with an email and a password, and every query is scoped
to the account that made it — one seller can never see, export or modify
another's cards. The app refuses to start multi-user while `FOILSTACK_SECRET_KEY`
is still the shipped default, because that key signs session cookies and a
published one lets anybody mint a session for any account.

The scoping is not a convention to be careful about. Ownership is a `NOT NULL`
column, single-user mode is *one account* rather than *no account*, and there is
exactly one query shape in the codebase — so there is no branch that could
forget. `tests/test_isolation.py` drives the real application against a real
Postgres and asserts a stranger gets nothing from every route that touches a
seller's work.

## Backups

The database is the one thing here that cannot be rebuilt. Containers, model
weights and the card catalogue are all reproducible; a seller's reviewed matches
and inventory are hours of somebody's work.

Backups are a **service**, not a cron job you have to remember:

```yaml
docker compose up -d          # the `backup` sidecar starts with everything else
```

It dumps whenever the newest dump is older than `FOILSTACK_BACKUP_INTERVAL`
(default 24h), verifies the gzip, rejects a suspiciously small file, keeps the
last `FOILSTACK_BACKUP_KEEP` (default 14) and writes `BACKUP_FAILING` into the
backup directory when a run produces nothing usable. Checking for that file is
the whole of your monitoring.

Dumps land in `FOILSTACK_BACKUP_DIR` on the host — a bind mount, not a named
volume, because a backup that `docker compose down -v` can destroy is not a
backup. Copy them off the machine as well; a backup that only exists on the box
it protects is not a backup either.

To restore:

```bash
scripts/restore.sh ~/backups/foilstack/foilstack-latest.sql.gz
```

Scans live on disk under `FOILSTACK_DATA_DIR`, not in the database. Back that
directory up too, or accept that a restore gives you an inventory with no
thumbnails.

## Database

Postgres with [pgvector](https://github.com/pgvector/pgvector). Nearest-neighbour
search over reference images runs in the database against an HNSW index rather
than in a numpy array held in memory — which is what made accounts possible, and
what stops a large catalogue reading every vector on every scan.

Schema changes are Alembic migrations:

```bash
uv run alembic upgrade head                        # apply
uv run alembic revision --autogenerate -m "what"   # write one
uv run alembic downgrade -1                        # step back
```

## Prices

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

**Price history is the one thing here that cannot be rebuilt.** Upstream mirrors
the current day — there is no historical endpoint — so a day this does not run
is a day of history gone permanently, for every card, at any price. Everything
else can be recreated by running `ingest` again.

History is a **change log, not a daily snapshot**: a row is written only when a
number moves, so a card that has not changed in a month has one row rather than
thirty. That matters when you query it — `WHERE recorded_on = '2026-08-23'`
returns the printings that *moved* that day, not the prices in effect. To value
a card on a date, take the most recent row at or before it.

Prices are stored per printing, so a foil is priced as a foil.

Foil is a coarse answer, though, and some cards have several printings that all
answer to it — Base Set Charizard is 1st Edition Holofoil at \$10,000, Unlimited
Holofoil at \$2,146 and Holofoil at \$855. Ticking "foil" chooses between none of
them, so **name the printing**: open a copy on the card page and pick from the
list, which shows each printing's current price beside it.

Until you do, the dearest matching printing is used and the row is marked
`guessed`. That direction is deliberate — an overpriced card sits unsold and you
notice, an underpriced one sells immediately and you find out from the payout.

## Plugins

**Source plugins** supply the catalogue. They run code and reach the network, so
nothing is installed automatically and there is no registry — you add one
deliberately. `tcgcsv` ships as the reference implementation, and it is a plugin
rather than a special case on purpose: if the primary data source cannot be
expressed through the interface every other plugin uses, the interface is wrong.

**Export plugins** are TOML column mappings, not code:

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

## This project redistributes no card data

The catalogue is fetched from upstream, on your machine, by a plugin you chose.
We ship the code that knows how to ask — not the prices, and not the images.

The one exception is the screenshot above, which shows a handful of card
thumbnails because a screenshot of a card tool with no cards in it would be
useless. No card data ships in any form the application reads.

## Contributing

[AGENTS.md](AGENTS.md) has the working notes: how the pieces fit, the
conventions, and the handful of mistakes this codebase has already made once.

Run the tests:

```bash
uv sync --extra dev && uv run pytest -q
```

Most of the suite needs nothing but Python. `tests/test_isolation.py` builds a
throwaway Postgres database, migrates it, and drives the real app against it —
it skips cleanly if there is no server, so start one before trusting a green run
on anything touching accounts:

```bash
docker compose up -d postgres && uv run pytest -q
```

### Running the UI

`scripts/preview.py` brings up a throwaway instance with sample data: a
disposable database, an account already signed in, and a slice of your
catalogue so the screens have real cards in them. It drops the database on
exit and never touches a real deployment.

```bash
uv run playwright install chromium          # once
uv run python scripts/preview.py            # serve at :8099 until Ctrl-C
uv run python scripts/preview.py --shots ./shots
```

Screenshots land in `shots/`, which is gitignored. The one in this README lives
in `docs/`.

Install the pre-commit hooks before your first commit. They run `gitleaks`,
because this repository is public and a credential pushed here is compromised
the moment it lands:

```bash
pre-commit install
```

## Supporting this

foilstack is free, AGPL, and costs you nothing to run. If it saves you an
afternoon of typing card names into a spreadsheet:

**[Buy me a coffee](https://buymeacoffee.com/foilstack)**

The link is in the sidebar of every install, self-hosted ones included.

## Licence

[AGPL-3.0](LICENSE).

The bundled JetBrains Mono subsets under
`src/foilstack/web/static/fonts/` are licensed separately, under the SIL Open
Font License 1.1 — see `OFL.txt` beside them.

The network clause is the point: if you host this as a service for other people,
those people are entitled to the source, including your changes.

**Running it for yourself or inside your own business triggers nothing.** A shop
using this in the back office owes no one anything. The obligation begins when
you offer it as a service to others.
