"""volume CBZ files built by the app

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "volume_archives",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("manga_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manga.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("language_code", sa.String(length=16), nullable=False),
        sa.Column("volume", sa.String(length=32), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("release_ids", JSON, nullable=False),
        sa.Column("chapters", sa.Integer(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("automatic", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("manga_id", "language_code", "volume", name="uq_volume_archive"),
    )
    op.create_index("ix_volume_archives_manga_id", "volume_archives", ["manga_id"])


def downgrade() -> None:
    op.drop_index("ix_volume_archives_manga_id", table_name="volume_archives")
    op.drop_table("volume_archives")
