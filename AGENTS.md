# Working on foilstack

Notes for whoever — or whatever — is editing this repository. The README is for
people deciding whether to run it; this is for people changing it.

## Shape of the thing

Card photographs go in — loose files, or a `.zip` of them. Loose uploads are
packed into an archive in `api_import` before anything else touches them, so
the traversal checks, the expansion ceiling, the duplicate-name suffixing and
the image cap all stay in `extract_archive` with one implementation rather than
two. Each image is encoded by a DINOv3 service and searched against a
catalogue of reference images held in Postgres with pgvector. Confident
matches go straight to inventory; everything else waits in a review queue.
Inventory becomes a CSV the seller uploads to a marketplace themselves.

```
src/foilstack/
  cli.py          ingest, embed, sets, rematch, sync-prices, migrate, plugins
  config.py       nearly every setting, read once from the environment
  db.py           the schema. One row in `inventory` is one physical card
  search.py       nearest-neighbour over card_embeddings (cosine, HNSW)
  importing.py    archive → scans → candidates. Also `scan_path`
  inventory.py    pricing rules, stock lines, totals, export shaping
  prices.py       price history, and the inline SVG that draws it
  images.py       display-sized copies of scans
  embedding.py    the client for the encoder. embedder/ is the service itself
  plugins/        sources (tcgcsv) and exports (the CSV formats)
  web/
    app.py        the application object, the public pages, /healthz
    routes/       accounts, scans, inventory, listings, media
    chrome.py     the Jinja environment, the topbar figures, _asset_version
    deps.py       the dependencies every route shares
    auth.py       accounts, sessions, the single-user escape hatch
    ratelimit.py  per-process counters on the routes strangers can reach
    joblog.py     a short in-memory "did that button do anything", per account
    proof.py      the two catalogue cards the landing page argues with
  migrations/     alembic. The schema lives here, not in create_all — and
                  inside the package so a pip install can run
                  `foilstack migrate` and build its own
scripts/
  preview.py      a throwaway instance, to serve or to photograph
  shots.py        screenshots of a running instance
  demo.py         the README GIF
  landing.py      the landing page stills
  bump_version.py the version bump, as a pre-commit hook
  check_categories.py   every TCGplayer category id against its real name
  check-tests.sh  the suite, on push, failing on a skip
  restore.sh      a backup back into a database
docs/             accounts, backups, the encoder, plugins, prices
```

`app.py` used to be every route and had reached fourteen hundred lines. The
groups that stand alone are routers under `routes/` now, split by the thing
they act on rather than by whether they answer with HTML or JSON — the route
that confirms a scan and the screen offering the button are one decision. What
made that possible was moving settings out of a module global and into
`deps.settings_dep`: a global bound at import belongs to whichever module
imported first, so every route reading it had to live beside that binding.

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
camera. It writes one committed file — the GIF the README shows — plus a
gitignored webm, which is the one to post anywhere that takes real video. It
used to write two animated WebPs as well, for a landing page that played the
animation in its hero; the landing page uses stills now and those were deleted.

**Re-shoot the landing stills when you change a screen one of them shows.** The
same rule, for the same reason, on the other set of images:

```bash
uv run python scripts/preview.py --landing src/foilstack/web/static/shots
```

Four committed files: the review queue (the hero), a card page with its price
trend, the listing run, and `og.png` — which is not a screenshot but a composed
card, because a link preview is rendered about 500px wide in a feed and an
application screen at that size is a grey rectangle.

The stills replaced a 3.0 MB animated WebP that was 94% of the landing page's
weight and the largest thing painted inside the fold. It also carried its own
copy of the application nav bar, so it announced a stale version number in the
middle of the hero for as long as nobody re-recorded it. The whole set is now
about 450 KB, and each still goes out of date on its own rather than all at
once.

