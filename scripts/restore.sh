#!/usr/bin/env bash
# Restore a foilstack dump. The other half of the `backup` service, and the
# half that is never exercised until it matters — so it is a script rather than
# a paragraph in a README that turns out to be wrong.
#
#   scripts/restore.sh ~/backups/foilstack/foilstack-latest.sql.gz
#
set -euo pipefail

DUMP="${1:?usage: restore.sh <dump.sql.gz> [scan-mirror-dir]}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# shellcheck disable=SC1091
[[ -f .env ]] && set -a && source .env && set +a
PG_USER="${POSTGRES_USER:-foilstack}"
PG_DB="${POSTGRES_DB:-foilstack}"
DATA_DIR="${FOILSTACK_DATA_DIR:-./data}"
# Where the backup sidecar mirrors scans to. Restoring the database without
# these gives you an inventory whose every image is a broken link: the rows
# come back, the JPEGs they point at do not.
SCANS_SRC="${2:-$(dirname "$DUMP")/scans}"

[[ -f "$DUMP" ]] || { echo "no such dump: $DUMP" >&2; exit 2; }
gzip -t "$DUMP" || { echo "dump is corrupt: $DUMP" >&2; exit 1; }

echo "This REPLACES the contents of database '$PG_DB'."
read -r -p "Type the database name to confirm: " CONFIRM
[[ "$CONFIRM" == "$PG_DB" ]] || { echo "aborted"; exit 1; }

# Every writer, not just the obvious one. `web` holds open connections that
# would block the DROPs inside a --clean dump, and `prices` is a second writer
# that nothing here would otherwise stop: it wakes on its own schedule, and a
# sync landing in the middle of a restore writes price rows across the
# boundary, into a database that is halfway to being some earlier day's.
WRITERS="web prices"

# Whatever happens next, the site comes back. Without this, `set -e` turns any
# failure after the stop above — a corrupt dump, a psql error, a full disk —
# into an outage that lasts until somebody notices the site is down and works
# out why, which is exactly the moment nobody is at their best.
restart_writers() {
    local status=$?
    echo "starting $WRITERS"
    # shellcheck disable=SC2086
    docker compose start $WRITERS || true
    if (( status != 0 )); then
        echo "restore FAILED (exit $status) — services restarted, database may be partial" >&2
    fi
}
trap restart_writers EXIT

# shellcheck disable=SC2086
docker compose stop $WRITERS
gunzip -c "$DUMP" | docker compose exec -T postgres \
    psql --username "$PG_USER" --dbname "$PG_DB" -v ON_ERROR_STOP=1

# Scans go back before the app does, so it never serves a page referencing an
# image that has not landed yet. `-n` so a file already present wins: if this
# is a partial restore onto a live data directory, the newer copy is the one
# on disk, not the one in the mirror.
if [[ -d "$SCANS_SRC" ]]; then
    mkdir -p "$DATA_DIR/scans"
    cp -an "$SCANS_SRC/." "$DATA_DIR/scans/" 2>/dev/null || true
    echo "restored $(find "$DATA_DIR/scans" -type f | wc -l) scan files from $SCANS_SRC"
else
    echo "WARNING: no scan mirror at $SCANS_SRC — the database will come back" >&2
    echo "         with inventory rows whose images are missing." >&2
fi

echo "restored $DUMP into $PG_DB"
