# Accounts

Who can reach a foilstack deployment, and what stops one seller seeing another's
cards. Most of this only matters once other people can reach the address; a
self-hoster running it for themselves can stop after the first section.

## Not having one

`FOILSTACK_MULTI_USER` is off by default. One implicit owner holds every scan,
job and inventory row, and you never create a password for a tool only you can
reach.

## Turning it on

```bash
FOILSTACK_MULTI_USER=true
FOILSTACK_SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')
```

Everyone then signs in with an email and a password, and every query is scoped
to the account that made it. The app refuses to start multi-user while
`FOILSTACK_SECRET_KEY` is still the shipped default, because that key signs
session cookies and a published one lets anybody mint a session for any account.

The scoping is not a convention to be careful about. Ownership is a `NOT NULL`
column, single-user mode is *one account* rather than *no account*, and there is
exactly one query shape in the codebase — so there is no branch that could
forget. `tests/test_isolation.py` drives the real application against a real
Postgres and asserts a stranger gets nothing from every route that touches a
seller's work.

## Deciding who gets in

Registration is open by default. On a deployment strangers can reach, three
settings decide how open:

```bash
FOILSTACK_ALLOW_REGISTRATION=false   # stop new accounts; existing ones keep working
FOILSTACK_INVITE_CODE=some-secret    # or stay open, but ask for a code
FOILSTACK_MAX_ACCOUNT_MB=2048        # cap the disk one account's scans may take
```

Turning registration off is the lever for the day a public deployment attracts
the wrong attention — it leaves everybody already using the site alone, which
taking the site down does not. The quota is off by default (`0`), because a
self-hoster should not have to configure a limit against themselves.

## Rate limiting

Sign-ins are limited per account **and** per address, both of which must allow
an attempt through: spoofing a forwarded address buys a fresh address budget,
not a fresh budget against the seller being targeted. Ten failures in fifteen
minutes by default (`FOILSTACK_LOGIN_ATTEMPTS`, `FOILSTACK_LOGIN_WINDOW_S`); a
successful sign-in clears both.

The counters live in the process, so adding uvicorn workers multiplies the
effective limit.
