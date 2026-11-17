"""external chapters (JAM import) and one-release-per-chapter

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_OLD = "('missing', 'downloaded', 'imported', 'modified', 'missing_on_disk', 'skipped')"
_NEW = "('missing', 'downloaded', 'imported', 'modified', 'missing_on_disk', 'skipped', 'external')"


def upgrade() -> None:
    op.add_column(
        "monitoring_configs",
        sa.Column("one_release_per_chapter", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "external_chapters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("manga_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("manga.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="jam"),
        sa.Column("release_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("language_code", sa.String(16), nullable=True),
        sa.Column("chapter", sa.String(32), nullable=True),
        sa.Column("chapter_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("manga_id", "source", "release_id", name="uq_external_chapter_release"),
    )
    op.create_index("ix_external_chapters_manga_id", "external_chapters", ["manga_id"])
    op.create_index("ix_external_chapters_release_id", "external_chapters", ["release_id"])
    op.drop_constraint("ck_release_local_status", "chapter_releases", type_="check")
    op.create_check_constraint("ck_release_local_status", "chapter_releases", f"local_status IN {_NEW}")


def downgrade() -> None:
    op.execute("UPDATE chapter_releases SET local_status = 'skipped' WHERE local_status = 'external'")
    op.drop_constraint("ck_release_local_status", "chapter_releases", type_="check")
    op.create_check_constraint("ck_release_local_status", "chapter_releases", f"local_status IN {_OLD}")
    op.drop_index("ix_external_chapters_release_id", "external_chapters")
    op.drop_index("ix_external_chapters_manga_id", "external_chapters")
    op.drop_table("external_chapters")
    op.drop_column("monitoring_configs", "one_release_per_chapter")
