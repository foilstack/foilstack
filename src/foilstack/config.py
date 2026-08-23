"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# The pooled width of DINOv3 ViT-L/16, and therefore the width of the
# `halfvec` column. A constant rather than a setting because it is not
# independently choosable: it has to match the encoder, and changing it means a
# migration and a re-embed, not a restart.
EMBEDDING_DIM = 1024


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    embedder_url: str
    embed_model: str
    auto_accept: float
    auto_accept_margin: float
    max_archive_mb: int
    multi_user: bool
    secret_key: str
    support_url: str
    # Defaults here, unlike the fields above, so that adding a setting does not
    # break every place a Settings is constructed by hand. `get_settings` always
    # passes all of them; the defaults are for tests and for callers that only
    # care about one thing.
    allow_registration: bool = True
    invite_code: str = ""
    max_account_mb: int = 0
    login_attempts: int = 10
    login_window_s: int = 900
    git_sha: str = ""

    @property
    def scans_dir(self) -> Path:
        return self.data_dir / "scans"

    @property
    def refs_dir(self) -> Path:
        return self.data_dir / "refs"

    @property
    def display_dir(self) -> Path:
        """Browser-sized copies of scans. Rebuildable from the originals, so
        this is a cache rather than data — safe to delete at any time."""
        return self.data_dir / "display"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.getenv("FOILSTACK_DATA_DIR", "./data")).expanduser().resolve()
    return Settings(
        data_dir=data_dir,
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://foilstack:foilstack@localhost:5434/foilstack",
        ),
        embedder_url=os.getenv("EMBEDDER_URL", "http://127.0.0.1:8100"),
        embed_model=os.getenv("EMBED_MODEL", "facebook/dinov3-vitl16-pretrain-lvd1689m"),
        # Deliberately high. The cost of a missed auto-accept is one click; the
        # cost of a wrong one is a real card listed at another card's price.
        auto_accept=float(os.getenv("FOILSTACK_AUTO_ACCEPT", "0.94")),
        # How far the top match must beat the runner-up. Without this a 0.932
        # top score auto-accepts over a 0.927 rival that is a different card.
        auto_accept_margin=float(os.getenv("FOILSTACK_AUTO_ACCEPT_MARGIN", "0.04")),
        max_archive_mb=int(os.getenv("FOILSTACK_MAX_ARCHIVE_MB", "512")),
        # Off by default, which is what a self-hoster wants: one person running
        # this for their own shop should not have to invent a password to reach
        # their own inventory. The hosted deployment sets it true, and then
        # every scan, job and inventory row belongs to exactly one account.
        multi_user=_flag("FOILSTACK_MULTI_USER", False),
        # Signs the session cookie. A fixed development default so `docker
        # compose up` works out of the box; `web.auth` refuses to start in
        # multi-user mode while it is still this value, because a published
        # signing key means anyone can mint a session for any account.
        secret_key=os.getenv("FOILSTACK_SECRET_KEY", "dev-insecure-change-me"),
        support_url=os.getenv("FOILSTACK_SUPPORT_URL", "https://buymeacoffee.com/foilstack"),
        # The lever for the day a public deployment attracts the wrong
        # attention. Turning it off leaves every existing account working and
        # simply stops new ones, which is what you want at 2am — the
        # alternative, taking the site down, punishes the people already using
        # it for the behaviour of somebody who is not.
        allow_registration=_flag("FOILSTACK_ALLOW_REGISTRATION", True),
        # The middle setting between "anyone" and "nobody": registration stays
        # open but needs a code you handed out. Empty means no code is asked
        # for. Compared against the submitted value in constant time.
        invite_code=os.getenv("FOILSTACK_INVITE_CODE", ""),
        # A ceiling on the scans one account may keep. Zero means no ceiling,
        # which is the sensible default for a self-hosted install where the
        # only account is the person who owns the disk. A deployment strangers
        # can register on wants a number here, because otherwise the amount of
        # disk one account may consume is decided by that account.
        max_account_mb=int(os.getenv("FOILSTACK_MAX_ACCOUNT_MB", "0")),
        # Failed sign-ins allowed per account and per address before the form
        # starts refusing. Ten is generous for a human who has forgotten which
        # password they used and ruinous for a script working through a list.
        login_attempts=int(os.getenv("FOILSTACK_LOGIN_ATTEMPTS", "10")),
        login_window_s=int(os.getenv("FOILSTACK_LOGIN_WINDOW_S", "900")),
        # Which commit is running. Baked into the image at build time; read
        # from the checkout when running straight from a clone.
        git_sha=os.getenv("FOILSTACK_GIT_SHA") or _git_sha_from_checkout(),
    )


def _git_sha_from_checkout(root: Path | None = None) -> str:
    """The current commit, read from `.git` — no subprocess.

    Only useful when running from a clone; a container has no `.git`, which is
    why the image bakes the value in instead. Reading the files directly rather
    than shelling out to `git` keeps this off the startup path of a machine
    that has no git installed, and cannot hang.
    """
    root = root or Path(__file__).resolve().parents[2]
    head = root / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
    except OSError:
        return ""
    if ref.startswith("ref: "):
        target = root / ".git" / ref[5:]
        try:
            ref = target.read_text().strip()
        except OSError:
            # A packed ref, which lives in packed-refs rather than as a file.
            packed = root / ".git" / "packed-refs"
            name = ref[5:]
            try:
                for line in packed.read_text().splitlines():
                    if line.endswith(f" {name}"):
                        ref = line.split(" ", 1)[0]
                        break
                else:
                    return ""
            except OSError:
                return ""
    return ref[:7] if len(ref) >= 7 else ""


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
