"""policy scope and leave applicability

Revision ID: 153a2d1819e8
Revises: dd6fae5ee332
Create Date: 2026-10-05 21:56:50.334943

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "153a2d1819e8"
down_revision: str | Sequence[str] | None = "dd6fae5ee332"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "documents",
        sa.Column("applies_to", postgresql.ARRAY(sa.String(length=16)), nullable=True),
    )
    op.add_column(
        "employees",
        sa.Column("leave_policy_applicable", sa.Boolean(), server_default="true", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("employees", "leave_policy_applicable")
    op.drop_column("documents", "applies_to")
