"""per-manga folder layout (language / group levels only when needed)

Revision ID: 0004
Revises: 0003

Titles that already have chapters in the library keep the full
{Language}/{Group} layout they were downloaded with; every other title starts flat.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_FLAT = """'{"language": false, "groups": []}'"""


def upgrade() -> None:
    op.add_column(
        "manga",
        sa.Column("folder_layout", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                  nullable=False,
                  server_default=sa.text(_FLAT)),
    )
    op.execute("""
        UPDATE manga m SET folder_layout = jsonb_build_object(
            'language', true,
            'groups', COALESCE((
                SELECT jsonb_agg(DISTINCT r.language_code)
                FROM chapter_releases r LEFT JOIN download_jobs j ON j.release_id = r.id
                WHERE r.manga_id = m.id
                  AND (r.folder_rel_path IS NOT NULL OR j.temp_dir_rel IS NOT NULL OR j.target_rel_path IS NOT NULL)
            ), '[]'::jsonb))
        WHERE EXISTS (
            SELECT 1 FROM chapter_releases r LEFT JOIN download_jobs j ON j.release_id = r.id
            WHERE r.manga_id = m.id
              AND (r.folder_rel_path IS NOT NULL OR j.temp_dir_rel IS NOT NULL OR j.target_rel_path IS NOT NULL))
    """)


def downgrade() -> None:
    op.drop_column("manga", "folder_layout")
