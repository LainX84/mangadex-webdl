from __future__ import annotations

import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.clients import get_client
from app.api.errors import mangadex_errors
from app.api.serializers import iso, run_out
from app.config import get_config
from app.db import get_db
from app.enums import JobStatus, LocalStatus, ReviewStatus
from app.library.storage import (
    StorageUnavailable,
    atomic_write_bytes,
    initialize_library,
    sentinel_path,
    replaced_root,
    staging_root,
    work_dir_status,
)
from app.mangadex.client import MangaDexClient
from app.models import (
    ChapterRelease,
    DownloadJob,
    Language,
    LibraryScanRun,
    Manga,
    MonitoringConfig,
    MonitoringRun,
    ReviewItem,
)
from app.ratelimit import get_limiter
from app.services.control import (
    cached_mount_status,
    clear_access_block,
    last_heartbeat,
    refresh_mount_status,
)
from app.services.jobs import resume_blocked_jobs
from app.settings_service import AppSettings, is_queue_paused, load_settings, save_settings

router = APIRouter(prefix="/api", tags=["system"])


class ConfirmIn(BaseModel):
    confirm: bool = False


def _cooldown() -> dict | None:
    cd = get_limiter().cooldown_state()
    if not cd:
        return None
    return {
        "until": datetime.fromtimestamp(cd["until_ms"] / 1000, UTC).isoformat(),
        "seconds_remaining": max(0, int(cd["until_ms"] / 1000 - time.time())),
        "reason": cd["reason"],
        "kind": cd.get("kind", "rate_limit"),
        "strikes": cd["strikes"],
    }


def _component_status() -> dict:
    now = time.time()
    out = {}
    for comp in ("scheduler", "worker"):
        hb = last_heartbeat(comp)
        out[comp] = {"alive": hb is not None and now - hb < 90, "last_seen": hb and datetime.fromtimestamp(hb, UTC).isoformat()}
    return out


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict:
    counts = dict(db.execute(select(DownloadJob.status, func.count()).group_by(DownloadJob.status)).all())
    followed = db.scalar(select(func.count()).select_from(Manga).where(Manga.pending_approval.is_(False))) or 0
    pending_approvals = db.scalar(select(func.count()).select_from(Manga)
                                  .where(Manga.pending_approval.is_(True))) or 0
    monitored = db.scalar(select(func.count()).select_from(MonitoringConfig)
                          .where(MonitoringConfig.monitoring_enabled.is_(True))) or 0
    completed_releases = db.scalar(
        select(func.count()).select_from(ChapterRelease).where(ChapterRelease.local_status.in_(
            [LocalStatus.DOWNLOADED, LocalStatus.IMPORTED, LocalStatus.MODIFIED]))
    ) or 0
    last_success = db.scalar(select(func.max(MonitoringRun.finished_at)).where(MonitoringRun.status == "success"))
    next_scan = db.scalar(select(func.min(MonitoringConfig.next_scan_at))
                          .where(MonitoringConfig.monitoring_enabled.is_(True)))
    recent = db.execute(
        select(MonitoringRun, Manga.title, Manga.title_override)
        .join(Manga, Manga.id == MonitoringRun.manga_id)
        .order_by(MonitoringRun.started_at.desc()).limit(10)
    ).all()
    active = db.execute(
        select(DownloadJob, ChapterRelease, Manga)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .join(Manga, Manga.id == ChapterRelease.manga_id)
        .where(DownloadJob.status.in_([JobStatus.DOWNLOADING, JobStatus.QUEUED]))
        .order_by(DownloadJob.started_at.desc().nulls_last()).limit(5)
    ).all()
    recent_dl = db.execute(
        select(DownloadJob, ChapterRelease, Manga)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .join(Manga, Manga.id == ChapterRelease.manga_id)
        .where(DownloadJob.status == JobStatus.COMPLETED)
        .order_by(DownloadJob.finished_at.desc().nulls_last(), DownloadJob.id.desc()).limit(8)
    ).all()
    from app.api.serializers import job_out

    open_reviews = db.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == ReviewStatus.OPEN)) or 0
    open_revisions = db.scalar(select(func.count()).select_from(ReviewItem).where(
        ReviewItem.status == ReviewStatus.OPEN, ReviewItem.kind == "revision_after_move")) or 0
    return {
        "followed_manga": followed,
        "monitored_manga": monitored,
        "completed_releases": completed_releases,
        "jobs": {
            "active": counts.get("downloading", 0) + counts.get("queued", 0),
            "pending": counts.get("pending", 0) + counts.get("retry_scheduled", 0),
            "paused": counts.get("paused", 0),
            "failed": counts.get("failed", 0) + counts.get("needs_attention", 0),
        },
        "active_downloads": [job_out(j, r, m) for j, r, m in active],
        "recent_downloads": [job_out(j, r, m) for j, r, m in recent_dl],
        "last_successful_scan": iso(last_success),
        "next_scheduled_scan": iso(next_scan),
        "recent_scans": [run_out(run, override or title) for run, title, override in recent],
        "cooldown": _cooldown(),
        "access_block": get_limiter().block_state(),
        "queue_paused": is_queue_paused(db),
        "library": cached_mount_status(),
        "components": _component_status(),
        "open_reviews": open_reviews,
        "open_revisions": open_revisions,
        "pending_approvals": pending_approvals,
        "mangadex_account": _account_brief(db),
    }


