from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.api.serializers import cover_url, job_out
from app.db import get_db
from app.enums import JobStatus, PauseReason
from app.models import ChapterRelease, DownloadJob, Manga
from app.services import jobs as jobsvc
from app.settings_service import is_queue_paused

router = APIRouter(prefix="/api/queue", tags=["queue"])

GROUPS = {
    "active": [JobStatus.QUEUED, JobStatus.DOWNLOADING],
    "pending": [JobStatus.PENDING, JobStatus.RETRY_SCHEDULED],
    "paused": [JobStatus.PAUSED],
    "failed": [JobStatus.FAILED, JobStatus.NEEDS_ATTENTION],
    "completed": [JobStatus.COMPLETED],
    "cancelled": [JobStatus.CANCELLED],
}


# Type of a title, from its original language (the same rule as the statistics page).
TYPES = ("manga", "manhwa", "manhua", "other")
_TYPE_LANGS = {"manga": ("ja",), "manhwa": ("ko",), "manhua": ("zh", "zh-hk")}


def _type_filter(types: list[str] | None):
    """SQL condition on ``Manga`` for the selected types; None when no type filter is set."""
    wanted = [t for t in (types or []) if t in TYPES]
    if not wanted or set(wanted) == set(TYPES):
        return None
    lang = func.lower(Manga.original_language)
    known = [code for codes in _TYPE_LANGS.values() for code in codes]
    conds = []
    for t in wanted:
        if t == "other":
            conds.append(or_(Manga.original_language.is_(None), lang.notin_(known)))
        else:
            conds.append(lang.in_(_TYPE_LANGS[t]))
    return or_(*conds)


