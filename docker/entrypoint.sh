#!/usr/bin/env sh
# Apply migrations, then start whatever was asked for.
#
# The deploy runs its own migrations rather than expecting an operator to
# remember: a container that starts against a schema it does not understand
# fails on the first query, which looks like an application bug and is not.
#
# `alembic upgrade head` is a no-op when there is nothing to apply, so this
# costs a second on every restart and saves the one restart where it matters.
set -e

echo "[entrypoint] applying database migrations…"
alembic upgrade head

echo "[entrypoint] starting: $*"
exec "$@"
