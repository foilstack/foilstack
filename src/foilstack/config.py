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
    )


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
