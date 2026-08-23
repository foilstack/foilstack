# Working on foilstack

Notes for whoever — or whatever — is editing this repository. The README is for
people deciding whether to run it; this is for people changing it.

## Shape of the thing

A `.zip` of card photographs goes in. Each image is encoded by a DINOv3 service
and searched against a catalogue of reference images held in Postgres with
pgvector. Confident matches go straight to inventory; everything else waits in a
review queue. Inventory becomes a CSV the seller uploads to a marketplace
themselves.

```
src/foilstack/
  cli.py          ingest / embed / sets / rematch / plugins
  config.py       every setting, read once from the environment
  db.py           the schema. One row in `inventory` is one physical card
  search.py       nearest-neighbour over card_embeddings (cosine, HNSW)
  importing.py    archive → scans → candidates. Also `scan_path`
  inventory.py    pricing rules, stock lines, totals, export shaping
  images.py       display-sized copies of scans
  web/app.py      every route
  web/auth.py     accounts, sessions, the single-user escape hatch
migrations/       alembic. The schema lives here, not in create_all
scripts/          preview.py (throwaway instance), shots.py, restore.sh
```

## Before you say it works

**Look at the page.** Several bugs have shipped here that no test would have
caught and a glance caught instantly: a reference image rendering at its natural
672×936 because a `<span>` was `display: inline`; the import screen showing its
progress tiles and Start button before a file was chosen, because `[hidden]`
loses to `display: grid`; a "low confidence" warning on a 99% match; queue rows
wrapping at 1280 but not 1440.

```bash
uv run python scripts/preview.py --shots ./shots
```

Then actually open the PNGs. A screenshot you did not look at is worth nothing.

Playwright is a dev dependency; `uv run playwright install chromium` is enough,
and the `--with-deps` step that wants sudo is not needed.

## Tests

```bash
docker compose up -d postgres && uv run pytest -q
```

`tests/test_isolation.py` builds its own database and drives the real
application. It **skips** when Postgres is unreachable, so a green run proves
nothing about account scoping unless the server was up — check that the skip
count is zero before trusting it. That has already caught one credential change
that silently disabled twelve security tests.

Two habits worth keeping:

* **Assert on behaviour, not markup.** A test matching `"<tr data-row>"` broke
  the moment the row gained a class, and the failure said nothing useful.
* **Make tests self-contained.** Three tests once leaned on a fixture row that
  earlier tests in the same module edited; they passed alone and failed in
  sequence, which is the least useful way for a test to fail.

## Things that have bitten

* **Never store an absolute path in the database.** `scans.stored_path` is
  relative to the scans directory. The compose file mounts `./data` at `/data`,
  so a row written by the CLI on the host was unreadable from the container and
  every thumbnail 404'd. Same rule for derived files: display copies are keyed
  by the scan's path, not its row id, because a row id is unique in one database
  and a data directory can be shared with another.
* **Scope every read by `user_id`.** Ownership is `NOT NULL` and single-user
  mode is *one account*, not *no account*, so there is one query shape and no
  branch that could forget. `inventory.items()` takes `user_id` positionally on
  purpose — a scoping argument with a default is one a caller can omit.
* **A `NOT NULL` column needs a `server_default` in the migration**, dropped
  afterwards. Autogenerate will not add one, and the migration fails outright on
  a table that already has rows.
* **`flex-wrap: wrap` wraps on `flex-basis`, not `min-width`.** Items only
  shrink after wrapping, within a line.
* **Bump `_asset_version()` inputs when you add a static file.** It hashes the
  files it knows about; one it does not know about ships behind a stale query
  string.
* **`finish` and `sub_type` are both on `inventory`, deliberately.** "Foil or
  not" is answerable once for a whole batch on the import screen; "1st Edition
  Holofoil or Holofoil" — $10,000 or $855 for the same Charizard — is not. The
  coarse one drives bulk entry and the fallback guess; the precise one, once a
  person sets it, is what pricing uses. Anything that drops `sub_type` on an
  edit silently restores a guess the seller had already corrected.
* **Price history cannot be backfilled.** TCGCSV mirrors the current day only.
  Anything that stops `sync-prices` running costs history permanently, so treat
  a broken sync as data loss rather than a stale cache.
* **`sync_state` is keyed per source *and* game.** It was keyed per source
  once, and a successful Magic sync made every other catalogue believe its own
  first run was already up to date.
* **Respect upstream's guidelines.** TCGCSV asks for a custom User-Agent, 100ms
  between requests, and at most one full pull per daily rebuild — checked via
  `last-updated.txt`. Exceeding it earns a throttle, then a ban. It is a free
  service mirroring data we would otherwise have to buy.
* **The encoder's vectors are numpy.** `search.as_literal` forces `float()`
  because NumPy 2 renders `np.float32(-0.02)` in list repr, which Postgres
  cannot parse as a `halfvec`.

## Conventions

* Comments explain **why**, and are worth writing where the reason is not
  recoverable from the code — a threshold, a trade-off, a bug that is now a
  rule. Do not narrate what the next line does.
* Schema changes are Alembic revisions. `create_all` beside migrations is how a
  database reaches a state no migration describes.
* Numbers in the interface must be real or visibly marked. The analytics screen
  computes what it can from inventory and labels the rest `demo data`; fees and
  shipping are absent rather than estimated, because foilstack never sees them.
* Nothing posts to a marketplace. "Channels" are CSV formats, and the button
  that records a sale says `Mark sold`, not `Push`.

## Deployment

`docker compose up -d` runs `alembic upgrade head` before uvicorn, so a deploy
applies its own migrations. The `backup` service dumps on a schedule and writes
`BACKUP_FAILING` into the backup directory when a run produces nothing usable —
that file is the whole of the monitoring.

`FOILSTACK_MULTI_USER=true` requires a real `FOILSTACK_SECRET_KEY`; the app
refuses to start otherwise, because that key signs session cookies and the
shipped default is public.
