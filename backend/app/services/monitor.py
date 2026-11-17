"""Monitoring scans: discover releases on MangaDex and queue the missing ones.

Request budget per scan (with defaults):
* incremental scan: 1 feed request (``updatedAtSince``) – most scans;
* full scan (daily, after filter changes, manual): ceil(releases / 500) requests;
* metadata refresh: +1 request at most once a day.
"""

from __future__ import annotations

import uuid
import zlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.db import get_engine, session_scope
from app.enums import JobStatus, LocalStatus, ScanStatus, ScanTrigger
from app.logging_setup import get_logger
from app.mangadex.client import MangaDexClient
from app.mangadex.errors import AccessDeniedError, MaintenanceError, NotFoundError
from app.mangadex.parse import parse_chapter, parse_manga
from app.models import ChapterRelease, DownloadJob, Manga, MonitoringConfig, MonitoringRun
from app.ratelimit import AccessBlocked, CooldownActive
from app.retry import backoff_seconds
from app.services.catalog import (
    covers_chapter,
    in_excluded_group,
    excluded_group_ids,
    apply_manga_metadata,
    manga_group_ids,
    manga_language_codes,
    release_wanted,
    one_per_chapter,
    same_group_reuploads,
    upsert_release,
    version_changed,
    want_contexts,
)
from app.services.control import record_event
from app.services.jobs import create_download_job, next_scan_time
from app.services.numbering import ensure_numbering
from app.services.tiers import effective_interval, refresh_tiers
from app.settings_service import load_settings

log = get_logger(__name__)

METADATA_REFRESH_AFTER = timedelta(hours=24)
INCREMENTAL_OVERLAP = timedelta(hours=1)


def _lock_id(manga_id: uuid.UUID) -> int:
    return zlib.crc32(b"scan:" + manga_id.bytes)


def _open_job_releases(session: Session, manga_id: uuid.UUID) -> set:
    return set(
        session.scalars(
            select(DownloadJob.release_id)
            .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
            .where(
                ChapterRelease.manga_id == manga_id,
                DownloadJob.status.notin_([JobStatus.COMPLETED, JobStatus.CANCELLED]),
            )
        )
    )


def reupload_replacements(session: Session, manga: Manga, open_job_releases: set | None = None) -> list[tuple]:
    """(new, old) same-group re-uploads to download in place of the copy in the library."""
    from sqlalchemy.orm import selectinload

    cfg = manga.monitoring
    langs = set(manga_language_codes(session, manga.id))
    groups = manga_group_ids(session, manga.id)
    excluded = excluded_group_ids(session)
    if open_job_releases is None:
        open_job_releases = _open_job_releases(session, manga.id)
    rows = session.scalars(
        select(ChapterRelease).options(selectinload(ChapterRelease.logical_chapter))
        .where(ChapterRelease.manga_id == manga.id)
    ).all()

    def ok(r):  # the language/group rules; the start chapter does not apply to a replacement
        return (r.id not in open_job_releases
                and release_wanted(r, langs, cfg.group_mode, groups, cfg.include_unknown_group, None,
                                   None, None, excluded))

    busy = {r.id for r in rows if r.id in open_job_releases}
    since = revision_window_start(session)
    return [(new, old) for new, old in same_group_reuploads(rows, ok, since) if old.id not in busy]


def revision_window_start(session: Session):
    """Chapters moved out of the library that were downloaded after this still get revisions (or None)."""
    from datetime import UTC, datetime, timedelta

    from app.settings_service import load_settings

    days = load_settings(session).revision_after_move_days
    return datetime.now(UTC) - timedelta(days=days) if days > 0 else None


