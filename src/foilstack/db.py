"""Postgres schema.

Postgres and pgvector rather than SQLite and a numpy index. The numpy index was
the right call while this was one person's laptop — it removed a server and a
compiler from the list of things a self-hoster had to get working — but it
cannot survive two things this now has to do at once:

  * **Accounts.** Every scan, job and inventory row belongs to somebody, and a
    hosted deployment must never let one seller see another's cards. That is a
    join and a `WHERE user_id = :me` on every query, which is a database's job.
  * **A catalogue that outgrows memory.** The numpy index reads every vector on
    every search. At 233k cards that is 477 MB per query.

Ownership is deliberately *not* nullable, and single-user mode does not mean
"no owner" — it means one implicit account that nobody has to log into. So
there is exactly one query shape in this codebase, and no branch anywhere that
could forget to scope a read.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from foilstack.config import EMBEDDING_DIM


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class User(Base):
    """One account.

    Exists even when `FOILSTACK_MULTI_USER` is false: a self-hosted install has
    a single row here that it never asks anyone to log into. Keeping the row
    means ownership is a foreign key rather than a nullable special case, and
    turning multi-user on later is a setting rather than a migration.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Stored lowercased — see `web.auth.normalise_email`. Two accounts that
    # differ only by capitalisation are the same person to everyone except the
    # login form, and that mismatch is a support ticket about a lost inventory.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Card(Base):
    """One printing, as supplied by a source plugin.

    Shared across accounts on purpose. The catalogue is public reference data
    fetched from upstream — what a card *is* does not differ per seller, and
    duplicating 200k rows per account to pretend otherwise would be absurd.
    Ownership begins at the scan.

    `source_id` is namespaced by the plugin that produced it (`tcgcsv:12345`).
    Two plugins covering overlapping games would otherwise collide on integer
    ids from different upstreams and silently overwrite each other.
    """

    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # The same card, spelled the way the source spells it.
    #
    # `name` is upstream's cleaned form — punctuation stripped, so
    # "Ancestor's Chosen" is stored as "Ancestors Chosen". That is the right
    # thing to search and display against, and the wrong thing to *join* on:
    # TCGplayer's own CSVs carry the raw spelling, and matching a listing file
    # on the cleaned one silently loses roughly one card in ten — every
    # apostrophe, colon and bracket in the catalogue. Nullable because a
    # catalogue ingested before this column existed has no answer, and a
    # guessed one is the bug this column exists to avoid.
    source_name: Mapped[str | None] = mapped_column(Text)
    game: Mapped[str] = mapped_column(String(64), nullable=False)
    set_name: Mapped[str | None] = mapped_column(Text)
    number: Mapped[str | None] = mapped_column(String(32))
    variant: Mapped[str | None] = mapped_column(String(64))
    image_url: Mapped[str | None] = mapped_column(Text)
    # When upstream last answered 4xx for `image_url`.
    #
    # A catalogue this size is full of promo, staff and placeholder entries
    # that have a URL and no image behind it — permanently. `embed` used to
    # count those and forget them, so every subsequent run re-requested all of
    # them to be told the same thing: ~2,900 pointless CDN requests per run on
    # a 147k catalogue, growing with it. Recording the answer makes the second
    # run cheap and keeps the daily request budget for cards that might work.
    image_missing_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    market: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


Index("ix_cards_name", Card.name)
Index("ix_cards_game", Card.game)


class CardEmbedding(Base):
    """The encoded reference image for one printing.

    `halfvec` rather than `vector`: two bytes per dimension instead of four,
    which halves both the table and the HNSW index built over it. The vectors
    are L2-normalised and compared by cosine, so fp16 is not the precision that
    will limit recall.

    `model` is not decoration. Vectors from two different encoders are not
    comparable and a mixed table returns confident nonsense rather than an
    error, so the column exists to make a half-finished model swap a query
    instead of a mystery.
    """

    __tablename__ = "card_embeddings"

    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[Any] = mapped_column(HALFVEC(EMBEDDING_DIM), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_now, nullable=False
    )

    card: Mapped[Card] = relationship()


