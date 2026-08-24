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
src/foilstack/migrations/  alembic. The schema lives here, not in
                  create_all — and inside the package so a pip install can
                  run `foilstack migrate` and build its own
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
uv run python scripts/preview.py                 # serve at :8099 until Ctrl-C
uv run python scripts/preview.py --shots ./shots # screenshot and exit
```

Either way it is a disposable database, an account already signed in, and a
slice of your catalogue so the screens have real cards in them. It drops the
database on exit and never touches a real deployment. Screenshots land in
`shots/`, which is gitignored.

Then actually open the PNGs. A screenshot you did not look at is worth nothing.

Playwright is a dev dependency; `uv run playwright install chromium` is enough,
and the `--with-deps` step that wants sudo is not needed.

**Re-record the README animation when you change a screen it shows.** It covers
the queue, the inventory table, a card page and the listing run, so a change to
any of those dates it:

```bash
uv run python scripts/preview.py --demo src/foilstack/web/static/demo
```

Same disposable database as `--shots`, so nobody's real inventory is ever on
camera. It writes the committed GIF plus a WebP and a webm that are gitignored —
the webm is the one to post anywhere that takes real video.

Then watch it. Every fault this has had was invisible in the code and obvious in
the output: scrolls that silently moved nothing and vanished from the GIF when
identical frames collapsed, a queue seeded with the same card twice, a price
trend panel reading "no price history" on the one card chosen to show it off.
The size ceiling is the `check-added-large-files` hook at 2048 KB — fit under
it by scrolling less rather than by dropping the frame rate, because a scroll at
five frames a second reads as broken.

## Formatting and types

Install both hook types before your first commit:

```bash
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

On commit: formatting, linting, types, the version bump, and `gitleaks` —
deliberately a hook and not a CI job, because this repository is public and a
credential CI catches is one that has already been pushed and can only be
rotated.

On push: the test suite, which also fails if anything **skipped**. Not on
commit, and that is the whole design — `tests/test_isolation.py` needs Postgres
and skips cleanly without it, so a plain pytest hook on a machine with no
database reports green while the forty tests proving one account cannot read
another's cards did not run. Requiring a database before every commit is
friction nobody keeps; before every push it is the right trade.

`ruff` formats and lints; `mypy` type-checks `src/`. All three run in
pre-commit and in CI, and the settings live in `pyproject.toml` so a local run
and the hook cannot disagree.

```bash
uv run ruff check --fix .   &&  uv run ruff format .  &&  uv run mypy
```

Two settings that look like laziness and are not:

* **B008 is exempted for FastAPI's `Depends`/`Query`/`Form`**, by name rather
  than by switching the rule off. Those are function calls in argument
  defaults on purpose; B006 still catches a real mutable default.
* **mypy is not `--strict`.** Strict reports 126 errors, nearly all "annotate
  this". A number that size gets ignored wholesale rather than fixed, and then
  the check means nothing. Tighten a module at a time as annotations arrive.

`6c12bdd` reformatted the whole codebase and is listed in
`.git-blame-ignore-revs`. Add a revision there only when the commit is
genuinely mechanical.

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

  ```bash
  uv run alembic upgrade head                        # apply
  uv run alembic revision --autogenerate -m "what"   # write one
  uv run alembic downgrade -1                        # step back
  ```
* Nearest-neighbour search runs **in Postgres**, against an HNSW index, rather
  than over a numpy array held in memory. That is what made accounts possible —
  a per-user filter is a `WHERE` clause — and what stops a large catalogue
  reading every vector on every scan.
* Numbers in the interface must be real or visibly marked. The analytics screen
  computes what it can from inventory and labels the rest `demo data`; fees and
  shipping are absent rather than estimated, because foilstack never sees them.
* Nothing posts to a marketplace. "Channels" are CSV formats, and the button
  that records a sale says `Mark sold`, not `Push`.

## Deployment

Deploy with the commit in hand, so the running build can say what it is:

```bash
GIT_SHA="$(git rev-parse --short HEAD)$(git diff --quiet HEAD || echo -dirty)" \
  docker compose up -d --build
curl -sS https://your-host/healthz     # ok / foilstack 0.1.9 (8c2cd47)
```

Commit before you build. The `-dirty` suffix is there because a build from an
uncommitted tree labelled with a clean commit claims code it does not contain,
and that has already happened here: a deploy built before its own commit
reported the commit *before* the fix while running the fix. The version behaves
the same way — the bump hook runs at commit time, so a build made first carries
the previous number.

It catches staged and unstaged edits, not untracked files, which do still reach
the build context. Committing first is the habit; the suffix is the safety net
for when you forget.

**Do not name a service in the build.** `web` and `prices` are separate images
built from the same `Dockerfile.web`, so `docker compose build web` leaves the
price sidecar running whatever code it was last built with — and it fails
quietly, because a sidecar that rejects a game just logs and loops. That is how
a corrected category map sat in `web` for a day while `prices` still refused
`dragonballfusion` as an unknown game. `--build` with no service rebuilds both.

`docker compose up -d` runs `alembic upgrade head` before uvicorn, so a deploy
applies its own migrations. The `backup` service dumps on a schedule and writes
`BACKUP_FAILING` into the backup directory when a run produces nothing usable —
that file is the whole of the monitoring.

`FOILSTACK_MULTI_USER=true` requires a real `FOILSTACK_SECRET_KEY`; the app
refuses to start otherwise, because that key signs session cookies and the
shipped default is public.
