#!/usr/bin/env bash
# The test suite, with the one extra question CI also asks: did anything skip?
#
# Runs on push rather than on commit, deliberately. `tests/test_isolation.py`
# needs a Postgres server and skips cleanly without one — so a plain `pytest`
# hook on a machine with no database reports green while the tests that prove
# one account cannot read another's cards did not run at all. That is worse
# than no hook. Requiring a running database before every *commit* is too much
# friction to survive; before every *push* it is the right trade, and push is
# the last moment the code is still only yours.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

out="$(uv run pytest -q --no-header -rs 2>&1)"
status=$?

if [[ $status -ne 0 ]]; then
    echo "$out"
    echo
    echo "pre-push: tests failed — nothing pushed." >&2
    exit 1
fi

if grep -qE '[0-9]+ skipped' <<<"$out"; then
    echo "$out" | grep -E 'SKIPPED|skipped' || true
    echo
    echo "pre-push: tests were SKIPPED, which is not the same as passing." >&2
    echo "          The account-isolation suite skips without Postgres. Start it:" >&2
    echo "              docker compose up -d postgres" >&2
    echo "          or push with --no-verify if you know why they are skipping." >&2
    exit 1
fi

echo "$out" | tail -1
