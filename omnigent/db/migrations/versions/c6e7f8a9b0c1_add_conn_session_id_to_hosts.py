"""add conn_session_id to hosts

Revision ID: c6e7f8a9b0c1
Revises: b3c4d5e6f7a8
Create Date: 2026-07-28 00:00:00.000000

Adds ``hosts.conn_session_id`` as a nullable ownership token for live
host tunnel connections. Disconnect cleanup compares this token so a
stale tunnel cannot mark a newer replacement connection offline.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6e7f8a9b0c1"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable host connection-session ownership token."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("conn_session_id", sa.String(32), nullable=True))


def downgrade() -> None:
    """Drop the host connection-session ownership token."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("conn_session_id")
