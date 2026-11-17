"""adaptive monitoring levels (update tiers) and upload-complete flag

Revision ID: 0005
Revises: 0004

The feed now also lists chapters removed from MangaDex (DMCA history,
``isUnavailable``). Every title gets one full scan so that history is loaded.
Tiers are computed by the scheduler shortly after start-up.
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("monitoring_configs", sa.Column("update_tier", sa.SmallInteger(), nullable=True))
    op.add_column("monitoring_configs", sa.Column("upload_complete", sa.Boolean(), nullable=False,
                                                  server_default=sa.false()))
    op.add_column("monitoring_configs", sa.Column("last_chapter_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("monitoring_configs", sa.Column("prev_chapter_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("monitoring_configs", sa.Column("tier_computed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE monitoring_configs SET needs_full_scan = true")


def downgrade() -> None:
    for col in ("tier_computed_at", "prev_chapter_at", "last_chapter_at", "upload_complete", "update_tier"):
        op.drop_column("monitoring_configs", col)
