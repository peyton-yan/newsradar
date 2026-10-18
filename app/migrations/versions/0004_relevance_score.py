"""replace macro_relevant with an LLM relevance score

Revision ID: 0004_relevance_score
Revises: 0003_cost_tracking
Create Date: 2026-10-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "0004_relevance_score"
down_revision = "0003_cost_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("analyses")}
    if "relevance" not in existing:
        op.add_column(
            "analyses",
            sa.Column("relevance", sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    with op.batch_alter_table("analyses") as batch:
        batch.drop_column("relevance")
