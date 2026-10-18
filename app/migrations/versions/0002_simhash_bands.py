"""add simhash LSH bands

Revision ID: 0002_simhash_bands
Revises: 0001_initial
Create Date: 2026-10-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "0002_simhash_bands"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("news_items")}
    indexes = {index["name"] for index in inspector.get_indexes("news_items")}
    for index in range(4):
        name = f"simhash_band{index}"
        if name not in columns:
            op.add_column("news_items", sa.Column(name, sa.Integer(), nullable=True))
        if f"ix_news_items_{name}" not in indexes:
            op.create_index(f"ix_news_items_{name}", "news_items", [name], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("news_items")}
    indexes = {index["name"] for index in inspector.get_indexes("news_items")}
    for index in range(4):
        name = f"simhash_band{index}"
        if f"ix_news_items_{name}" in indexes:
            op.drop_index(f"ix_news_items_{name}", table_name="news_items")
    with op.batch_alter_table("news_items") as batch:
        for index in range(4):
            name = f"simhash_band{index}"
            if name in columns:
                batch.drop_column(name)
