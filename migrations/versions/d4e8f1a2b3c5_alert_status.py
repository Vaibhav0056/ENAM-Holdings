"""alerts.status (active | superseded) and superseded_by: one alert per company per day

Revision ID: d4e8f1a2b3c5
Revises: c7a1d2e9f310
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e8f1a2b3c5"
down_revision: Union[str, Sequence[str], None] = "c7a1d2e9f310"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("alerts", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    op.add_column("alerts", sa.Column("superseded_by", sa.Integer(), sa.ForeignKey("alerts.id"), nullable=True))
    op.create_index("ix_alerts_status", "alerts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_alerts_status", "alerts")
    op.drop_column("alerts", "superseded_by")
    op.drop_column("alerts", "status")
