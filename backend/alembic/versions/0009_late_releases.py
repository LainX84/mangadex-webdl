"""late releases below the start chapter, same-group re-uploads, version checks

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_OLD = ("missing", "downloaded", "imported", "modified", "missing_on_disk", "skipped", "external")
_NEW = (*_OLD, "replaced")


def _check(values) -> str:
    return "local_status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.add_column("monitoring_configs", sa.Column("start_chapter_set_at", sa.DateTime(timezone=True)))
    # Existing start chapters count from the day the title was added.
    op.execute("""
        UPDATE monitoring_configs c SET start_chapter_set_at = m.created_at
        FROM manga m WHERE m.id = c.manga_id AND c.start_chapter IS NOT NULL
    """)
    op.add_column("chapter_releases", sa.Column("downloaded_version", sa.Integer()))
    # What is on disk now is taken as the current version: no mass re-check.
    op.execute("""
        UPDATE chapter_releases SET downloaded_version = version
        WHERE local_status IN ('downloaded', 'modified', 'imported')
    """)
    op.add_column("download_jobs", sa.Column("is_refresh", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("download_jobs", sa.Column("replaces_release_id", sa.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_download_jobs_replaces_release", "download_jobs", "chapter_releases",
                          ["replaces_release_id"], ["id"], ondelete="SET NULL")
    op.drop_constraint("ck_release_local_status", "chapter_releases", type_="check")
    op.create_check_constraint("ck_release_local_status", "chapter_releases", _check(_NEW))


def downgrade() -> None:
    op.execute("UPDATE chapter_releases SET local_status = 'skipped' WHERE local_status = 'replaced'")
    op.drop_constraint("ck_release_local_status", "chapter_releases", type_="check")
    op.create_check_constraint("ck_release_local_status", "chapter_releases", _check(_OLD))
    op.drop_constraint("fk_download_jobs_replaces_release", "download_jobs", type_="foreignkey")
    op.drop_column("download_jobs", "replaces_release_id")
    op.drop_column("download_jobs", "is_refresh")
    op.drop_column("chapter_releases", "downloaded_version")
    op.drop_column("monitoring_configs", "start_chapter_set_at")
