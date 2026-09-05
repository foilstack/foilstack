"""The dependencies every route shares.

Here rather than in app.py so a route module can take them without importing
the application object and creating a cycle.

Settings are resolved per call rather than bound at import. `get_settings` is
cached, so this costs a dict lookup — and it means `get_settings.cache_clear()`
actually takes effect here. Bound at import, it does not: a module imported
before a fixture points the application at a throwaway database keeps the
first settings object it ever saw, and every test then fails against the
developer's own database with an error that names a password rather than an
ordering. That cost hours, twice.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Query, Request

from foilstack import db, inventory
from foilstack.config import Settings, get_settings
from foilstack.web import auth


def settings_dep() -> Settings:
    """The configuration this request runs under.

    A dependency rather than a module global, which is what let the routes
    below it move out of app.py at all. A global bound at import belongs to
    whichever module imported first, so every route reading it had to live
    beside that binding — and a test wanting different settings had to reach
    into the module and reassign the name, which only worked for routes that
    happened to be in that same module.

    As a dependency it is overridable the ordinary way:

        app.dependency_overrides[settings_dep] = lambda: replace(s, x=1)

    which works for every route regardless of which file it ended up in.
    """
    return get_settings()


def db_session():
    """One session per request, always closed."""
    session = db.session()
    try:
        yield session
    finally:
        session.close()


def owner(request: Request, session=Depends(db_session)) -> db.User:
    """The account this request acts as.

    In single-user mode this is the local owner and never fails. In multi-user
    mode it redirects to the login screen. Every route that touches a seller's
    work depends on it, so there is no way to reach one of those queries
    without an id to scope it by.
    """
    return auth.require_user(request, session, get_settings())


def api_owner(request: Request, session=Depends(db_session)) -> db.User:
    """Same, but answering 401 instead of redirecting.

    A fetch() that follows a 303 to the login page succeeds with an HTML body,
    and the caller reports "saved" for a request that saved nothing.
    """
    user = auth.current_user(request, session, get_settings())
    if user is None:
        raise HTTPException(401, "sign in required")
    return user


@dataclass(frozen=True)
class Selection:
    """What a listing run is over: hand-picked rows, or a filter.

    Three modes, and the mode is always stated rather than inferred. That is
    the whole design here. `/listings` used to read "no ids" as "everything
    the account owns", while the filter the seller had been looking at was not
    on the form — so a screen reading `Not listed 75` handed off a run priced
    over all of it. An absent selection now selects nothing, and breadth is
    something the seller asked for in a parameter that says so.

    * `""`     — the ids given, and only those.
    * `page`   — one page of a filter, resolved through `inventory.narrow`.
    * `all`    — every line the filter matches.

    The two filter modes exist because a selection cannot be carried as ids at
    this size. One page of a hundred lines is around 740 copies, which is a
    6.4 KB querystring — already inside the 8 KB a proxy will commonly accept,
    before a shop with twenty copies of a staple is considered. "All matching"
    would be a quarter of a megabyte. So the filter travels and the ids are
    resolved at the far end.

    The cost is that a filter is resolved against the data as it stands when
    the run is priced, not as it stood when the page was drawn. Between those
    two moments another tab can confirm a scan, and the page then holds
    slightly different lines. Nothing is written by looking, and `/listings`
    states what it resolved and how many, so the drift is visible before any
    of it is acted on — but it is why `sel` carries the sort and the page
    number rather than trusting position alone.
    """

    ids: frozenset[int]
    sel: str
    show: str
    q: str
    picks: dict[str, tuple[str, ...]]
    sort: str
    dir: str
    page: int

    @property
    def by_filter(self) -> bool:
        return self.sel in ("page", "all")

    def wire(self) -> dict[str, list[str] | None]:
        """The picks in the shape `inventory.narrow` validates."""
        return {key: list(values) for key, values in self.picks.items()}

    def query_items(self) -> list[tuple[str, str]]:
        """This selection as querystring pairs, to hand to the next screen.

        The export links on `/listings` carry the selection onward, so they
        have to be able to say the same thing — and an export link that fell
        back to enumerating ids would reintroduce the length ceiling on the
        one URL a browser follows after the run is already priced.
        """
        if not self.by_filter:
            return [("id", str(i)) for i in sorted(self.ids)]
        out = [("sel", self.sel)]
        if self.show and self.show != inventory.DEFAULT_SHOW:
            out.append(("show", self.show))
        if self.q:
            out.append(("q", self.q))
        for key in inventory.FACET_KEYS:
            out.extend((key, v) for v in self.picks.get(key, ()))
        # Only page mode needs an order and a place in it. Carried for `all`
        # they would be noise in the URL that changes nothing about the run.
        if self.sel == "page":
            if self.sort != inventory.DEFAULT_SORT:
                out.append(("sort", self.sort))
            if self.dir != inventory.DEFAULT_DIR:
                out.append(("dir", self.dir))
            if self.page > 1:
                out.append(("page", str(self.page)))
        return out


def selection_dep(
    id: list[int] | None = Query(None),
    sel: str = "",
    show: str = inventory.DEFAULT_SHOW,
    q: str = "",
    sort: str = inventory.DEFAULT_SORT,
    dir: str = inventory.DEFAULT_DIR,
    page: int = 1,
    game: list[str] | None = Query(None),
    # Aliased rather than named `set`, which would shadow the builtin.
    card_set: list[str] | None = Query(None, alias="set"),
    condition: list[str] | None = Query(None),
    printing: list[str] | None = Query(None),
    listed: list[str] | None = Query(None),
) -> Selection:
    """The selection a listing or export route is being asked to act on.

    One dependency, so `/listings`, `/export/{name}` and the TCGplayer round
    trip cannot come to different conclusions about the same querystring —
    they are three steps of one run, and a seller who exports what they were
    shown is relying on exactly that.
    """
    return build_selection(
        ids=id,
        sel=sel,
        show=show,
        q=q,
        sort=sort,
        dir=dir,
        page=page,
        wire={
            "game": game,
            "set": card_set,
            "condition": condition,
            "printing": printing,
            "listed": listed,
        },
    )


def build_selection(
    *,
    ids: list[int] | None = None,
    sel: str = "",
    show: str = inventory.DEFAULT_SHOW,
    q: str = "",
    sort: str = inventory.DEFAULT_SORT,
    dir: str = inventory.DEFAULT_DIR,
    page: int = 1,
    wire: dict[str, list[str] | None] | None = None,
) -> Selection:
    """The same thing over plain values, so the rules can be tested directly.

    `selection_dep` cannot be: called outside a request its defaults are
    FastAPI `Query` objects rather than the values they stand for, so a test
    driving it gets a `TypeError` about iterating a `Query` instead of an
    answer about selections.
    """
    given = wire or {}
    return Selection(
        ids=frozenset(ids or []),
        # Anything else means the ids, which is the narrow answer. A mode that
        # is not one of the two is a hand-edited URL, and reading an unknown
        # word as "everything you own" is the failure this parameter exists to
        # have made impossible.
        sel=sel if sel in ("page", "all") else "",
        show=show,
        q=q,
        picks={key: tuple(given.get(key) or ()) for key in inventory.FACET_KEYS},
        sort=sort,
        dir=dir,
        page=page,
    )