def version_refreshes(session: Session, manga: Manga, open_job_releases: set | None = None) -> list[ChapterRelease]:
    """Downloaded releases whose MangaDex version went up: check (and refresh) their pages."""
    langs = set(manga_language_codes(session, manga.id))
    if open_job_releases is None:
        open_job_releases = _open_job_releases(session, manga.id)
    rows = session.scalars(
        select(ChapterRelease).where(
            ChapterRelease.manga_id == manga.id,
            ChapterRelease.local_status.in_([LocalStatus.DOWNLOADED, LocalStatus.MODIFIED,
                                             LocalStatus.MISSING_ON_DISK]),
            ChapterRelease.version > ChapterRelease.downloaded_version,
        )
    ).all()
    since = revision_window_start(session)
    return [r for r in rows
            if r.id not in open_job_releases and r.language_code in langs and version_changed(r, since)]


def wanted_missing_releases(session: Session, manga: Manga, skip: set | None = None) -> list[ChapterRelease]:
    cfg = manga.monitoring
    langs = set(manga_language_codes(session, manga.id))
    groups = manga_group_ids(session, manga.id)
    open_job_releases = _open_job_releases(session, manga.id)
    ctx = want_contexts(session, [manga.id]).get(manga.id)
    excluded = excluded_group_ids(session)
    rows = session.scalars(
        select(ChapterRelease).where(
            ChapterRelease.manga_id == manga.id,
            ChapterRelease.local_status == LocalStatus.MISSING,
            ChapterRelease.is_unavailable.is_(False),
        )
    ).all()
    wanted = [
        r for r in rows
        if r.id not in open_job_releases and r.id not in (skip or ())
        and release_wanted(r, langs, cfg.group_mode, groups, cfg.include_unknown_group, cfg.start_chapter,
                           None, None, excluded, ctx=ctx)
    ]
    if cfg.one_release_per_chapter and wanted:
        covered = covered_chapter_keys(session, manga.id, open_job_releases, excluded)
        wanted = one_per_chapter(wanted, covered)
    return wanted


def covered_chapter_keys(session: Session, manga_id: uuid.UUID, open_job_releases: set,
                         excluded=None) -> set[str]:
    from sqlalchemy.orm import selectinload

    rows = session.scalars(
        select(ChapterRelease).options(selectinload(ChapterRelease.logical_chapter))
        .where(ChapterRelease.manga_id == manga_id, ChapterRelease.logical_chapter_id.is_not(None))
    )
    return {r.logical_chapter.chapter_key for r in rows
            if covers_chapter(r, excluded) or r.id in open_job_releases}


def queue_missing(session: Session, manga: Manga, trigger: str) -> int:
    queued = 0
    open_jobs = _open_job_releases(session, manga.id)
    # Same group uploaded the chapter again (v2, fixed pages): download it in place of the old copy.
    replacements = reupload_replacements(session, manga, open_jobs)
    for new, old in replacements:
        job = create_download_job(session, new, trigger, replaces=old)
        if job is not None and job.status == JobStatus.PENDING:
            queued += 1
            record_event(session, "reupload", f"Re-upload by the same group: replaces {old.folder_rel_path}",
                         job_id=job.id, release_id=new.id, manga_id=manga.id)
    # Same chapter UUID with a newer version: compare the page list, download only if it changed.
    for release in version_refreshes(session, manga, open_jobs):
        job = create_download_job(session, release, trigger, redownload=True, refresh=True)
        if job is not None and job.status == JobStatus.PENDING:
            queued += 1
    for release in sorted(wanted_missing_releases(session, manga, {n.id for n, _ in replacements}),
                          key=lambda r: (r.logical_chapter.sort_number is None, r.logical_chapter.sort_number or 0)):
        job = create_download_job(session, release, trigger)
        if job is not None and job.status == JobStatus.PENDING:
            queued += 1
    return queued


def scan_manga(manga_id: uuid.UUID, trigger: str, client: MangaDexClient | None = None) -> dict:
    if client is None:
        from app.services.control import build_client

        client = build_client()
    with get_engine().connect() as lock_conn:
        got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _lock_id(manga_id)}).scalar()
        if not got:
            log.info("scan_skipped_overlap", manga_id=str(manga_id))
            return {"status": "skipped"}
        try:
            return _scan_locked(manga_id, trigger, client)
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_id(manga_id)})
            lock_conn.commit()


