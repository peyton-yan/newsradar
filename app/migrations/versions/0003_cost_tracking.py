"""add token cache and cost accounting

Revision ID: 0003_cost_tracking
Revises: 0002_simhash_bands
Create Date: 2026-10-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "0003_cost_tracking"
down_revision = "0002_simhash_bands"
branch_labels = None
depends_on = None


BATCH_COLUMNS = {
    "cache_hit_tokens": sa.Integer(),
    "cache_miss_tokens": sa.Integer(),
    "cost_usd": sa.Float(),
    "cost_cny": sa.Float(),
    "is_peak": sa.Boolean(),
}
USAGE_COLUMNS = {
    "cache_hit_tokens": sa.Integer(),
    "cache_miss_tokens": sa.Integer(),
    "cost_usd": sa.Float(),
    "cost_cny": sa.Float(),
    "cost_alert_sent": sa.Boolean(),
}


def _add_columns(table: str, columns: dict[str, sa.types.TypeEngine]) -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns(table)}
    for name, type_ in columns.items():
        if name in existing:
            continue
        op.add_column(
            table,
            sa.Column(name, type_, nullable=False, server_default=sa.text("0")),
        )


def upgrade() -> None:
    _add_columns("analysis_batches", BATCH_COLUMNS)
    _add_columns("daily_usage", USAGE_COLUMNS)


def downgrade() -> None:
    with op.batch_alter_table("analysis_batches") as batch:
        for name in BATCH_COLUMNS:
            batch.drop_column(name)
    with op.batch_alter_table("daily_usage") as batch:
        for name in USAGE_COLUMNS:
            batch.drop_column(name)
