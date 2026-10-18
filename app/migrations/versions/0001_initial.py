"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-18
"""

from __future__ import annotations

from alembic import op

from app import models  # noqa: F401
from app.db import Base


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The M1 schema is intentionally created from the SQLAlchemy metadata.  A
    # later milestone can replace this with granular operations once the model
    # stops moving; the revision id and upgrade path stay valid.
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
