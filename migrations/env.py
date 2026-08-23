"""Alembic environment.

Two deliberate departures from the generated template:

  * The database URL comes from `foilstack.config`, not from `alembic.ini`.
    The URL carries the password and already lives in the environment; a second
    copy in a committed file is a leak, and worse, a way for migrations to run
    against a different database than the app.
  * `target_metadata` is the application's real metadata, so `--autogenerate`
    works. Importing `foilstack.db` is what registers the models on it.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from foilstack.config import get_settings
from foilstack.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Every model must be imported for autogenerate to see it; they all live in
# `foilstack.db`, so importing Base is enough.
target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", get_settings().database_url)


def render_item(type_, obj, autogen_context):
    """Render pgvector column types with the import they need.

    Autogenerate writes `pgvector.sqlalchemy.halfvec.HALFVEC(dim=1024)` into
    the migration but adds no import for it, so the revision it produces fails
    at `NameError` the first time it runs. Registering the import here fixes
    every future revision rather than every future revision by hand.
    """
    if type_ == "type" and obj.__class__.__module__.startswith("pgvector"):
        autogen_context.imports.add("import pgvector.sqlalchemy")
        return f"pgvector.sqlalchemy.HALFVEC(dim={obj.dim})"
    return False


def include_object(obj, name, type_, reflected, compare_to):
    """Keep autogenerate's hands off the HNSW index.

    It is created in its own revision with tuned build memory and cannot be
    expressed in the model metadata, so autogenerate sees an index Postgres has
    and the models do not, and helpfully proposes dropping it.
    """
    if type_ == "index" and name == "ix_card_embeddings_hnsw":
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it — `alembic upgrade --sql`."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            render_item=render_item,
            # Without these, autogenerate ignores a column whose type or
            # default changed and silently produces an empty migration.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
