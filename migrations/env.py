"""Alembic env: URL comes from .env (never alembic.ini). Migrations use Neon's direct (unpooled) endpoint."""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from finn.config import database_url, migration_url  # noqa: E402
from finn.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata

# Created by hand in the initial migration and deliberately not mapped (queried with SQL): tsvector columns and
# the GIN/HNSW search indexes. Without this, autogenerate proposes dropping them.
UNMAPPED_COLUMNS = {"tsv"}
UNMAPPED_INDEXES = {"ix_pages_tsv", "ix_chunks_tsv", "ix_filings_tsv", "ix_chunks_embedding"}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "column" and name in UNMAPPED_COLUMNS and reflected:
        return False
    if type_ == "index" and name in UNMAPPED_INDEXES:
        return False
    return True


def run_migrations_offline():
    context.configure(url=migration_url() or database_url(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = create_engine(migration_url() or database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                          include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