def _scan_locked(manga_id: uuid.UUID, trigger: str, client: MangaDexClient) -> dict:
    started = datetime.now(UTC)
    client.requests_made = 0
    with session_scope() as s:
        manga = s.get(Manga, manga_id)
        if manga is None:
            return {"status": "missing"}
        cfg = manga.monitoring
        settings = load_settings(s)
        languages = manga_language_codes(s, manga_id)
        full = (
            cfg.needs_full_scan
            or cfg.last_full_scan_at is None
            or cfg.last_success_at is None
            or started - cfg.last_full_scan_at > timedelta(hours=settings.full_scan_interval_hours)
            or trigger in {ScanTrigger.MANUAL, ScanTrigger.SETTINGS_CHANGE, ScanTrigger.ADDED}
        )
        since = None if full else cfg.last_success_at - INCREMENTAL_OVERLAP
        refresh_meta = manga.metadata_refreshed_at is None or started - manga.metadata_refreshed_at > METADATA_REFRESH_AFTER
        run = MonitoringRun(manga_id=manga_id, trigger=trigger, full_scan=full, status=ScanStatus.RUNNING)
        s.add(run)
        s.flush()
        run_id = run.id
        interval = effective_interval(cfg, settings)

    try:
        if refresh_meta:
            parsed = parse_manga(client.get_manga(str(manga_id), use_cache=False))
            with session_scope() as s:
                apply_manga_metadata(s.get(Manga, manga_id), parsed)
        seen = new = 0
        with session_scope() as s:
            # A just-added manga reuses the feed fetched for the group picker (10 min cache).
            ttl = 600 if trigger == ScanTrigger.ADDED else None
            for item in client.iter_manga_feed(str(manga_id), languages, updated_since=since, cache_ttl=ttl):
                ch = parse_chapter(item)
                _, is_new = upsert_release(s, manga_id, ch)
                seen += 1
                new += int(is_new)
            if full and seen < 9_500:  # feed fully listed (MangaDex offset window is 10k)
                # Releases no longer listed (removed/hidden on MangaDex): flag only, never touch files.
                s.execute(
                    update(ChapterRelease)
                    .where(
                        ChapterRelease.manga_id == manga_id,
                        ChapterRelease.language_code.in_(languages),
                        ChapterRelease.last_seen_at < started,
                    )
                    .values(is_unavailable=True)
                )
        with session_scope() as s:
            manga = s.get(Manga, manga_id)
            cfg = manga.monitoring
            # Numbering restarting every volume (anthologies) must be known before queueing.
            ensure_numbering(s, manga)
            queued = queue_missing(s, manga, "monitor") if cfg.auto_download else 0
            now = datetime.now(UTC)
            # New chapters (or none for a long time) may move the title to another level.
            refresh_tiers(s, [manga_id], now)
            interval = effective_interval(cfg, load_settings(s))
            cfg.last_scan_at = now
            cfg.last_success_at = started
            if full:
                cfg.last_full_scan_at = started
                cfg.needs_full_scan = False
            cfg.last_error = None
            cfg.consecutive_failures = 0
            cfg.next_scan_at = next_scan_time(interval, now)
            run = s.get(MonitoringRun, run_id)
            run.status = ScanStatus.SUCCESS
            run.finished_at = now
            run.releases_seen = seen
            run.new_releases = new
            run.queued = queued
            run.requests_made = client.requests_made
            if new or queued:
                record_event(s, "scan", f"{new} new release(s), {queued} queued", manga_id=manga_id)
        from app.services.layout import rename_after_volume_changes

        rename_after_volume_changes(manga_id)  # "Volume TBD …" folders whose volume MangaDex just set
        log.info("scan_completed", manga_id=str(manga_id), full=full, seen=seen, new=new, queued=queued,
                 requests=client.requests_made)
        return {"status": "success", "seen": seen, "new": new, "queued": queued, "full": full}
    except Exception as exc:  # noqa: BLE001
        _record_scan_failure(manga_id, run_id, exc, interval, client.requests_made)
        if isinstance(exc, (CooldownActive, AccessBlocked, AccessDeniedError, NotFoundError, MaintenanceError)):
            return {"status": "failed", "error": str(exc)}
        raise


