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

# The web service holds open connections that would block the DROPs inside a
# --clean dump; stopping it first turns a confusing partial restore into a
# short outage.
docker compose stop web
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

docker compose start web

echo "restored $DUMP into $PG_DB"
