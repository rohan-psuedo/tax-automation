from logging.config import fileConfig

import app.models  # noqa: F401 - registers all tables on Base.metadata
from alembic import context
from app.db import Base, engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            # Table rebuilds (batch mode) drop and recreate tables that others reference.
            # Must run outside a transaction to take effect; app connections turn it on.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            # SQLAlchemy auto-began a transaction for the PRAGMA; end it, or Alembic's
            # begin_transaction() below joins it and nothing is ever committed.
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-friendly ALTERs
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
