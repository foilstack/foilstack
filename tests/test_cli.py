"""The command-line surface other things depend on.

`docker-compose.yml` runs these commands, so a renamed flag or a changed
default is a production change with no other test standing in front of it.
Nothing here touches a database: it checks the contract, not the work.
"""

from __future__ import annotations

import argparse
import re
import unittest.mock as mock
from pathlib import Path

from foilstack import cli

ROOT = Path(__file__).resolve().parents[1]


def _parse(argv: list[str]) -> argparse.Namespace:
    """Run argv through the real parser, capturing the args the command got.

    The handlers are replaced rather than the parser rebuilt, so this exercises
    the same `main()` the container calls.
    """
    seen: dict = {}

    async def capture(args):
        seen.update(vars(args))
        return 0

    with (
        mock.patch.object(cli, "cmd_sync_prices", capture),
        mock.patch.object(cli, "cmd_embed", capture),
    ):
        assert cli.main(argv) == 0
    return argparse.Namespace(**seen)


def test_sync_prices_defaults_to_every_ingested_game():
    """The default is the whole catalogue, not a list to keep up to date.

    A named list is one someone has to remember to extend after each ingest,
    and forgetting leaves that game holding its ingest-day prices for good
    with nothing anywhere saying so. That happened on the live deployment.
    """
    args = _parse(["sync-prices"])
    assert args.game == "all"


def test_embed_skips_known_missing_images_unless_asked():
    """Upstream's "there is no image here" is permanent, so it is remembered.

    Without the flag every run re-requested thousands of cards that answer
    404, spending the daily request budget to learn nothing.
    """
    assert _parse(["embed"]).retry_missing is False
    assert _parse(["embed", "--retry-missing"]).retry_missing is True


def test_compose_price_sync_calls_a_command_that_exists():
    """The sidecar's command line is not covered by anything else.

    It is a shell loop inside YAML, so a flag renamed in Python fails at 3am
    in a container nobody is watching rather than in CI.
    """
    compose = (ROOT / "docker-compose.yml").read_text()
    invocation = re.search(r"foilstack\.cli sync-prices ([^\n|]*)", compose)
    assert invocation, "the prices service no longer calls sync-prices"
    assert "--game" in invocation.group(1)

    default = re.search(r"SYNC_GAMES: \$\{FOILSTACK_SYNC_GAMES:-([^}]*)\}", compose)
    assert default and default.group(1).strip() == "all"


# ---------------------------------------------------------------------------
# Which upstream answers are permanent, and which are worth another go.
# ---------------------------------------------------------------------------


def test_a_4xx_means_there_is_no_image_and_never_will_be():
    """Promo and staff entries answer 403 or 404 forever. Retrying one four
    times with backoff costs seven seconds to learn nothing, and there are
    thousands of them."""
    assert cli.image_is_permanently_missing(404, b"")
    assert cli.image_is_permanently_missing(403, b"some body")


def test_an_empty_200_is_permanent_too():
    """Found on the live catalogue: `Treecko 016 Target Promo` answers 200 with
    `image/jpeg` and zero bytes.

    It looks like success, so it fell past the 4xx check, past
    `raise_for_status`, and into the encoder — which cannot decode nothing. It
    was then retried four times with backoff, on every run, forever.
    """
    assert cli.image_is_permanently_missing(200, b"")


def test_a_real_image_and_a_server_error_are_not_permanent():
    """The distinction the whole retry policy rests on: a 5xx or a timeout is
    worth another go, and marking it missing would write off a card over a
    blip."""
    assert not cli.image_is_permanently_missing(200, b"\xff\xd8\xff jpeg bytes")
    assert not cli.image_is_permanently_missing(503, b"")
    assert not cli.image_is_permanently_missing(429, b"")