def _account_brief(db: Session) -> dict | None:
    from app.services.follows import account_status

    try:
        st = account_status(db)
    except Exception:
        return None
    return {k: st[k] for k in ("configured", "active", "username", "last_feed_sync_at", "last_feed_result",
                               "last_error", "auth_failed")}


@router.get("/system/status")
def system_status(db: Session = Depends(get_db)) -> dict:
    cfg = get_config()
    st = refresh_mount_status()
    return {
        "library": st.to_dict(),
        "library_root": str(cfg.downloads_root),
        "staging": work_dir_status(staging_root()),
        "replaced": work_dir_status(replaced_root()),
        "sentinel": str(sentinel_path()),
        "expected_fs_types": sorted(cfg.expected_fs_type_set),
        "cooldown": _cooldown(),
        "access_block": get_limiter().block_state(),
        "queue_paused": is_queue_paused(db),
        "components": _component_status(),
        "version": __import__("app").__version__,
    }


@router.post("/system/library/initialize")
def init_library(body: ConfirmIn) -> dict:
    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Confirmation required")
    try:
        st = initialize_library()
    except StorageUnavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    refresh_mount_status()
    return st.to_dict()


@router.post("/system/access-block/clear")
def clear_block(body: ConfirmIn, db: Session = Depends(get_db)) -> dict:
    """Acknowledge a MangaDex access restriction after resolving its cause."""
    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Confirmation required")
    clear_access_block(db)
    resumed = resume_blocked_jobs(db)
    db.commit()
    return {"ok": True, "resumed_jobs": resumed}


# ----------------------------------------------------------------- settings


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)) -> dict:
    return {"settings": load_settings(db, use_cache=False).model_dump(), "defaults": AppSettings().model_dump()}


@router.put("/settings")
def put_settings(body: dict, db: Session = Depends(get_db)) -> dict:
    current = load_settings(db, use_cache=False).model_dump()
    unknown = set(body) - set(current)
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown setting(s): {', '.join(sorted(unknown))}")
    try:
        new = AppSettings.model_validate({**current, **body})
    except ValidationError as exc:
        msgs = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msgs) from exc
    save_settings(db, new)
    out: dict = {"settings": new.model_dump()}
    if [n.lower() for n in new.excluded_group_names] != [n.lower() for n in current["excluded_group_names"]]:
        from app.services.monitor import apply_group_exclusions

        out["group_exclusions"] = apply_group_exclusions(db)
    db.commit()
    return out


# ----------------------------------------------------------------- languages


class LanguageUpdate(BaseModel):
    folder_code: str = Field(min_length=1, max_length=32)


@router.get("/languages")
def languages(db: Session = Depends(get_db)) -> list[dict]:
    return [{"code": lang.code, "name": lang.name, "folder_code": lang.folder_code}
            for lang in db.scalars(select(Language).order_by(Language.name))]


@router.patch("/languages/{code}")
def update_language(code: str, body: LanguageUpdate, db: Session = Depends(get_db)) -> dict:
    lang = db.get(Language, code)
    if lang is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown language")
    folder = body.folder_code.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}", folder):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Folder code may contain letters, digits, space, _ . -")
    clash = db.scalar(select(Language).where(func.upper(Language.folder_code) == folder.upper(), Language.code != code))
    if clash:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Folder code already used by {clash.name}")
    lang.folder_code = folder
    db.commit()
    # Existing chapter folders keep their location; only new downloads use the new code.
    return {"code": lang.code, "name": lang.name, "folder_code": lang.folder_code}


# ----------------------------------------------------------------- library scans & review


@router.post("/library/scan")
def scan_library() -> dict:
    from app.tasks import enqueue_library_scan

    return {"queued": enqueue_library_scan(None, queue_missing=False)}


@router.get("/library/check")
def library_check_status(db: Session = Depends(get_db)) -> dict:
    """Weekly library check: schedule, last full library scan and what it found."""
    from app.services.library_check import status as check_status

    return check_status(db)


