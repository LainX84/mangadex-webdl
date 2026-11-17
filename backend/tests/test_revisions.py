"""Revisions by the scanlation group (same-group re-upload, new MangaDex version with new pages).

* chapter still in the library, untouched  -> replaced in place, old copy deleted;
* chapter still in the library, edited     -> replaced in place, old copy kept in the replaced folder;
* chapter already moved out, downloaded within ``revision_after_move_days`` -> new copy saved in the
  replaced folder only, plus a Review item;
* chapter moved out longer ago (or the window set to 0) -> nothing downloaded.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.db import session_scope
from app.enums import JobStatus, LocalStatus
from app.library.marker import read_release_marker
from app.models import ChapterRelease, DownloadJob, ReviewItem
from tests.conftest import REPLACED
from tests.helpers import GROUP_A, add, releases, run_queue, scan, set_settings
from tests.test_api_auth import admin, app_client, authed  # noqa: F401  (fixtures)


def _downloaded(md, library, days_ago: float = 1):
    """One chapter downloaded by the app ``days_ago`` days ago; returns (manga id, chapter, folder)."""
    mid = md.add_manga(title="Revised")
    ch = md.add_chapter(mid, volume="1", chapter="1", groups=[GROUP_A], pages=3,
                        when=datetime.now(UTC) - timedelta(days=days_ago + 1))
    add(md, mid)
    scan(md, mid)
    run_queue(md)
    (rel,) = releases(mid)
    assert rel.local_status == LocalStatus.DOWNLOADED
    with session_scope() as s:  # pretend the download (and the first sight) happened days_ago
        past = datetime.now(UTC) - timedelta(days=days_ago)
        s.execute(update(ChapterRelease).where(ChapterRelease.id == rel.id)
                  .values(downloaded_at=past, first_seen_at=past))
    return mid, ch, library / rel.folder_rel_path


def _reupload(md, mid):
    return md.add_chapter(mid, volume="1", chapter="1", groups=[GROUP_A], pages=4, when=datetime.now(UTC))


def _replaced_dirs():
    return sorted(p for p in REPLACED.rglob("*") if p.is_dir() and (p / ".mdm-release.json").exists()) \
        if REPLACED.exists() else []


def _reviews():
    with session_scope() as s:
        return [(r.kind, r.details.get("revision")) for r in s.scalars(select(ReviewItem))]


def _status(cid):
    with session_scope() as s:
        return s.get(ChapterRelease, uuid.UUID(cid)).local_status


# ------------------------------------------------------------------ still in the library


def test_reupload_replaces_untouched_copy_and_deletes_it(md, library):
    mid, old, folder = _downloaded(md, library)
    new = _reupload(md, mid)
    assert scan(md, mid)["queued"] == 1
    run_queue(md)
    assert read_release_marker(folder)["chapter_id"] == new["id"]
    assert len([p for p in folder.iterdir() if p.suffix == ".jpg"]) == 4
    assert _replaced_dirs() == []  # the untouched old copy was deleted
    assert _status(old["id"]) == LocalStatus.REPLACED and _status(new["id"]) == LocalStatus.DOWNLOADED
    assert _reviews() == []


def test_reupload_keeps_edited_copy_in_replaced_folder(md, library):
    mid, old, folder = _downloaded(md, library)
    next(p for p in folder.iterdir() if p.suffix == ".jpg").unlink()  # ad page removed by hand
    new = _reupload(md, mid)
    scan(md, mid)
    run_queue(md)
    assert read_release_marker(folder)["chapter_id"] == new["id"]
    (kept,) = _replaced_dirs()
    assert read_release_marker(kept)["chapter_id"] == old["id"]
    assert len([p for p in kept.iterdir() if p.suffix == ".jpg"]) == 2


def test_new_version_with_new_pages_replaces_untouched_copy(md, library):
    mid, ch, folder = _downloaded(md, library)
    before = sorted(p.name for p in folder.iterdir())
    md.revise(ch["id"], pages=5)
    assert scan(md, mid)["queued"] == 1
    run_queue(md)
    assert len([p for p in folder.iterdir() if p.suffix == ".jpg"]) == 5
    assert sorted(p.name for p in folder.iterdir()) != before
    assert _replaced_dirs() == []
    assert _status(ch["id"]) == LocalStatus.DOWNLOADED


def test_new_version_with_same_pages_downloads_nothing(md, library):
    mid, ch, folder = _downloaded(md, library)
    ch["attributes"]["version"] = 2  # metadata-only edit
    scan(md, mid)
    run_queue(md)
    with session_scope() as s:
        job = s.scalars(select(DownloadJob).order_by(DownloadJob.id.desc())).first()
        assert job.status == JobStatus.COMPLETED and job.is_refresh
        assert s.get(ChapterRelease, uuid.UUID(ch["id"])).downloaded_version == 2
    assert _replaced_dirs() == []


# ------------------------------------------------------------------ already moved out


def test_whole_title_moved_out_leaves_no_folder_in_library(md, library):
    mid, ch, folder = _downloaded(md, library, days_ago=1)
    shutil.rmtree(folder.parent)  # the whole title folder moved to the PC
    md.revise(ch["id"], pages=4)
    scan(md, mid)
    run_queue(md)
    assert len(_replaced_dirs()) == 1
    assert list(library.iterdir()) == [library / ".mdm-library"]  # no empty title folder recreated


def test_reupload_of_chapter_moved_out_goes_to_replaced_folder(md, library):
    mid, old, folder = _downloaded(md, library, days_ago=3)
    shutil.rmtree(folder)  # moved to the PC to be packed into a volume
    new = _reupload(md, mid)
    assert scan(md, mid)["queued"] == 1
    run_queue(md)
    assert not folder.exists()  # never back in the library
    (saved,) = _replaced_dirs()
    assert read_release_marker(saved)["chapter_id"] == new["id"]
    assert _status(new["id"]) == LocalStatus.IN_REPLACED
    assert _status(old["id"]) == LocalStatus.MISSING_ON_DISK
    assert _reviews() == [("revision_after_move", "reupload")]
    with session_scope() as s:
        item = s.scalars(select(ReviewItem)).one()
        assert item.path == f"Replaced/{saved.relative_to(REPLACED).as_posix()}"
        assert item.details["library_path"] and item.details["groups"] == ["Team X"]


def test_reupload_after_library_scan_still_recognised(md, library):
    """Once a library scan marked the chapter *Folder deleted*, the re-upload is still queued."""
    from app.services.library_scan import run_library_scan

    mid, old, folder = _downloaded(md, library, days_ago=3)
    shutil.rmtree(folder)
    run_library_scan(None, client=md.client)
    assert _status(old["id"]) == LocalStatus.MISSING_ON_DISK
    _reupload(md, mid)
    assert scan(md, mid)["queued"] == 1
    run_queue(md)
    assert len(_replaced_dirs()) == 1 and not folder.exists()
    assert _reviews() == [("revision_after_move", "reupload")]


def test_new_version_of_chapter_moved_out_goes_to_replaced_folder(md, library):
    mid, ch, folder = _downloaded(md, library, days_ago=3)
    shutil.rmtree(folder)
    md.revise(ch["id"], pages=4)
    scan(md, mid)
    run_queue(md)
    assert not folder.exists()
    (saved,) = _replaced_dirs()
    assert len([p for p in saved.iterdir() if p.suffix == ".jpg"]) == 4
    assert _status(ch["id"]) == LocalStatus.MISSING_ON_DISK
    assert _reviews() == [("revision_after_move", "version")]


def test_same_pages_of_chapter_moved_out_change_nothing(md, library):
    mid, ch, folder = _downloaded(md, library, days_ago=3)
    shutil.rmtree(folder)
    ch["attributes"]["version"] = 2
    scan(md, mid)
    run_queue(md)
    assert _replaced_dirs() == [] and _reviews() == []
    assert _status(ch["id"]) == LocalStatus.MISSING_ON_DISK


def test_revision_outside_window_is_not_downloaded(md, library):
    mid, old, folder = _downloaded(md, library, days_ago=20)
    shutil.rmtree(folder)
    new = _reupload(md, mid)
    scan(md, mid)  # the app did not notice the move yet: the job starts and is cancelled
    run_queue(md)
    assert _replaced_dirs() == [] and _reviews() == []
    assert _status(new["id"]) == LocalStatus.SKIPPED
    assert _status(old["id"]) == LocalStatus.MISSING_ON_DISK
    # Known as moved out now: later revisions are not even queued.
    md.revise(old["id"], pages=6)
    assert scan(md, mid)["queued"] == 0


def test_window_zero_disables_revisions_of_moved_out_chapters(md, library):
    set_settings(revision_after_move_days=0)
    mid, ch, folder = _downloaded(md, library, days_ago=1)
    shutil.rmtree(folder)
    md.revise(ch["id"], pages=4)
    scan(md, mid)
    run_queue(md)
    assert _replaced_dirs() == [] and _reviews() == []


def test_dashboard_and_review_show_revisions(md, library, authed):
    mid, old, folder = _downloaded(md, library, days_ago=2)
    shutil.rmtree(folder)
    _reupload(md, mid)
    scan(md, mid)
    run_queue(md)
    d = authed.get("/api/dashboard").json()
    assert d["open_reviews"] == 1 and d["open_revisions"] == 1
    (item,) = authed.get("/api/review").json()
    assert item["kind"] == "revision_after_move" and item["manga"]["title"] == "Revised"
    assert authed.post(f"/api/review/{item['id']}/dismiss").status_code == 200
    assert authed.get("/api/dashboard").json()["open_revisions"] == 0
    assert len(_replaced_dirs()) == 1  # "Seen" never deletes files
