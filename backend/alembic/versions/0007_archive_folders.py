"""read-only comics archive index (newest owned chapter suggestions)

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "archive_folders",
        sa.Column("rel_path", sa.Text(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_norm", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("files", JSON, nullable=False),
        sa.Column("files_hash", sa.String(length=64), nullable=False),
        sa.Column("analysis", JSON, nullable=True),
        sa.Column("custom_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("indexed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_archive_folders_title_norm", "archive_folders", ["title_norm"])


def downgrade() -> None:
    op.drop_index("ix_archive_folders_title_norm", table_name="archive_folders")
    op.drop_table("archive_folders")
