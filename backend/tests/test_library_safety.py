"""Library sentinel (also outside the library folder) and the weekly library check."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_config
from app.db import session_scope
from app.library.storage import StorageUnavailable, check_library, initialize_library, sentinel_path
from app.models import LibraryScanRun
from app.services.library_check import check_due, last_slot, next_slot
from app.settings_service import AppSettings
from tests.helpers import set_settings
from tests.test_api_auth import admin, app_client, authed  # noqa: F401  (fixtures)

ROME = timezone(timedelta(hours=1))


@pytest.fixture
def parent_sentinel(library, monkeypatch):
    """Sentinel configured in the folder above the library (like /mangadex in production)."""
    monkeypatch.setattr(get_config(), "sentinel_dir", library.parent)
    return library.parent / get_config().library_sentinel


# ------------------------------------------------------------------ sentinel


def test_library_without_sentinel_is_unavailable(library):
    (library / ".mdm-library").unlink()
    st = check_library(library)
    assert not st.ok and not st.sentinel_present and "not found" in st.error


def test_init_moves_sentinel_to_configured_folder(library, parent_sentinel):
    legacy = library / ".mdm-library"
    assert check_library(library).ok  # the old location is still accepted
    assert initialize_library(library).ok
    assert parent_sentinel.is_file() and not legacy.exists()
    assert sentinel_path(library) == parent_sentinel


def test_moving_whole_library_content_keeps_library_available(library, parent_sentinel, tmp_path):
    initialize_library(library)
    (library / "Some Manga" / "Volume 01 Chapter 01").mkdir(parents=True)
    for entry in list(library.iterdir()):  # "select all" in a file manager, hidden files included
        shutil.move(str(entry), tmp_path / entry.name)
    assert list(library.iterdir()) == []
    assert check_library(library).ok


def test_missing_sentinel_folder_is_unavailable(library, parent_sentinel):
    (library / ".mdm-library").unlink()
    st = check_library(library)
    assert not st.ok and str(parent_sentinel) in st.error


def test_sentinel_on_other_filesystem_is_refused(library, monkeypatch):
    other = "/dev/shm"
    if not os.path.isdir(other) or os.stat(other).st_dev == os.stat(library).st_dev:
        pytest.skip("no second filesystem available")
    monkeypatch.setattr(get_config(), "sentinel_dir", other)
    with pytest.raises(StorageUnavailable, match="same filesystem"):
        initialize_library(library)


# ------------------------------------------------------------------ weekly check schedule


def _s(weekday=6, hour=5):
    return AppSettings(library_check_weekday=weekday, library_check_hour=hour)


def test_slots_follow_weekday_and_hour():
    sunday_6am = datetime(2026, 11, 15, 6, 0, tzinfo=ROME)
    assert last_slot(_s(), sunday_6am) == datetime(2026, 11, 15, 5, 0, tzinfo=ROME)
    assert next_slot(_s(), sunday_6am) == datetime(2026, 11, 22, 5, 0, tzinfo=ROME)
    sunday_4am = datetime(2026, 11, 15, 4, 0, tzinfo=ROME)
    assert last_slot(_s(), sunday_4am) == datetime(2026, 11, 8, 5, 0, tzinfo=ROME)
    wednesday = datetime(2026, 11, 18, 12, 0, tzinfo=ROME)
    assert last_slot(_s(weekday=0, hour=23), wednesday) == datetime(2026, 11, 16, 23, 0, tzinfo=ROME)
    assert last_slot(_s(weekday=-1), wednesday) is None and next_slot(_s(weekday=-1), wednesday) is None


def _scan_at(when):
    with session_scope() as s:
        s.add(LibraryScanRun(manga_id=None, status="success", started_at=when, finished_at=when))


def test_check_due_once_per_week(db_env):
    slot = datetime(2026, 11, 15, 5, 0, tzinfo=ROME)
    assert check_due_at(slot + timedelta(minutes=1))
    _scan_at(slot - timedelta(days=2))  # a scan before the slot does not count
    assert check_due_at(slot + timedelta(minutes=1))
    _scan_at(slot + timedelta(minutes=1))
    assert not check_due_at(slot + timedelta(minutes=2))
    assert not check_due_at(slot + timedelta(days=3))


def test_check_catches_up_the_same_day_only(db_env):
    slot = datetime(2026, 11, 15, 5, 0, tzinfo=ROME)
    assert check_due_at(slot + timedelta(hours=11))
    assert not check_due_at(slot + timedelta(hours=13))


def test_check_can_be_switched_off(db_env):
    set_settings(library_check_weekday=-1)
    assert not check_due_at(datetime(2026, 11, 15, 5, 30, tzinfo=ROME))


def check_due_at(now):
    with session_scope() as s:
        return check_due(s, now)


def test_library_check_api(authed):
    body = authed.get("/api/library/check").json()
    assert body["enabled"] is True and body["weekday"] == 6 and body["hour"] == 5
    assert body["next_run"] and body["last_run"] is None
    nxt = datetime.fromisoformat(body["next_run"])
    assert nxt.weekday() == 6 and nxt.hour == 5
