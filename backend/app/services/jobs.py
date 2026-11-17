"""Persistent download-job state machine and dispatching.

PostgreSQL is the source of truth for every job. Redis/RQ is only the transport
that wakes a worker up; if Redis loses its data, the scheduler re-dispatches
from the database. State diagram::

    pending ──dispatch──> queued ──worker──> downloading ──> completed
       ^                                        │
       │<──── retry_scheduled <── transient ────┤
       │<──── paused(storage|queue|blocked|user)┤
       │                                        ├──> failed (retries exhausted)
       └──── manual retry ──────────────────────┴──> needs_attention (permanent)
    any active ──cancel──> cancelled
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta

from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus as RQStatus
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.enums import (
    ACTIVE_JOB_STATUSES,
    HAVE_LOCAL_STATUSES,
    OPEN_JOB_STATUSES,
    JobStatus,
    LocalStatus,
    PauseReason,
)
from app.logging_setup import get_logger
from app.models import ChapterRelease, DownloadJob, MonitoringConfig
from app.ratelimit import get_limiter
from app.redis_client import get_redis
from app.services.control import record_event
from app.settings_service import is_queue_paused, load_settings

log = get_logger(__name__)

QUEUE_DOWNLOADS = "downloads"
QUEUE_SCANS = "scans"
QUEUE_DEFAULT = "default"

QUEUED_ORPHAN_AFTER = timedelta(minutes=10)
HEARTBEAT_STALE_AFTER = timedelta(minutes=3)


class JobActionError(ValueError):
    pass


def get_queue(name: str) -> Queue:
    return Queue(name, connection=get_redis(), default_timeout=6 * 3600)


def now_utc() -> datetime:
    return datetime.now(UTC)


# ----------------------------------------------------------------- creation


def open_job_for(session: Session, release_id: uuid.UUID) -> DownloadJob | None:
    return session.scalar(
        select(DownloadJob).where(
            DownloadJob.release_id == release_id, DownloadJob.status.in_([s.value for s in OPEN_JOB_STATUSES])
        )
    )


def create_download_job(
    session: Session,
    release: ChapterRelease,
    trigger: str,
    *,
    priority: int = 0,
    redownload: bool = False,
    refresh: bool = False,
    replaces: ChapterRelease | None = None,
) -> DownloadJob | None:
    """Create a pending job unless one is already open or the release is on disk.

    A release that was already downloaded/imported is never queued again unless
    ``redownload`` is explicitly requested. ``refresh`` (with ``redownload``) first
    compares MangaDex's page list with the downloaded one; ``replaces`` is the older
    release of the same group whose folder this download takes over.
    """
    existing = open_job_for(session, release.id)
    if existing is not None:
        return existing
    if not redownload and release.local_status in {s.value for s in HAVE_LOCAL_STATUSES}:
        return None
    if release.is_external or release.pages <= 0 or release.is_unavailable:
        return None
    job = DownloadJob(
        release_id=release.id,
        status=JobStatus.PENDING,
        priority=priority,
        trigger=trigger,
        is_redownload=redownload,
        is_refresh=refresh and redownload,
        replaces_release_id=replaces.id if replaces is not None else None,
        pages_total=release.pages or None,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        # Another process created the open job concurrently (partial unique index).
        return open_job_for(session, release.id)
    kind = "version_check" if job.is_refresh else ("redownload_requested" if redownload else "queued")
    record_event(session, kind, trigger, job_id=job.id,
                 release_id=release.id, manga_id=release.manga_id)
    return job


# ----------------------------------------------------------------- dispatching


def _rq_job_alive(rq_job_id: str | None) -> bool:
    if not rq_job_id:
        return False
    try:
        job = Job.fetch(rq_job_id, connection=get_redis())
    except NoSuchJobError:
        return False
    status = job.get_status(refresh=True)
    return status in {RQStatus.QUEUED, RQStatus.STARTED, RQStatus.DEFERRED, RQStatus.SCHEDULED}


def recover_orphans(session: Session, now: datetime | None = None) -> int:
    """Return jobs lost by crashed workers / flushed Redis to a dispatchable state."""
    now = now or now_utc()
    recovered = 0
    stale = session.scalars(
        select(DownloadJob).where(
            DownloadJob.status == JobStatus.DOWNLOADING,
            func.coalesce(DownloadJob.heartbeat_at, DownloadJob.started_at, DownloadJob.updated_at)
            < now - HEARTBEAT_STALE_AFTER,
        ).with_for_update(skip_locked=True)
    ).all()
    for job in stale:
        job.status = JobStatus.RETRY_SCHEDULED
        job.next_attempt_at = now
        job.last_error = "Worker stopped while downloading (crash or restart); resuming"
        record_event(session, "recovered", job.last_error, job_id=job.id, release_id=job.release_id)
        recovered += 1
    queued = session.scalars(
        select(DownloadJob).where(
            DownloadJob.status == JobStatus.QUEUED,
            func.coalesce(DownloadJob.dispatched_at, DownloadJob.updated_at) < now - QUEUED_ORPHAN_AFTER,
        ).with_for_update(skip_locked=True)
    ).all()
    for job in queued:
        if not _rq_job_alive(job.rq_job_id):
            job.status = JobStatus.PENDING
            job.rq_job_id = None
            record_event(session, "recovered", "Queue entry lost; re-dispatching", job_id=job.id, release_id=job.release_id)
            recovered += 1
    return recovered


def dispatch_jobs(session: Session, storage_ok: bool, now: datetime | None = None, enqueue=None) -> int:
    """Hand dispatchable jobs to workers, respecting the concurrency limit.

    Must be followed by ``session.commit()``; RQ enqueueing happens after the
    commit via the returned callback list to avoid workers racing the DB.
    """
    now = now or now_utc()
    settings = load_settings(session)
    if is_queue_paused(session):
        return 0
    limiter = get_limiter()
    if limiter.block_state() or limiter.cooldown_state():
        return 0
    # Storage-paused jobs resume automatically once the library is back.
    if storage_ok:
        session.execute(
            update(DownloadJob)
            .where(DownloadJob.status == JobStatus.PAUSED, DownloadJob.pause_reason == PauseReason.STORAGE)
            .values(status=JobStatus.PENDING, pause_reason=None, next_attempt_at=None)
        )
    else:
        return 0
    active = session.scalar(
        select(func.count()).select_from(DownloadJob).where(
            DownloadJob.status.in_([JobStatus.QUEUED, JobStatus.DOWNLOADING])
        )
    ) or 0
    slots = settings.max_concurrent_chapters - active
    if slots <= 0:
        return 0
    candidates = session.scalars(
        select(DownloadJob)
        .where(
            (DownloadJob.status == JobStatus.PENDING)
            | ((DownloadJob.status == JobStatus.RETRY_SCHEDULED) & (DownloadJob.next_attempt_at <= now))
        )
        # Due retries (e.g. chapters interrupted by a crash, with pages already in
        # staging) go before fresh pending jobs of the same priority; otherwise a
        # large backlog of new jobs (next_attempt_at NULL) would starve them.
        .order_by(
            DownloadJob.priority.desc(),
            case((DownloadJob.status == JobStatus.RETRY_SCHEDULED, 0), else_=1),
            DownloadJob.next_attempt_at.asc().nulls_last(),
            DownloadJob.id,
        )
        .limit(slots)
        .with_for_update(skip_locked=True)
    ).all()
    from app.services.rename import lock_manga_folder_shared

    manga_of = dict(session.execute(
        select(ChapterRelease.id, ChapterRelease.manga_id)
        .where(ChapterRelease.id.in_([j.release_id for j in candidates]))
    ).all()) if candidates else {}
    to_enqueue = []
    for job in candidates:
        mid = manga_of.get(job.release_id)
        if mid is not None and not lock_manga_folder_shared(session, mid, wait=False):
            continue  # the manga folder is being renamed right now; next round
        job.status = JobStatus.QUEUED
        job.dispatched_at = now
        job.rq_job_id = f"dl-{job.id}-{uuid.uuid4().hex[:8]}"
        to_enqueue.append((job.id, job.rq_job_id))
    session.commit()
    enqueue = enqueue or _enqueue_download
    for job_id, rq_id in to_enqueue:
        try:
            enqueue(job_id, rq_id)
        except Exception:
            log.exception("enqueue_failed", job_id=job_id)  # recover_orphans will re-dispatch
    return len(to_enqueue)


def _enqueue_download(job_id: int, rq_id: str) -> None:
    get_queue(QUEUE_DOWNLOADS).enqueue(
        "app.tasks.task_download", job_id, job_id=rq_id, job_timeout=6 * 3600, result_ttl=3600, failure_ttl=7 * 86400
    )


# ----------------------------------------------------------------- user actions


def _job(session: Session, job_id: int) -> DownloadJob:
    job = session.get(DownloadJob, job_id, with_for_update=True)
    if job is None:
        raise JobActionError("Job not found")
    return job


def retry_job(session: Session, job_id: int) -> DownloadJob:
    job = _job(session, job_id)
    if job.status not in {JobStatus.FAILED, JobStatus.NEEDS_ATTENTION, JobStatus.PAUSED, JobStatus.RETRY_SCHEDULED}:
        raise JobActionError(f"Cannot retry a job that is {job.status}")
    job.status = JobStatus.PENDING
    job.pause_reason = None
    job.attempts = 0
    job.next_attempt_at = None
    job.error_kind = None
    record_event(session, "retry", "Manual retry", job_id=job.id, release_id=job.release_id)
    return job


def pause_job(session: Session, job_id: int) -> DownloadJob:
    job = _job(session, job_id)
    if job.status not in {s.value for s in ACTIVE_JOB_STATUSES}:
        raise JobActionError(f"Cannot pause a job that is {job.status}")
    job.status = JobStatus.PAUSED
    job.pause_reason = PauseReason.USER
    record_event(session, "paused", "Paused by user", job_id=job.id, release_id=job.release_id)
    return job


def resume_job(session: Session, job_id: int) -> DownloadJob:
    job = _job(session, job_id)
    if job.status != JobStatus.PAUSED:
        raise JobActionError("Job is not paused")
    if job.pause_reason == PauseReason.BLOCKED and get_limiter().block_state():
        raise JobActionError("MangaDex access is restricted; acknowledge the restriction in Settings first")
    job.status = JobStatus.PENDING
    job.pause_reason = None
    record_event(session, "resumed", "Resumed by user", job_id=job.id, release_id=job.release_id)
    return job


def cancel_job(session: Session, job_id: int) -> DownloadJob:
    """Cancel a job. The release is marked *skipped* so monitoring won't re-queue it.

    Partially downloaded pages are kept in the hidden temp folder and are reused
    if the release is downloaded later.
    """
    job = _job(session, job_id)
    if job.status in {JobStatus.COMPLETED, JobStatus.CANCELLED}:
        raise JobActionError(f"Job is already {job.status}")
    job.status = JobStatus.CANCELLED
    job.finished_at = now_utc()
    release = session.get(ChapterRelease, job.release_id)
    if release is not None and release.local_status == LocalStatus.MISSING:
        release.local_status = LocalStatus.SKIPPED
    record_event(session, "cancelled", "Cancelled by user", job_id=job.id, release_id=job.release_id)
    return job


def set_queue_paused(session: Session, paused: bool) -> None:
    from app.settings_service import QUEUE_PAUSED_KEY, set_raw

    set_raw(session, QUEUE_PAUSED_KEY, paused)
    if not paused:
        session.execute(
            update(DownloadJob)
            .where(DownloadJob.status == JobStatus.PAUSED, DownloadJob.pause_reason == PauseReason.QUEUE)
            .values(status=JobStatus.PENDING, pause_reason=None)
        )


def resume_blocked_jobs(session: Session) -> int:
    res = session.execute(
        update(DownloadJob)
        .where(DownloadJob.status == JobStatus.PAUSED, DownloadJob.pause_reason == PauseReason.BLOCKED)
        .values(status=JobStatus.PENDING, pause_reason=None)
    )
    return res.rowcount or 0


def request_redownload(session: Session, release: ChapterRelease) -> DownloadJob:
    if open_job_for(session, release.id) is not None:
        raise JobActionError("This release already has an open download job")
    job = create_download_job(session, release, "manual_redownload", priority=10, redownload=True)
    if job is None:
        raise JobActionError("This release cannot be downloaded (external or unavailable)")
    return job


# ----------------------------------------------------------------- scans


SCAN_PENDING_PREFIX = "mdm:scan_pending:"


def enqueue_scan(manga_id: uuid.UUID, trigger: str, delay_s: int = 0) -> bool:
    """Queue a monitoring scan; de-duplicated so a manga is never queued twice."""
    r = get_redis()
    key = f"{SCAN_PENDING_PREFIX}{manga_id}"
    if not r.set(key, trigger, nx=True, ex=2 * 3600):
        return False
    try:
        q = get_queue(QUEUE_SCANS)
        kwargs = dict(job_id=f"scan-{manga_id}-{uuid.uuid4().hex[:6]}", job_timeout=2 * 3600, result_ttl=3600,
                      failure_ttl=7 * 86400)
        if delay_s > 0:
            q.enqueue_in(timedelta(seconds=delay_s), "app.tasks.task_scan", str(manga_id), trigger, **kwargs)
        else:
            q.enqueue("app.tasks.task_scan", str(manga_id), trigger, **kwargs)
        return True
    except Exception:
        r.delete(key)
        raise


def clear_scan_pending(manga_id: uuid.UUID | str) -> None:
    get_redis().delete(f"{SCAN_PENDING_PREFIX}{manga_id}")


def next_scan_time(interval_minutes: int, now: datetime | None = None, rng: random.Random | None = None) -> datetime:
    """Next scan with ±10% jitter so monitored titles drift apart over time."""
    now = now or now_utc()
    rng = rng or random
    jitter = rng.uniform(-0.1, 0.1) * interval_minutes
    return now + timedelta(minutes=interval_minutes + jitter)


def schedule_due_scans(session: Session, now: datetime | None = None, enqueue=None) -> int:
    """Start at most one due scan per call, spaced by ``scan_spacing_seconds``."""
    now = now or now_utc()
    limiter = get_limiter()
    if limiter.block_state() or limiter.cooldown_state():
        return 0
    settings = load_settings(session)
    r = get_redis()
    if not r.set("mdm:scan_spacing", "1", nx=True, ex=settings.scan_spacing_seconds):
        return 0
    cfg = session.scalar(
        select(MonitoringConfig)
        .where(MonitoringConfig.monitoring_enabled.is_(True), MonitoringConfig.next_scan_at <= now)
        .order_by(MonitoringConfig.next_scan_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if cfg is None:
        r.delete("mdm:scan_spacing")
        return 0
    # Provisional next time; the scan itself sets the real one when it finishes.
    cfg.next_scan_at = now + timedelta(minutes=30)
    manga_id = cfg.manga_id
    session.commit()
    (enqueue or enqueue_scan)(manga_id, "scheduled")
    return 1