class CardPrice(Base):
    """The current prices for one printing of one card.

    Keyed by sub-type, not just by card. Normal, Foil and Reverse Holofoil are
    the same artwork at wildly different money — collapsing them into a single
    `market` on the card, which is what this replaces, priced every foil in the
    catalogue as if it were the plain printing.

    `low` is not decoration either: the "Low + $0.01" pricing rule was an
    invented multiplier off market until this column existed, because the
    catalogue had no lowest-listing figure to undercut.

    Overwritten by every sync. `CardPriceHistory` is what remembers.
    """

    __tablename__ = "card_prices"

    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), primary_key=True
    )
    sub_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    market: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    mid: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_now, nullable=False
    )


class CardPriceHistory(Base):
    """One observation of a printing's price, kept forever.

    This is the only table here whose contents cannot be reconstructed. TCGCSV
    mirrors *the current day* — there is no historical endpoint — so a day that
    goes unrecorded is gone for that card permanently, at any price. Everything
    else in this database can be rebuilt by running `ingest` again.

    **Rows are written only when a price changes**, and on first sight. A card
    that has not moved in a month has one row, not thirty.

    That storage choice has a consequence worth stating loudly, because it is a
    quiet source of wrong answers: this is a change log, not a daily snapshot.
    `WHERE recorded_on = '2026-08-23'` returns the printings that *moved* that
    day, not the prices in effect that day. To value a card on a date, take the
    most recent row at or before it:

        SELECT DISTINCT ON (card_id, sub_type) *
          FROM card_price_history
         WHERE card_id = :id AND recorded_on <= :on
         ORDER BY card_id, sub_type, recorded_on DESC

    `recorded_on` is a date rather than a timestamp so that a second sync in
    one day overwrites that day's reading instead of appending a duplicate.
    """

    __tablename__ = "card_price_history"

    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), primary_key=True
    )
    sub_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    recorded_on: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    market: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    mid: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)


Index("ix_price_history_card", CardPriceHistory.card_id, CardPriceHistory.recorded_on)


class SyncState(Base):
    """What each source was last successfully synced against.

    Holds upstream's own build timestamp, not ours. TCGCSV publishes
    `last-updated.txt` and asks that a full sync run only when it is newer than
    your last pull — comparing our clock to theirs would re-sync every run or
    none of them, depending on drift.
    """

    __tablename__ = "sync_state"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    upstream_stamp: Mapped[str | None] = mapped_column(Text)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    rows_changed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)


class ImportJob(Base):
    """One archive, and the settings it was matched under.

    `auto_accept` and `default_condition` are stored per job rather than read
    from the environment at match time. They are chosen on the import screen,
    and a threshold that only exists in a config file is one nobody adjusts —
    but it also has to stay attached to the job afterwards, because "why did
    this scan auto-accept" is unanswerable if the setting has since changed.
    """

    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    message: Mapped[str | None] = mapped_column(Text)
    auto_accept: Mapped[float | None] = mapped_column(Float)
    default_condition: Mapped[str] = mapped_column(String(16), default="NM")
    default_finish: Mapped[str] = mapped_column(String(16), default="nonfoil")
    # "Every card in this batch is from one game" / "…from one set". An
    # assertion the seller makes about the physical stack, which the matcher
    # cannot make for itself: nothing in a photograph says whether the pile it
    # came from was a booster box or a shoebox. See `importing.apply_cohort`.
    same_game: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    same_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # What that assertion resolved to, once the batch had been matched. Kept
    # for the same reason `auto_accept` is: "why is this scan pointing at a
    # card the encoder ranked fourth" is unanswerable without it, and the
    # answer stops existing the moment the next import overwrites it.
    cohort_game: Mapped[str | None] = mapped_column(Text)
    cohort_set: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    scans: Mapped[list[Scan]] = relationship(back_populates="job")


