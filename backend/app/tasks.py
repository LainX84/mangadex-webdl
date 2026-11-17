"""RQ task entry points (thin wrappers; the logic lives in app.services)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db import session_scope
from app.logging_setup import get_logger
from app.models import Manga
from app.redis_client import get_redis

log = get_logger(__name__)

LIBSCAN_PENDING_KEY = "mdm:libscan_pending"


def task_download(job_id: int) -> str:
    from app.services.downloader import run_download

    return str(run_download(job_id))


def task_scan(manga_id: str, trigger: str) -> dict:
    from app.services.jobs import clear_scan_pending
    from app.services.monitor import scan_manga

    try:
        return scan_manga(uuid.UUID(manga_id), trigger)
    finally:
        clear_scan_pending(manga_id)


def task_follow_sync(kind: str, force: bool = False) -> dict:
    """MangaDex account sync: ``list`` (followed list → pending titles) or ``feed`` (new chapters)."""
    from app.services.control import build_client
    from app.services.follows import PENDING_KEY, sync_follow_feed, sync_follow_list

    try:
        client = build_client()
        if kind == "list":
            result = sync_follow_list(client)
            if result.get("added_ids"):
                result["auto_approve"] = _auto_approve(client, [uuid.UUID(x) for x in result["added_ids"]])
            return result
        if kind == "feed":
            return sync_follow_feed(client)
        raise ValueError(f"Unknown follow sync kind: {kind}")
    finally:
        get_redis().delete(PENDING_KEY + kind)


def _auto_approve(client, manga_ids: list[uuid.UUID] | None) -> dict | None:
    """Approve pending titles found in the comics archive, when the setting is on. Never raises."""
    from app.services import auto_approve
    from app.services.archive import archive_root
    from app.settings_service import load_settings

    try:
        with session_scope() as s:
            settings = load_settings(s)
            if not (settings.archive_auto_approve and settings.archive_enabled) or archive_root() is None:
                return None
            r = auto_approve.run(s, client, manga_ids)
        return {k: r[k] for k in ("approved", "skipped")}
    except Exception as exc:  # the sync / index result must not be lost because of this
        log.warning("auto_approve_error", error=str(exc))
        return {"error": str(exc)[:200]}


def task_archive_index() -> dict:
    from app.services.archive import rebuild_index
    from app.services.control import build_client

    try:
        with session_scope() as s:
            result = rebuild_index(s)
        result["auto_approve"] = _auto_approve(build_client(), None)
        return result
    finally:
        get_redis().delete("mdm:archive_index_pending")


def task_junk_cleanup() -> dict:
    from app.services.junk import remove_junk_files

    return remove_junk_files()


def task_library_scan(manga_id: str | None, queue_missing: bool = True) -> dict:
    from app.services.control import build_client
    from app.services.library_scan import run_library_scan
    from app.services.monitor import queue_missing as _queue_missing
    from app.services.monitor import sync_feed

    client = build_client()
    mid = uuid.UUID(manga_id) if manga_id else None
    try:
        summary = run_library_scan(mid, client=client, sync_feed=lambda m, langs: sync_feed(m, langs, client))
        if queue_missing:
            queued = 0
            with session_scope() as s:
                ids = [mid] if mid else list(s.scalars(select(Manga.id)))
                for i in ids:
                    manga = s.get(Manga, i)
                    if manga and manga.monitoring.auto_download:
                        queued += _queue_missing(s, manga, "library_scan")
            summary["queued"] = queued
        return summary
    finally:
        get_redis().delete(f"{LIBSCAN_PENDING_KEY}:{manga_id or 'all'}")


def enqueue_library_scan(manga_id: uuid.UUID | None, queue_missing: bool = True) -> bool:
    from app.services.jobs import QUEUE_SCANS, get_queue

    key = f"{LIBSCAN_PENDING_KEY}:{manga_id or 'all'}"
    r = get_redis()
    if not r.set(key, "1", nx=True, ex=4 * 3600):
        return False
    try:
        get_queue(QUEUE_SCANS).enqueue(
            task_library_scan, str(manga_id) if manga_id else None, queue_missing,
            job_timeout=4 * 3600, result_ttl=3600, failure_ttl=7 * 86400,
        )
    except Exception:
        r.delete(key)
        raise
    return True
