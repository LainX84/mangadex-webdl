"""Shared fixtures.

The tests need a PostgreSQL database and a Redis instance that may be wiped:

    export MDM_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/mdm_test
    export MDM_TEST_REDIS_URL=redis://localhost:6379/15

Without them every test that needs the database is skipped (pure unit tests still run).
The library, staging and replaced folders live in a temporary directory that is reset
before each test; MangaDex is the in-memory ``tests.helpers.FakeMangaDex``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="mdm-test-"))
LIBRARY = _TMP / "MangaDex"
STAGING = _TMP / "Incoming"
REPLACED = _TMP / "Replaced"
DB_URL = os.environ.get("MDM_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("MDM_TEST_REDIS_URL")

# Must be set before app.config is first used (get_config is cached).
os.environ.update({
    "MDM_DOWNLOADS_ROOT": str(LIBRARY),
    "MDM_STAGING_ROOT": str(STAGING),
    "MDM_REPLACED_ROOT": str(REPLACED),
    "MDM_SENTINEL_DIR": "",
    "MDM_DATA_DIR": str(_TMP / "data"),
    "MDM_ARCHIVE_ROOT": str(_TMP / "archive"),
    "MDM_EXPECTED_FS_TYPES": "",
    "MDM_COOKIE_SECURE": "false",
    "MDM_LOG_JSON": "false",
    "MDM_LOG_LEVEL": "WARNING",
})
if DB_URL:
    os.environ["MDM_DATABASE_URL"] = DB_URL
if REDIS_URL:
    os.environ["MDM_REDIS_URL"] = REDIS_URL


def _reset_folders() -> None:
    from app.config import get_config

    for p in (LIBRARY, STAGING, REPLACED, _TMP / "archive"):
        shutil.rmtree(p, ignore_errors=True)
    (_TMP / get_config().library_sentinel).unlink(missing_ok=True)  # sentinel above the library
    LIBRARY.mkdir(parents=True)
    (_TMP / "data").mkdir(exist_ok=True)
    (LIBRARY / get_config().library_sentinel).write_text("{}")


@pytest.fixture(scope="session")
def _schema():
    if not DB_URL or not REDIS_URL:
        pytest.skip("MDM_TEST_DATABASE_URL / MDM_TEST_REDIS_URL not set")
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from app.cli import _alembic_ini
    from app.db import session_scope
    from app.languages import seed_languages

    command.upgrade(AlembicConfig(_alembic_ini()), "head")
    with session_scope() as s:
        seed_languages(s)
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture
def db_env(_schema):
    """Empty database (languages kept), empty Redis, fresh library folders, fast settings."""
    from sqlalchemy import text

    from app import settings_service
    from app.db import Base, session_scope
    from app.redis_client import get_redis

    tables = [t.name for t in Base.metadata.sorted_tables if t.name != "languages"]
    with session_scope() as s:
        s.execute(text("TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE"))
    get_redis().flushdb()
    settings_service.invalidate_cache()
    import app.api.clients as api_clients
    import app.ratelimit as ratelimit

    api_clients._client = None
    ratelimit._limiter = None
    _reset_folders()
    from tests.helpers import set_settings

    # MD@Home reports would only add background traffic.
    set_settings(report_to_mangadex_at_home=False)
    yield
    settings_service.invalidate_cache()


@pytest.fixture
def library(db_env) -> Path:
    return LIBRARY


@pytest.fixture
def md(db_env):
    from tests.helpers import FakeMangaDex

    return FakeMangaDex()