@router.get("")
def list_jobs(
    db: Session = Depends(get_db),
    group: str = Query("active", pattern="^(active|pending|paused|failed|completed|cancelled)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    manga: uuid.UUID | None = Query(None, description="Only the chapters of this manga"),
    type: list[str] | None = Query(None, description="Only titles of these types: manga, manhwa, manhua, other"),
) -> dict:
    statuses = [s.value for s in GROUPS[group]]
    tf = _type_filter(type)
    if group == "completed":
        order = DownloadJob.finished_at.desc().nulls_last()  # most recently downloaded first
    elif group in {"cancelled", "failed"}:
        order = DownloadJob.updated_at.desc()
    else:
        order = DownloadJob.priority.desc()
    rows = db.execute(
        select(DownloadJob, ChapterRelease, Manga)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .join(Manga, Manga.id == ChapterRelease.manga_id)
        .where(DownloadJob.status.in_(statuses), *([ChapterRelease.manga_id == manga] if manga else []),
               *([tf] if tf is not None else []))
        .order_by(order, DownloadJob.next_attempt_at.asc().nulls_first(), DownloadJob.id)
        .limit(limit)
        .offset(offset)
    ).all()
    out = {
        "items": [job_out(j, r, m) for j, r, m in rows],
        "counts": _group_counts(db, manga, tf),
        "queue_paused": is_queue_paused(db),
    }
    if manga is not None:
        m = db.get(Manga, manga)
        out["manga"] = {"id": str(manga), "title": m.display_title if m else None}
    return out


def _group_counts(db: Session, manga: uuid.UUID | None = None, type_filter=None) -> dict[str, int]:
    stmt = select(DownloadJob.status, func.count()).group_by(DownloadJob.status)
    if manga is not None or type_filter is not None:
        stmt = stmt.join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
    if manga is not None:
        stmt = stmt.where(ChapterRelease.manga_id == manga)
    if type_filter is not None:
        stmt = stmt.join(Manga, Manga.id == ChapterRelease.manga_id).where(type_filter)
    counts = dict(db.execute(stmt).all())
    return {g: sum(counts.get(s.value, 0) for s in sts) for g, sts in GROUPS.items()}


# Statuses counted as "still to download" in the per-series view.
TO_DO = [JobStatus.PENDING, JobStatus.RETRY_SCHEDULED, JobStatus.QUEUED, JobStatus.DOWNLOADING]


@router.get("/series")
def list_series(
    db: Session = Depends(get_db),
    group: str = Query("active", pattern="^(active|pending|paused|failed|completed|cancelled)$"),
    type: list[str] | None = Query(None, description="Only titles of these types: manga, manhwa, manhua, other"),
) -> dict:
    """The queue grouped by manga: every title with jobs in ``group``, with its chapter counts.

    ``queued`` = still to download (pending, retry scheduled, queued, downloading), ``downloaded`` =
    completed download jobs, ``in_group`` = jobs in the selected tab. Downloading titles come first,
    then the ones with the most chapters in the tab.
    """
    def n(statuses):
        return func.count().filter(DownloadJob.status.in_([s.value for s in statuses]))

    rows = db.execute(
        select(
            ChapterRelease.manga_id,
            n(GROUPS[group]).label("in_group"),
            n(TO_DO).label("queued"),
            n([JobStatus.DOWNLOADING]).label("downloading"),
            n(GROUPS["paused"]).label("paused"),
            n(GROUPS["failed"]).label("failed"),
            n(GROUPS["completed"]).label("downloaded"),
            func.max(DownloadJob.finished_at).label("last_finished"),
            func.max(DownloadJob.priority).filter(DownloadJob.status.in_([s.value for s in GROUPS[group]])).label("priority"),
        )
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .join(Manga, Manga.id == ChapterRelease.manga_id)
        .where(*([tf] if (tf := _type_filter(type)) is not None else []))
        .group_by(ChapterRelease.manga_id)
        .having(n(GROUPS[group]) > 0)
    ).all()
    mangas = {m.id: m for m in db.scalars(select(Manga).where(Manga.id.in_([r.manga_id for r in rows])))}
    items = []
    for r in rows:
        m = mangas.get(r.manga_id)
        if m is None:
            continue
        cfg = m.monitoring
        items.append({
            "manga": {"id": str(m.id), "title": m.display_title, "cover_url": cover_url(m.id, m.cover_filename),
                      "update_tier": cfg.update_tier if cfg else None},
            "in_group": r.in_group, "queued": r.queued, "downloading": r.downloading, "paused": r.paused,
            "failed": r.failed, "downloaded": r.downloaded,
            "last_finished_at": r.last_finished.isoformat() if r.last_finished else None,
            "_prio": r.priority or 0, "_last": r.last_finished,
        })
    if group == "completed":
        items.sort(key=lambda i: (i["_last"] is None, -(i["_last"].timestamp() if i["_last"] else 0)))
    else:
        items.sort(key=lambda i: (-i["downloading"], -i["_prio"], -i["in_group"], i["manga"]["title"].lower()))
    for i in items:
        i.pop("_prio"), i.pop("_last")
    return {"items": items, "counts": _group_counts(db, None, _type_filter(type)), "queue_paused": is_queue_paused(db)}


def _act(db: Session, fn, job_id: int) -> dict:
    try:
        job = fn(db, job_id)
    except jobsvc.JobActionError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return job_out(job)


@router.post("/{job_id}/retry")
def retry(job_id: int, db: Session = Depends(get_db)) -> dict:
    return _act(db, jobsvc.retry_job, job_id)


@router.post("/{job_id}/pause")
def pause(job_id: int, db: Session = Depends(get_db)) -> dict:
    return _act(db, jobsvc.pause_job, job_id)


@router.post("/{job_id}/resume")
def resume(job_id: int, db: Session = Depends(get_db)) -> dict:
    return _act(db, jobsvc.resume_job, job_id)


@router.post("/{job_id}/cancel")
def cancel(job_id: int, db: Session = Depends(get_db)) -> dict:
    return _act(db, jobsvc.cancel_job, job_id)


@router.post("/pause")
def pause_queue(db: Session = Depends(get_db)) -> dict:
    jobsvc.set_queue_paused(db, True)
    # Jobs waiting to start are paused immediately; running ones stop after their current page.
    db.execute(
        update(DownloadJob)
        .where(DownloadJob.status.in_([JobStatus.PENDING, JobStatus.RETRY_SCHEDULED, JobStatus.QUEUED]))
        .values(status=JobStatus.PAUSED, pause_reason=PauseReason.QUEUE)
    )
    db.commit()
    return {"queue_paused": True}


@router.post("/resume")
def resume_queue(db: Session = Depends(get_db)) -> dict:
    jobsvc.set_queue_paused(db, False)
    db.commit()
    return {"queue_paused": False}


@router.post("/retry-failed")
def retry_failed(db: Session = Depends(get_db)) -> dict:
    res = db.execute(
        update(DownloadJob)
        .where(DownloadJob.status == JobStatus.FAILED)
        .values(status=JobStatus.PENDING, attempts=0, next_attempt_at=None, error_kind=None)
    )
    db.commit()
    return {"retried": res.rowcount or 0}
