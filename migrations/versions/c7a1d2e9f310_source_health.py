"""source health (circuit breakers, per-source freshness); companies.status

Revision ID: c7a1d2e9f310
Revises: b0e71db43a6c
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7a1d2e9f310"
down_revision: Union[str, Sequence[str], None] = "b0e71db43a6c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("status", sa.String(16), nullable=False, server_default="ok"))
    op.create_table(
        "source_health",
        sa.Column("source", sa.String(40), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_until", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_added", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("source_health")
    op.drop_column("companies", "status")