Then watch it. Every fault this has had was invisible in the code and obvious in
the output: scrolls that silently moved nothing and vanished from the GIF when
identical frames collapsed, a queue seeded with the same card twice, a price
trend panel reading "no price history" on the one card chosen to show it off.
The size ceiling is not the `check-added-large-files` hook, which is set to
2048 KB and does not apply: it only inspects files being *added*, and the GIF
has been tracked since the beginning. It currently sits at about 4.4 MB and
commits without complaint. The real ceiling is camo, GitHub's image proxy,
which stops serving somewhere near 5 MB — and a README whose only picture
silently fails to load is the failure that matters here. Buy headroom by
scrolling less rather than by dropping the frame rate, because a scroll at five
frames a second reads as broken.

Capture is at `device_scale_factor=2` and the GIF is written down to 1000px at
192 colours. Those are separate numbers on purpose: the browser is asked for
twice the CSS size so the downsample has real detail to work from, and 1000 is
what GitHub's content column actually renders. The palette is close to free —
at 800px, 192 colours came out fractionally *smaller* than 160 — so width is
the whole cost, and stopping at 1000 rather than 1200 is the camo margin again.

The preview seeds runners-up, not just the top match. It used to seed one
candidate per scan, which quietly made the demo argue less than the product
does: `also matched: ...` never rendered on a queue row, and the "wrong card?"
panel opened on a heading promising a choice above a single option.

## Formatting and types

Install both hook types before your first commit:

```bash
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
```

On commit: formatting, linting, types, the version bump, and `gitleaks` —
deliberately a hook and not a CI job, because this repository is public and a
credential CI catches is one that has already been pushed and can only be
rotated.

The bump fires only when something under `src/` is staged — a README fix is not
a new version of the software, and a number that moves for everything means
nothing — and never during a merge, rebase, cherry-pick or revert, which replay
commits that already carry their own version. `uv run python
scripts/bump_version.py --dry-run` says what it would do without touching
anything.

On push: the test suite, which also fails if anything **skipped**. Not on
commit, and that is the whole design — `tests/test_isolation.py` needs Postgres
and skips cleanly without it, so a plain pytest hook on a machine with no
database reports green while the sixty-six tests proving one account cannot
read another's cards did not run. Requiring a database before every commit is
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
* **mypy is not `--strict`.** Strict reports 142 errors, nearly all "annotate
  this". A number that size gets ignored wholesale rather than fixed, and then
  the check means nothing. Tighten a module at a time as annotations arrive.

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
* **A card id is a row number, not a name.** `37` is Base Set Charizard in one
  database and something else entirely in the next, so nothing outside a single
  install may hardcode one. `web/proof.py` finds the landing page's two cards by
  name and set, and shows no thumbnails at all when the catalogue has not been
  ingested — which is the honest state for an install that cannot yet identify
  anything.
* **Scope every read by `user_id`.** Ownership is `NOT NULL` and single-user
  mode is *one account*, not *no account*, so there is one query shape and no
  branch that could forget. `inventory.items()` takes `user_id` positionally on
  purpose — a scoping argument with a default is one a caller can omit.
* **The job log is keyed by account too.** It is in-memory and deliberately
  tiny, but its messages name filenames, SKUs, row counts and export sizes, and
  every one of those is somebody's business data. A single process-wide log
  reads fine on a self-hosted install and hands a hosted seller's activity to
  whoever loads the listings page next.
* **Route declaration order survives into the route table.** FastAPI matches
  routes in the order they are declared and routers in the order they are
  included, so a same-shape pair still depends on which came first. Declare
  `/api/inventory/{item_id}` ahead of `/api/inventory/delete` and a POST to
  `delete` is captured by the id route, `"delete"` fails to parse as an integer,
  and bulk delete answers 422. The extraction out of `app.py` preserved the
  original order for exactly that reason.
