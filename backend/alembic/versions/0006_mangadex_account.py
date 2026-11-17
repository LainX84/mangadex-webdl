"""MangaDex account: followed-list mirror and manga waiting for approval

Revision ID: 0006
Revises: 0005

* ``manga.pending_approval``: titles added automatically from the MangaDex followed
  list; nothing is scanned or downloaded until the user approves them.
* ``mangadex_follows``: mirror of the followed list (reading status, dismissed titles).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("manga", sa.Column("pending_approval", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("manga", sa.Column("pending_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("manga", sa.Column("added_via", sa.String(length=32), nullable=True))
    op.create_table(
        "mangadex_follows",
        sa.Column("manga_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False),
        sa.Column("reading_status", sa.String(length=32), nullable=True),
        sa.Column("followed", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("unfollowed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("mangadex_follows")
    for col in ("added_via", "pending_since", "pending_approval"):
        op.drop_column("manga", col)
