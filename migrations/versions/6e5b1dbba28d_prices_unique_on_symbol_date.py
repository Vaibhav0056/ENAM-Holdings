"""prices unique on symbol+date

Index rows (^NSEI) have company_id NULL, and NULLs never collide in a unique constraint, so (company_id, date)
let re-runs duplicate index bars. (symbol, date) is unique for both stocks and indices.

Revision ID: 6e5b1dbba28d
Revises: 1396ff35f08f
"""
from typing import Sequence, Union

from alembic import op

revision: str = "6e5b1dbba28d"
down_revision: Union[str, Sequence[str], None] = "1396ff35f08f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("prices_company_id_date_key", "prices", type_="unique")
    op.create_unique_constraint("uq_prices_symbol_date", "prices", ["symbol", "date"])


def downgrade() -> None:
    op.drop_constraint("uq_prices_symbol_date", "prices", type_="unique")
    op.create_unique_constraint("prices_company_id_date_key", "prices", ["company_id", "date"])