@router.get("/library/scans")
def library_scans(db: Session = Depends(get_db)) -> list[dict]:
    runs = db.scalars(select(LibraryScanRun).order_by(LibraryScanRun.started_at.desc()).limit(20))
    return [{"id": r.id, "manga_id": str(r.manga_id) if r.manga_id else None, "status": r.status,
             "started_at": iso(r.started_at), "finished_at": iso(r.finished_at), "summary": r.summary,
             "error": r.error} for r in runs]


@router.get("/review")
def list_review(db: Session = Depends(get_db), include_closed: bool = False) -> list[dict]:
    stmt = select(ReviewItem, Manga).outerjoin(Manga, Manga.id == ReviewItem.manga_id).order_by(ReviewItem.created_at.desc())
    if not include_closed:
        stmt = stmt.where(ReviewItem.status == ReviewStatus.OPEN)
    return [
        {"id": it.id, "kind": it.kind, "path": it.path, "status": it.status, "details": it.details,
         "candidates": it.candidates, "resolution": it.resolution, "created_at": iso(it.created_at),
         "manga": {"id": str(m.id), "title": m.display_title} if m else None}
        for it, m in db.execute(stmt.limit(300)).all()
    ]


class LinkIn(BaseModel):
    release_id: uuid.UUID


def _review_or_404(db: Session, review_id: int) -> ReviewItem:
    it = db.get(ReviewItem, review_id)
    if it is None or it.status != ReviewStatus.OPEN:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Open review item not found")
    return it


@router.post("/review/{review_id}/link")
def review_link(review_id: int, body: LinkIn, db: Session = Depends(get_db)) -> dict:
    """Confirm that a folder contains a given release (writes only a hidden marker file)."""
    from app.library.storage import require_library
    from app.services.library_scan import link_folder

    it = _review_or_404(db, review_id)
    release = db.get(ChapterRelease, body.release_id)
    if release is None or (it.manga_id and release.manga_id != it.manga_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Release does not belong to this manga")
    if release.folder_rel_path and release.folder_rel_path != it.path:
        raise HTTPException(status.HTTP_409_CONFLICT, "That release is already linked to another folder")
    with mangadex_errors():
        root = require_library()
    link_folder(db, root, release, it.path)
    it.status = ReviewStatus.RESOLVED
    it.resolution = f"linked:{release.id}"
    it.resolved_at = datetime.now(UTC)
    db.commit()
    return {"ok": True}


@router.post("/review/{review_id}/dismiss")
def review_dismiss(review_id: int, db: Session = Depends(get_db)) -> dict:
    it = _review_or_404(db, review_id)
    it.status = ReviewStatus.DISMISSED
    it.resolution = "dismissed"
    it.resolved_at = datetime.now(UTC)
    db.commit()
    return {"ok": True}


@router.post("/review/{review_id}/download-separately")
def review_download_separately(review_id: int, db: Session = Depends(get_db)) -> dict:
    """For a destination conflict: download the release into a suffixed folder instead."""
    from app.library.naming import with_collision_suffix

    it = _review_or_404(db, review_id)
    job_id = (it.details or {}).get("job_id")
    job = db.get(DownloadJob, job_id) if job_id else None
    if job is None or job.status not in (JobStatus.NEEDS_ATTENTION, JobStatus.FAILED):
        raise HTTPException(status.HTTP_409_CONFLICT, "No failed download is waiting on this folder")
    parent, _, name = it.path.rpartition("/")
    job.target_rel_path = f"{parent}/{with_collision_suffix(name, str(job.release_id))}"
    job.status = JobStatus.PENDING
    job.attempts = 0
    job.last_error = None
    it.status = ReviewStatus.RESOLVED
    it.resolution = f"download_to:{job.target_rel_path}"
    it.resolved_at = datetime.now(UTC)
    db.commit()
    return {"ok": True, "target": job.target_rel_path}


# ----------------------------------------------------------------- covers


_COVER_FILE = re.compile(r"^[A-Za-z0-9-]{8,64}\.(jpg|jpeg|png|gif|webp)$")


@router.get("/covers/{manga_id}/{filename}")
def cover(manga_id: uuid.UUID, filename: str, client: MangaDexClient = Depends(get_client)) -> Response:
    """Cover thumbnails proxied through the app (cached on disk, never hot-linked)."""
    if not _COVER_FILE.match(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cover filename")
    cache_dir = Path(get_config().cache_dir) / "covers" / str(manga_id)
    path = cache_dir / f"{filename}.256.jpg"
    if not path.is_file():
        with mangadex_errors():
            resp = client.download_image(client.cover_url(str(manga_id), filename), bucket="cover")
        if not resp.content:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Empty cover")
        cache_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, resp.content)
    return Response(path.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=604800"})
