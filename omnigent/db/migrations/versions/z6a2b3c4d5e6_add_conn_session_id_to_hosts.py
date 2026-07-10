"""add conn_session_id to hosts

Revision ID: z6a2b3c4d5e6
Revises: z5a2b3c4d5e6
Create Date: 2026-07-02 00:00:00.000000

Adds ``hosts.conn_session_id`` as a nullable ownership token for live
host tunnel connections. Disconnect cleanup can compare this token so a
stale tunnel cannot mark a newer replacement connection offline.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z6a2b3c4d5e6"
down_revision: str | None = "z5a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable host connection-session ownership token."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("conn_session_id", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the host connection-session ownership token."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("conn_session_id")