class Scan(Base):
    """One image from an archive, and what we think it is.

    `status` is the review state: `pending` (waiting on a human), `confirmed`
    (accepted, in inventory) or `discarded`. Auto-accepted scans land straight
    in `confirmed` but keep their candidate list, so a threshold set too low
    stays auditable after the fact.

    `stored_path` is relative to the scans directory, never absolute. An
    absolute path is a property of whichever process ran the import: the
    compose file mounts `./data` at `/data`, so a row written by the CLI on the
    host recorded a path the container could not resolve, and every thumbnail
    404'd. See `importing.scan_path`.

    `user_id` is carried here as well as on the job. It is denormalised on
    purpose: the review queue and every image route filter on the scan alone,
    and a join through `import_jobs` to discover the owner is one more place a
    query could be written without it.
    """

    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("import_jobs.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    # The image's size on disk, recorded at import. Summing this per account is
    # what makes a storage quota answerable in a query rather than by walking
    # the filesystem — and it falls as scans are discarded, which a running
    # total kept on the job would not.
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    auto_accepted: Mapped[int] = mapped_column(Integer, default=0)
    # The nearest catalogue card's similarity, kept even when it was too low to
    # accept as a match. Without it, "the tool found nothing" and "the tool
    # nearly found it" are the same row, and the match floor cannot be tuned
    # from data.
    best_score: Mapped[float | None] = mapped_column(Float)
    # The card a person picked when the encoder's ranking was wrong.
    #
    # Separate from `candidates` rather than reordering them: the candidate
    # list is what the encoder said, and rewriting it to record a human
    # decision destroys the evidence of how the miss happened. This is a
    # different claim — not "the nearest neighbour" but "the seller looked at
    # the card and says it is this one" — so it is a different column.
    #
    # `SET NULL` on delete because a re-ingested catalogue can drop rows. A
    # choice pointing at a card that no longer exists should fall back to the
    # encoder's guesses, not break the queue.
    chosen_card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id", ondelete="SET NULL"))
    # The card the batch put here, when the encoder's first answer came from
    # outside the game or set the rest of the batch agreed on.
    #
    # A third column rather than a reuse of either of the other two, because it
    # is a third kind of claim. `candidates` is what the encoder saw in this
    # one image; `chosen_card_id` is what a person holding the card says it is;
    # this is neither — it is what the other four hundred scans imply about
    # this one. Folding it into `chosen_card_id` would tell the queue a human
    # had decided something no human has looked at, and reordering the
    # candidates would erase the ranking that is the evidence for the move.
    #
    # `SET NULL` on delete for the same reason as above: a re-ingested
    # catalogue can drop the row, and that has to fall back to the encoder's
    # guesses rather than break the queue.
    cohort_card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped[ImportJob] = relationship(back_populates="scans")
    chosen_card: Mapped[Card | None] = relationship(foreign_keys=[chosen_card_id])
    cohort_card: Mapped[Card | None] = relationship(foreign_keys=[cohort_card_id])
    candidates: Mapped[list[Candidate]] = relationship(
        back_populates="scan",
        cascade="all, delete-orphan",
        order_by="Candidate.rank",
    )


Index("ix_scans_status", Scan.status)
Index("ix_scans_user_status", Scan.user_id, Scan.status)


class Candidate(Base):
    """A ranked guess at what a scan shows.

    Kept even after a scan is confirmed. Reprints share artwork, so the
    runners-up are usually the *same card in a different printing* — which is
    exactly the mistake that produces a plausible, wrong price. Discarding the
    list would discard the evidence that a match was close-run.
    """

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"), nullable=False)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)

    scan: Mapped[Scan] = relationship(back_populates="candidates")
    card: Mapped[Card] = relationship()


class InventoryItem(Base):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"), nullable=False)
    scan_id: Mapped[int | None] = mapped_column(ForeignKey("scans.id", ondelete="SET NULL"))
    condition: Mapped[str] = mapped_column(String(16), default="NM")
    # Foil or not, declared by the seller rather than inferred.
    #
    # Image search cannot settle this and should not be asked to: a foil and a
    # non-foil printing share artwork exactly, differ under the light rather
    # than in the scan, and differ in price by multiples. It is the same
    # judgement as condition — the person holding the card knows, the encoder
    # is guessing, and a wrong guess prices a card at another card's value.
    finish: Mapped[str] = mapped_column(String(16), nullable=False, default="nonfoil")
    # The printing the seller named, when they have. `finish` is the coarse
    # answer they can give for a whole batch; this is the exact one, and it is
    # what pricing uses when present.
    #
    # Both exist because they answer different questions. "Foil or not" is
    # something you can say once for 200 cards on the import screen. "1st
    # Edition Holofoil or Unlimited Holofoil" — $10,000 or $2,146 for the same
    # Charizard — is a per-card answer nobody can give in bulk, and guessing it
    # is what this column exists to stop.
    sub_type: Mapped[str | None] = mapped_column(String(64))
    # There is deliberately no `quantity`. One row is one physical card — that
    # is what a scan is evidence of, and what carries this row's cost, notes
    # and sale. A quantity column beside a single scan was a second and
    # contradictory answer to "how many cards is this", and in practice it was
    # never anything but 1. Stock lines are grouped for display; see
    # `inventory.groups`.
    cost: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    # `stock` or `sold`. Sold rows stay in the table rather than being deleted:
    # they are what makes realised profit and sell-through answerable at all,
    # and a row removed on sale takes its cost basis with it.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="stock")
    sold_price: Mapped[float | None] = mapped_column(Float)
    sold_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    listed: Mapped[int] = mapped_column(Integer, default=0)
    # Which marketplaces this row has been marked as listed on, and when.
    # Recorded here rather than inferred from an export file, because an
    # export is a file you may or may not have uploaded — this is the seller
    # telling us they did.
    listed_channels: Mapped[str | None] = mapped_column(Text)
    listed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    card: Mapped[Card] = relationship()


Index("ix_inventory_user_status", InventoryItem.user_id, InventoryItem.status)
# One row is one physical card, and one scan is one physical card — so a scan
# may be spoken for exactly once. Partial, because `scan_id` goes NULL when a
# scan is deleted and any number of rows may have lost theirs.
Index(
    "uq_inventory_scan_id",
    InventoryItem.scan_id,
    unique=True,
    postgresql_where=InventoryItem.scan_id.isnot(None),
)


_engine = None
_Session = None


def init(database_url: str | None = None):
    """Open the connection pool.

    Creates no tables: the schema belongs to Alembic, and `create_all` beside
    migrations is how a database ends up in a state no migration describes.
    The container runs `alembic upgrade head` before the app starts.
    """
    global _engine, _Session
    if database_url is None:
        from foilstack.config import get_settings

        database_url = get_settings().database_url
    _engine = create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,  # a pooled connection outlives a postgres restart
        # Sized against the threadpool that will ask for them, not against a
        # guess. FastAPI runs every `def` route in a pool forty threads wide,
        # so a default of five plus ten overflow is a ceiling those threads
        # reach on any page that loads a lot of images — and reaching it means
        # thirty seconds of waiting on every other route, `/healthz` with them.
        pool_size=20,
        max_overflow=30,
        # Fail in five seconds rather than thirty. A request that cannot get a
        # connection is not going to be saved by waiting half a minute; it is
        # going to hold a worker while the queue behind it grows.
        pool_timeout=5,
        # Postgres drops idle connections eventually; recycling first means the
        # application never hands out one that the server has already closed.
        pool_recycle=1800,
    )
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _Session


def engine():
    if _engine is None:
        raise RuntimeError("db.init() has not been called")
    return _engine


def session():
    if _Session is None:
        raise RuntimeError("db.init() has not been called")
    return _Session()
