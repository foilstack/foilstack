"""Accounts: hashing, scoping, and the two things that must never regress —
a password that verifies against the wrong hash, and a form that tells a
stranger who has an account here."""

import pytest

from foilstack.web import auth


def test_password_round_trips():
    stored = auth.hash_password("a reasonably long passphrase")
    assert auth.verify_password(stored, "a reasonably long passphrase")
    assert not auth.verify_password(stored, "a reasonably long passphras")


def test_hashes_are_salted():
    """Two accounts with the same password must not share a hash."""
    assert auth.hash_password("identical passphrase") != auth.hash_password("identical passphrase")


def test_garbage_hash_is_a_failed_login_not_a_crash():
    """The local owner's hash is '!', which is not a valid argon2 hash.

    It exists so that row can never be logged into. If that raised instead of
    returning False, switching a single-user database to multi-user would 500
    on the first login attempt.
    """
    assert not auth.verify_password("!", "anything")
    assert not auth.verify_password("", "anything")


def test_email_is_normalised():
    assert auth.normalise_email("  Foo@Example.COM ") == "foo@example.com"


def test_secret_check_blocks_multi_user_with_the_shipped_key():
    from foilstack.config import Settings

    def make(multi_user, secret):
        return Settings(
            data_dir=".",
            database_url="",
            embedder_url="",
            embed_model="",
            auto_accept=0.9,
            auto_accept_margin=0.04,
            max_archive_mb=1,
            multi_user=multi_user,
            secret_key=secret,
            support_url="",
        )

    # Single-user never asks for a cookie to be trustworthy, so the default is fine.
    auth.check_secret(make(False, auth.INSECURE_SECRET))
    auth.check_secret(make(True, "a-real-random-secret"))
    with pytest.raises(RuntimeError, match="FOILSTACK_SECRET_KEY"):
        auth.check_secret(make(True, auth.INSECURE_SECRET))
