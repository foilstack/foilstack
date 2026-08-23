#!/usr/bin/env bash
# Restore a foilstack dump. The other half of the `backup` service, and the
# half that is never exercised until it matters — so it is a script rather than
# a paragraph in a README that turns out to be wrong.
#
#   scripts/restore.sh ~/backups/foilstack/foilstack-latest.sql.gz
#
set -euo pipefail

DUMP="${1:?usage: restore.sh <dump.sql.gz>}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# shellcheck disable=SC1091
[[ -f .env ]] && set -a && source .env && set +a
PG_USER="${POSTGRES_USER:-foilstack}"
PG_DB="${POSTGRES_DB:-foilstack}"

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
docker compose start web

echo "restored $DUMP into $PG_DB"