* **Settings resolve per call, not at import.** `deps.settings_dep` reads
  `get_settings()` on each request — a cached dict lookup, and the reason
  `get_settings.cache_clear()` takes effect at all. Bound at import it does not:
  a module imported before a fixture repoints the application at a throwaway
  database keeps the first settings object it ever saw, and the suite then fails
  against the developer's own database with an error that names a password
  rather than an ordering. That cost hours, twice.
* **A `NOT NULL` column needs a `server_default` in the migration**, dropped
  afterwards. Autogenerate will not add one, and the migration fails outright on
  a table that already has rows.
* **`flex-wrap: wrap` wraps on `flex-basis`, not `min-width`.** Items only
  shrink after wrapping, within a line.
* **A setting has to be named in three places.** `config.py` reads it, the
  `web` service in `docker-compose.yml` passes it through, and `.env.example`
  documents it — and compose forwards only what that block lists, so a setting
  added to the first and the third but not the second is a knob that silently
  does nothing on the one install path the README describes. The two settings
  that live outside `config.py` are easier still to miss: `MAX_IMAGES` in
  `importing.py` and `FOILSTACK_EMBED_CONCURRENCY` in `cli.py` are module
  constants read at import, and the first of them is printed on the import
  screen as a promise to the seller.
* **Bump `_asset_version()` inputs when you add a static file.** It hashes the
  files it knows about; one it does not know about ships behind a stale query
  string, and the deploy then looks like it did nothing. `tests/test_assets.py`
  now walks the templates for `?v=` and fails on any asset missing from the
  hash — it found two that had been wrong since the beginning.
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
* **A wrong TCGplayer category id is invisible.** It is a real category
  returning a real catalogue of real cards — from a different game. Three of the
  eleven ids here were wrong for months: `gundam` fetched hololive,
  `dragonball` fetched Neopets Battledome, `finalfantasy` fetched Godzilla, and
  the comment above them saying they had been read from the categories endpoint
  rather than guessed is how they went unexamined. `scripts/check_categories.py`
  checks every id against the name upstream gives it. It is deliberately not in
  the suite — it needs the network, and a test that fails because upstream is
  down cries wolf. Run it when you add a game.
* **The wheel has to contain more than Python.** Templates, static files and
  migrations are none of them code, and any of them can quietly stop being
  packaged; a release that imports fine and 500s on its first page is worse than
  one that fails to build, so `release.yml` unzips the wheel and looks. The
  static files are derived from the templates rather than listed, because a
  hand-kept list only holds what somebody remembered — `app.js` was missing from
  it for a whole release, and a missing script is the quiet kind of broken too:
  every page renders and every button is dead.
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
* **Front end: no build step, no framework.** Behaviour shared by more than one
  screen belongs in `static/app.js`, which exposes `$`, `$$`, `postJSON`,
  `postForm` and `wireCardSearch` as plain globals. A page's own script goes in
  a `scripts` block, which `base.html` emits *after* `app.js`; a script written
  inline in a screen's markup instead runs before the helpers exist. Redeclaring
  one of those names in a page script is a `SyntaxError`, not a shadow — two
  top-level `const`s of one name in classic scripts collide.
* **A route module must not import `app.py`.** That is a cycle, and it is what
  kept every route in one file. What the shell offers is in `web/chrome.py` —
  the Jinja environment and its filters, the topbar figures, `_asset_version` —
  and what a request needs is in `web/deps.py`. `foilstack.web` has no
  `__init__.py`, so a constant two of those modules share is defined in the one
  whose job it is rather than hoisted into the package.
* Jinja parses its tags inside HTML comments, so an `<!-- -->` comment that
  names a block opens one, and the template dies at the next `endblock`. Use a
  `{# ... #}` comment when the text needs to mention a tag.

## Deployment

Deploy with the commit in hand, so the running build can say what it is:

```bash
GIT_SHA="$(git rev-parse --short HEAD)$(git diff --quiet HEAD || echo -dirty)" \
  docker compose up -d --build
curl -sS https://your-host/healthz     # ok / foilstack 0.2.14 (a439f79)
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