def sync_feed(manga_id: uuid.UUID, languages: list[str], client: MangaDexClient) -> int:
    """Full feed sync for the given languages without queueing (used by library import)."""
    if not languages:
        return 0
    seen = 0
    with session_scope() as s:
        for item in client.iter_manga_feed(str(manga_id), languages):
            upsert_release(s, manga_id, parse_chapter(item))
            seen += 1
        manga = s.get(Manga, manga_id)
        if manga is not None:
            ensure_numbering(s, manga)
    return seen


def _record_scan_failure(manga_id, run_id, exc, interval_minutes, requests) -> None:
    now = datetime.now(UTC)
    with session_scope() as s:
        cfg = s.get(MonitoringConfig, manga_id)
        settings = load_settings(s)
        maintenance = isinstance(exc, MaintenanceError) or (isinstance(exc, CooldownActive) and exc.is_maintenance)
        msg = "MangaDex is down for maintenance" if maintenance else f"{exc.__class__.__name__}: {exc}"[:2000]
        if cfg is not None:
            cfg.last_scan_at = now
            cfg.last_error = msg
            if not maintenance:  # MangaDex being offline says nothing about this title
                cfg.consecutive_failures += 1
            if maintenance and getattr(exc, "until_ms", None):
                nxt = datetime.fromtimestamp(exc.until_ms / 1000, UTC) + timedelta(minutes=2)
            elif isinstance(exc, CooldownActive):
                nxt = datetime.fromtimestamp(exc.until_ms / 1000, UTC) + timedelta(minutes=2)
            elif isinstance(exc, (AccessBlocked, AccessDeniedError, NotFoundError)):
                nxt = now + timedelta(minutes=interval_minutes)
            else:
                delay = backoff_seconds(cfg.consecutive_failures, max(300, settings.initial_retry_delay_seconds),
                                        interval_minutes * 60)
                nxt = now + timedelta(seconds=delay)
            cfg.next_scan_at = nxt
        run = s.get(MonitoringRun, run_id)
        if run is not None:
            run.status = ScanStatus.FAILED
            run.finished_at = now
            run.error = msg
            run.requests_made = requests
    log.warning("scan_failed", manga_id=str(manga_id), error=msg[:300])


_CANCELLABLE = (JobStatus.PENDING, JobStatus.QUEUED, JobStatus.PAUSED, JobStatus.RETRY_SCHEDULED,
                JobStatus.FAILED, JobStatus.NEEDS_ATTENTION)


def apply_group_exclusions(session: Session) -> dict:
    """Cancel open jobs for official-publisher links and excluded groups, then queue alternatives.

    Run after the excluded-group list changes. A cancelled release is marked *skipped*;
    for titles with auto-download on, the chapter is re-evaluated so another group's
    release of the same chapter can be queued instead.
    """
    excluded = excluded_group_ids(session)
    rows = session.execute(
        select(DownloadJob, ChapterRelease)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(DownloadJob.status.in_([s.value for s in _CANCELLABLE]))
    ).all()
    now = datetime.now(UTC)
    cancelled = 0
    affected: set[uuid.UUID] = set()
    for job, release in rows:
        if not (release.is_external or in_excluded_group(release, excluded)):
            continue
        job.status = JobStatus.CANCELLED
        job.finished_at = now
        if release.local_status == LocalStatus.MISSING:
            release.local_status = LocalStatus.SKIPPED
        record_event(session, "cancelled", "Cancelled: excluded group or official publisher link",
                     job_id=job.id, release_id=release.id)
        cancelled += 1
        affected.add(release.manga_id)
    session.flush()
    queued = 0
    for mid in affected:
        manga = session.get(Manga, mid)
        if manga is not None and manga.monitoring is not None and manga.monitoring.auto_download:
            queued += queue_missing(session, manga, "settings_change")
    log.info("group_exclusions_applied", cancelled=cancelled, queued=queued, manga=len(affected))
    return {"cancelled": cancelled, "queued_instead": queued, "manga": len(affected)}
