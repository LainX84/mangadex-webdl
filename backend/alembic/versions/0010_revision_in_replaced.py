"""revisions of chapters moved out of the library, downloaded into the replaced folder

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_OLD = ("missing", "downloaded", "imported", "modified", "missing_on_disk", "skipped", "external", "replaced")
_NEW = (*_OLD, "in_replaced")


def _check(values) -> str:
    return "local_status IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_release_local_status", "chapter_releases", type_="check")
    op.create_check_constraint("ck_release_local_status", "chapter_releases", _check(_NEW))


def downgrade() -> None:
    op.execute("UPDATE chapter_releases SET local_status = 'replaced' WHERE local_status = 'in_replaced'")
    op.drop_constraint("ck_release_local_status", "chapter_releases", type_="check")
    op.create_check_constraint("ck_release_local_status", "chapter_releases", _check(_OLD))
