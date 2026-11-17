from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.clients import get_client
from app.api.errors import mangadex_errors
from app.api.serializers import iso, manga_detail, manga_summary, release_out, run_out
from app.db import get_db
from app.enums import OPEN_JOB_STATUSES, GroupMode, JobStatus, LocalStatus, ScanTrigger
from app.mangadex.client import MangaDexClient
from app.library.naming import chapter_sort_number, normalize_number
from app.mangadex.parse import parse_chapter
from app.mangadex.urls import InvalidMangaReference, parse_manga_reference
from app.models import (
    ChapterRelease,
    DownloadJob,
    GroupSubscription,
    JobEvent,
    LogicalChapter,
    Manga,
    MonitoringRun,
    ScanlationGroup,
)
from app.services import catalog
from app.services.catalog import CatalogError, release_wanted
from app.services.jobs import JobActionError, create_download_job, enqueue_scan, open_job_for, request_redownload
from app.services.monitor import queue_missing
from app.settings_service import load_settings

router = APIRouter(prefix="/api", tags=["manga"])

_HAVE = {LocalStatus.DOWNLOADED, LocalStatus.IMPORTED, LocalStatus.MODIFIED}


class AddMangaIn(BaseModel):
    reference: str = Field(min_length=1, max_length=500, description="MangaDex URL or UUID")
    languages: list[str] = Field(min_length=1, max_length=20)
    group_mode: GroupMode = GroupMode.ALL
    group_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    include_unknown_group: bool = True
    monitoring_enabled: bool = True
    auto_download: bool = True
    title_override: str | None = Field(default=None, max_length=300)
    existing_folder: str | None = Field(default=None, max_length=300,
                                        description="Use an existing top-level library folder (import)")
    start_chapter: float | None = Field(default=None, ge=0, le=99_999_999)
    # "New chapters only": everything MangaDex lists now counts as already owned.
    only_new: bool = False


class UpdateMangaIn(BaseModel):
    # The title is changed with POST /manga/{id}/rename (it also moves the folder).
    languages: list[str] | None = Field(default=None, max_length=20)
    group_mode: GroupMode | None = None
    group_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    include_unknown_group: bool | None = None
    monitoring_enabled: bool | None = None
    auto_download: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=30, le=7 * 24 * 60)
    use_global_interval: bool = False
    start_chapter: float | None = Field(default=None, ge=0, le=99_999_999)
    clear_start_chapter: bool = False
    one_release_per_chapter: bool | None = None


class BulkDownloadIn(BaseModel):
    release_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


class ConfirmIn(BaseModel):
    confirm: bool = False


def _manga_or_404(db: Session, manga_id: uuid.UUID) -> Manga:
    m = db.get(Manga, manga_id)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Manga not found")
    return m


def _not_pending(m: Manga) -> Manga:
    if m.pending_approval:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This title is waiting for approval: approve it first (Approvals page).")
    return m


def _groups_for(db: Session, manga_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict]]:
    out: dict[uuid.UUID, list[dict]] = defaultdict(list)
    rows = db.execute(
        select(GroupSubscription.manga_id, ScanlationGroup.id, ScanlationGroup.name)
        .join(ScanlationGroup, ScanlationGroup.id == GroupSubscription.group_id)
        .where(GroupSubscription.manga_id.in_(manga_ids))
    ).all()
    for mid, gid, name in rows:
        out[mid].append({"id": str(gid), "name": name})
    return out


class _Rel:
    """Lightweight read-only view of a release (just the columns the download rules need).

    Building full ORM objects for every release of every manga (tens of thousands)
    cost hundreds of MB per library request; this keeps it to a few MB.
    """

    __slots__ = ("id", "manga_id", "local_status", "language_code", "is_unavailable", "external_url", "pages",
                 "group_ids", "chapter", "publish_at", "readable_at", "chapter_key", "sort_number")

    def __init__(self, row):
        (self.id, self.manga_id, self.local_status, self.language_code, self.is_unavailable, self.external_url,
         self.pages, self.group_ids, self.chapter, self.publish_at, self.readable_at, self.chapter_key,
         self.sort_number) = row

    @property
    def is_external(self) -> bool:
        return bool(self.external_url) and (self.pages or 0) <= 1

    @property
    def logical_chapter(self):
        return self if self.chapter_key is not None else None


_REL_COLUMNS = (ChapterRelease.id, ChapterRelease.manga_id, ChapterRelease.local_status, ChapterRelease.language_code,
                ChapterRelease.is_unavailable, ChapterRelease.external_url, ChapterRelease.pages,
                ChapterRelease.group_ids, ChapterRelease.chapter, ChapterRelease.publish_at,
                ChapterRelease.readable_at, LogicalChapter.chapter_key, LogicalChapter.sort_number)
_COUNT_BATCH = 150


def _counts(db: Session, mangas: list[Manga]) -> dict[uuid.UUID, dict[str, int]]:
    counts: dict[uuid.UUID, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if not mangas:
        return counts
    excluded = catalog.excluded_group_ids(db)
    for start in range(0, len(mangas), _COUNT_BATCH):
        _counts_batch(db, mangas[start:start + _COUNT_BATCH], excluded, counts)
    return counts


def _counts_batch(db: Session, mangas: list[Manga], excluded, counts) -> None:
    ids = [m.id for m in mangas]
    # Plain counts: done by the database.
    for mid, st, n in db.execute(
        select(ChapterRelease.manga_id, ChapterRelease.local_status, func.count())
        .where(ChapterRelease.manga_id.in_(ids)).group_by(ChapterRelease.manga_id, ChapterRelease.local_status)
    ):
        if st == LocalStatus.EXTERNAL:
            counts[mid]["external"] += n
        if st in _HAVE:
            counts[mid]["downloaded"] += n
    for mid, st, n in db.execute(
        select(ChapterRelease.manga_id, DownloadJob.status, func.count())
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id.in_(ids), DownloadJob.status.in_([s.value for s in OPEN_JOB_STATUSES]))
        .group_by(ChapterRelease.manga_id, DownloadJob.status)
    ):
        counts[mid]["failed" if st in (JobStatus.FAILED, JobStatus.NEEDS_ATTENTION) else "active"] += n

    # "Missing" applies the download rules, so it needs a few columns of the missing releases.
    langs = defaultdict(set)
    for mid, code in db.execute(select(catalog.MangaLanguage.manga_id, catalog.MangaLanguage.language_code)
                                .where(catalog.MangaLanguage.manga_id.in_(ids))):
        langs[mid].add(code)
    subs = defaultdict(set)
    for mid, gid in db.execute(select(GroupSubscription.manga_id, GroupSubscription.group_id)
                               .where(GroupSubscription.manga_id.in_(ids))):
        subs[mid].add(str(gid))
    cfgs = {m.id: m.monitoring for m in mangas}
    ext = catalog.want_contexts(db, ids)
    base = select(*_REL_COLUMNS).outerjoin(LogicalChapter, LogicalChapter.id == ChapterRelease.logical_chapter_id)
    candidates: dict[uuid.UUID, list[_Rel]] = defaultdict(list)
    for row in db.execute(base.where(ChapterRelease.manga_id.in_(ids),
                                     ChapterRelease.local_status == LocalStatus.MISSING)):
        r = _Rel(row)
        cfg = cfgs.get(r.manga_id)
        e = ext.get(r.manga_id)
        if cfg is not None and release_wanted(r, langs[r.manga_id], cfg.group_mode, subs[r.manga_id],
                                              cfg.include_unknown_group, cfg.start_chapter,
                                              None, None, excluded, ctx=e):
            candidates[r.manga_id].append(r)
    dedupe = [mid for mid in candidates if cfgs[mid] is not None and cfgs[mid].one_release_per_chapter]
    covered: dict[uuid.UUID, set[str]] = defaultdict(set)
    if dedupe:
        for row in db.execute(base.where(ChapterRelease.manga_id.in_(dedupe),
                                         ChapterRelease.local_status.in_([s.value for s in catalog.COVERED_STATUSES]))):
            r = _Rel(row)
            if r.chapter_key is not None and catalog.covers_chapter(r, excluded):
                covered[r.manga_id].add(r.chapter_key)
    for mid, cands in candidates.items():
        if mid in dedupe:
            cands = catalog.one_per_chapter(cands, covered[mid])
        counts[mid]["missing"] += len(cands)


def _detail(db: Session, m: Manga) -> dict:
    from app.services.tiers import effective_interval
    from app.settings_service import load_settings

    langs = catalog.manga_language_codes(db, m.id)
    d = manga_detail(m, sorted(langs), _groups_for(db, [m.id]).get(m.id, []), _counts(db, [m]).get(m.id, {}))
    d["effective_interval_minutes"] = effective_interval(m.monitoring, load_settings(db))
    return d


# ----------------------------------------------------------------- library


@router.get("/manga")
def list_manga(db: Session = Depends(get_db)) -> list[dict]:
    mangas = list(db.scalars(select(Manga).options(selectinload(Manga.monitoring), selectinload(Manga.languages))
                             .order_by(Manga.title)))
    counts = _counts(db, mangas)
    groups = _groups_for(db, [m.id for m in mangas])
    return [
        manga_summary(m, sorted(lang.language_code for lang in m.languages), groups.get(m.id, []), counts.get(m.id, {}))
        for m in mangas
    ]


@router.post("/manga", status_code=201)
def add_manga(body: AddMangaIn, db: Session = Depends(get_db), client: MangaDexClient = Depends(get_client)) -> dict:
    try:
        manga_id = parse_manga_reference(body.reference)
    except InvalidMangaReference as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if body.group_mode == GroupMode.SELECTED and not body.group_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select at least one scanlation group (or choose all groups)")
    pending = db.get(Manga, manga_id)
    approving = pending is not None and pending.pending_approval
    with mangadex_errors():
        try:
            if approving:
                # Titles added from the MangaDex followed list: approval = adding with these options.
                manga = catalog.approve_pending_manga(
                    db, client, pending, body.languages, body.group_mode, body.group_ids,
                    body.monitoring_enabled, body.auto_download, body.title_override, body.include_unknown_group,
                    body.start_chapter,
                )
                from app.services.control import record_event

                record_event(db, "approved", "Approved from the MangaDex followed list", manga_id=manga.id)
            else:
                manga = catalog.add_manga(
                    db, client, manga_id, body.languages, body.group_mode, body.group_ids,
                    body.monitoring_enabled, body.auto_download, body.title_override, body.include_unknown_group,
                    body.start_chapter,
                )
        except CatalogError as exc:
            code = status.HTTP_409_CONFLICT if "already in the library" in str(exc) else status.HTTP_400_BAD_REQUEST
            raise HTTPException(code, str(exc)) from exc
        if body.only_new:
            catalog.mark_current_releases_owned(db, client, manga)
    if body.existing_folder:
        folder = body.existing_folder.strip()
        if "/" in folder or folder.startswith(".") or db.scalar(select(Manga.id).where(Manga.folder_name == folder, Manga.id != manga.id)):
            db.rollback()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or already used library folder")
        manga.folder_name = folder
    try:
        db.commit()
    except IntegrityError as exc:  # the same manga added twice at the same moment
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "This manga is already in the library") from exc
    if body.existing_folder:
        # Import: recognise what is already on disk before anything is downloaded.
        from app.tasks import enqueue_library_scan

        enqueue_library_scan(manga.id, queue_missing=True)
    else:
        enqueue_scan(manga.id, ScanTrigger.ADDED)
    return _detail(db, manga)


class RenameIn(BaseModel):
    title: str | None = Field(default=None, max_length=300, description="None or empty = MangaDex main title")
    dry_run: bool = False


@router.get("/manga/{manga_id}/titles")
def manga_titles(manga_id: uuid.UUID, db: Session = Depends(get_db),
                 client: MangaDexClient = Depends(get_client)) -> dict:
    """Titles MangaDex lists for this manga (main + alternatives, every language)."""
    from app.mangadex.errors import MangaDexError
    from app.services.rename import title_candidates

    m = _manga_or_404(db, manga_id)
    attrs: dict | None = None
    live = True
    try:
        data = client.get_manga(str(m.id))
        attrs = data.get("attributes") or {}
        alts = [a for a in (attrs.get("altTitles") or []) if isinstance(a, dict)]
        if alts != m.alt_titles:
            m.alt_titles = alts
            db.commit()
    except MangaDexError:
        live = False  # offline / rate limited: use what is stored
    if attrs is None:
        attrs = {"title": {m.original_language or "ja-ro": m.title}}
    preferred = catalog.manga_language_codes(db, m.id)
    return {
        "title": m.display_title,
        "canonical_title": m.title,
        "title_override": m.title_override,
        "folder_name": m.folder_name,
        "preferred_languages": preferred,
        "live": live,
        "titles": title_candidates(attrs, m.alt_titles, preferred),
    }


@router.post("/manga/{manga_id}/rename")
def rename_manga_route(manga_id: uuid.UUID, body: RenameIn, db: Session = Depends(get_db)) -> dict:
    """Change the title; the manga folder (and every stored path below it) follows."""
    from app.services.rename import RenameBusy, RenameError, rename_manga

    m = _not_pending(_manga_or_404(db, manga_id))
    db.rollback()  # the rename runs its own transaction (locks + commit)
    try:
        with mangadex_errors():  # maps StorageUnavailable → 503
            report = rename_manga(db, m.id, body.title, dry_run=body.dry_run)
    except RenameBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except RenameError as exc:
        code = status.HTTP_409_CONFLICT if "already exists" in str(exc) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Could not move the folder: {exc}") from exc
    if body.dry_run:
        return {"report": report}
    return {"report": report, "manga": _detail(db, db.get(Manga, manga_id))}


@router.get("/manga/{manga_id}")
def get_manga(manga_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    return _detail(db, _manga_or_404(db, manga_id))


@router.patch("/manga/{manga_id}")
def update_manga(manga_id: uuid.UUID, body: UpdateMangaIn, db: Session = Depends(get_db),
                 client: MangaDexClient = Depends(get_client)) -> dict:
    m = _not_pending(_manga_or_404(db, manga_id))
    cfg = m.monitoring
    if body.group_mode == GroupMode.SELECTED and body.group_ids is not None and not body.group_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select at least one scanlation group")
    with mangadex_errors():
        try:
            rescan = catalog.update_preferences(
                db, client, m, languages=body.languages, group_mode=body.group_mode,
                group_ids=body.group_ids, include_unknown_group=body.include_unknown_group,
                start_chapter=body.start_chapter, clear_start_chapter=body.clear_start_chapter,
            )
        except CatalogError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if cfg.group_mode == GroupMode.SELECTED and not catalog.manga_group_ids(db, m.id):
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select at least one scanlation group")
    if body.one_release_per_chapter is not None and body.one_release_per_chapter != cfg.one_release_per_chapter:
        cfg.one_release_per_chapter = body.one_release_per_chapter
        rescan = True
    if body.monitoring_enabled is not None:
        cfg.monitoring_enabled = body.monitoring_enabled
    if body.auto_download is not None:
        cfg.auto_download = body.auto_download
    if body.use_global_interval:
        cfg.interval_minutes = None
    elif body.interval_minutes is not None:
        cfg.interval_minutes = body.interval_minutes
    db.commit()
    if rescan:
        enqueue_scan(m.id, ScanTrigger.SETTINGS_CHANGE)
    return _detail(db, m)


@router.delete("/manga/{manga_id}")
def delete_manga(manga_id: uuid.UUID, body: ConfirmIn, db: Session = Depends(get_db)) -> dict:
    """Stop following a manga. Removes database records only – files are never deleted."""
    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Confirmation required")
    m = _manga_or_404(db, manga_id)
    from app.services.follows import remember_removed

    remember_removed(db, m)  # so the MangaDex followed list does not add it back
    db.delete(m)
    db.commit()
    return {"ok": True, "files_deleted": False}


@router.get("/manga/{manga_id}/chapters")
def list_chapters(
    manga_id: uuid.UUID,
    db: Session = Depends(get_db),
    scope: Literal["wanted", "all"] = "wanted",
    language: str | None = None,
) -> dict:
    m = _manga_or_404(db, manga_id)
    cfg = m.monitoring
    langs = set(catalog.manga_language_codes(db, m.id))
    groups = catalog.manga_group_ids(db, m.id)
    stmt = select(ChapterRelease).options(selectinload(ChapterRelease.logical_chapter)).where(ChapterRelease.manga_id == m.id)
    if language:
        stmt = stmt.where(ChapterRelease.language_code == language)
    releases = list(db.scalars(stmt))
    latest_jobs: dict[uuid.UUID, DownloadJob] = {}
    for j in db.scalars(select(DownloadJob).join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id).where(ChapterRelease.manga_id == m.id)
                        .order_by(DownloadJob.id)):
        latest_jobs[j.release_id] = j
    e = catalog.want_contexts(db, [m.id]).get(m.id)
    excluded = catalog.excluded_group_ids(db)
    wanted_ids = {
        r.id for r in releases
        if release_wanted(r, langs, cfg.group_mode, groups, cfg.include_unknown_group, cfg.start_chapter,
                          None, None, excluded, ctx=e)
    }
    if cfg.one_release_per_chapter:
        open_ids = {rid for rid, j in latest_jobs.items() if j.status in OPEN_JOB_STATUSES}
        covered = {r.logical_chapter.chapter_key for r in releases if r.logical_chapter is not None
                   and (catalog.covers_chapter(r, excluded) or r.id in open_ids)}
        pending = [r for r in releases if r.id in wanted_ids and r.local_status == LocalStatus.MISSING
                   and r.id not in open_ids]
        keep = {r.id for r in catalog.one_per_chapter(pending, covered)}
        wanted_ids = {rid for rid in wanted_ids if rid in keep or rid not in {p.id for p in pending}}
    from app.services.volumes import volume_release_map

    in_volume = volume_release_map(db, m.id)
    items = []
    for r in releases:
        wanted = r.id in wanted_ids
        if (scope == "wanted" and not wanted and r.local_status not in _HAVE
                and r.local_status not in (LocalStatus.EXTERNAL, LocalStatus.REPLACED) and r.id not in latest_jobs):
            continue
        items.append(release_out(r, latest_jobs.get(r.id), wanted, in_volume.get(str(r.id))))
    items.sort(key=lambda x: (x["sort"] is None, x["sort"] or 0, x["volume"] or "", x["language"], ",".join(x["groups"])))
    return {"items": items, "total": len(items)}


@router.get("/manga/{manga_id}/groups")
def manga_groups(manga_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """Scanlation groups seen for this manga (per language), with subscription state."""
    m = _manga_or_404(db, manga_id)
    subscribed = catalog.manga_group_ids(db, m.id)
    agg: dict[str, dict] = {}
    unknown: dict[str, int] = defaultdict(int)
    for r in db.scalars(select(ChapterRelease).where(ChapterRelease.manga_id == m.id)):
        if not r.group_ids:
            unknown[r.language_code] += 1
            continue
        for gid, name in zip(r.group_ids, r.group_names, strict=False):
            g = agg.setdefault(gid, {"id": gid, "name": name, "languages": defaultdict(int), "subscribed": gid in subscribed})
            g["languages"][r.language_code] += 1
    # Subscribed groups with no releases yet still need to be shown.
    for gid in subscribed - agg.keys():
        grp = db.get(ScanlationGroup, uuid.UUID(gid))
        agg[gid] = {"id": gid, "name": grp.name if grp else gid, "languages": {}, "subscribed": True}
    groups = sorted(agg.values(), key=lambda g: (-sum(g["languages"].values()), g["name"].lower()))
    for g in groups:
        g["languages"] = dict(g["languages"])
    return {"groups": groups, "unknown_group_releases": dict(unknown)}


@router.get("/manga/{manga_id}/history")
def manga_history(manga_id: uuid.UUID, db: Session = Depends(get_db), limit: int = Query(100, ge=1, le=500)) -> dict:
    _manga_or_404(db, manga_id)
    events = db.execute(
        select(JobEvent, ChapterRelease)
        .outerjoin(ChapterRelease, ChapterRelease.id == JobEvent.release_id)
        .where((JobEvent.manga_id == manga_id) | (ChapterRelease.manga_id == manga_id))
        .order_by(JobEvent.created_at.desc())
        .limit(limit)
    ).all()
    runs = db.scalars(select(MonitoringRun).where(MonitoringRun.manga_id == manga_id)
                      .order_by(MonitoringRun.started_at.desc()).limit(20))
    return {
        "events": [
            {
                "id": e.id, "event": e.event, "message": e.message, "created_at": iso(e.created_at),
                "release": {"id": str(r.id), "chapter": r.chapter, "volume": r.volume, "language": r.language_code,
                            "groups": r.group_names} if r else None,
            }
            for e, r in events
        ],
        "scans": [run_out(run) for run in runs],
    }


@router.post("/manga/{manga_id}/scan")
def scan_now(manga_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    _not_pending(_manga_or_404(db, manga_id))
    queued = enqueue_scan(manga_id, ScanTrigger.MANUAL)
    return {"queued": queued, "message": "Scan queued" if queued else "A scan is already queued or running"}


@router.post("/manga/{manga_id}/download-missing")
def download_missing(manga_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    m = _not_pending(_manga_or_404(db, manga_id))
    n = queue_missing(db, m, "manual")
    db.commit()
    return {"queued": n}


class MarkOwnedIn(BaseModel):
    up_to: float | None = Field(default=None, ge=0, le=100_000,
                                description="Owned up to this chapter; None = everything MangaDex lists now")


@router.post("/manga/{manga_id}/mark-owned")
def mark_owned(manga_id: uuid.UUID, body: MarkOwnedIn | None = None, db: Session = Depends(get_db),
               client: MangaDexClient = Depends(get_client)) -> dict:
    """"I already have these": chapters owned elsewhere are never downloaded.

    Without ``up_to`` every release MangaDex lists now counts as owned (the same rule as
    "New chapters only" when adding); with ``up_to`` every chapter numbered up to it does,
    and higher numbers stay wanted. Open downloads of owned chapters are cancelled;
    monitoring stays as it is.
    """
    up_to = body.up_to if body is not None else None
    from app.services import jobs as jobsvc
    from app.services.control import record_event

    from app.library.naming import chapter_sort_number

    m = _not_pending(_manga_or_404(db, manga_id))
    if up_to is not None and m.chapters_per_volume:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "This title numbers chapters per volume: a single chapter number is ambiguous")
    open_jobs = db.execute(
        select(DownloadJob, ChapterRelease).join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == m.id, DownloadJob.status.in_([
            s.value for s in (JobStatus.PENDING, JobStatus.RETRY_SCHEDULED, JobStatus.QUEUED,
                              JobStatus.DOWNLOADING, JobStatus.PAUSED)]))
    ).all()
    cancelled = 0
    for j, rel in open_jobs:
        if up_to is not None and ((n := chapter_sort_number(rel.chapter)) is None or n > up_to):
            continue
        try:
            jobsvc.cancel_job(db, j.id)
            cancelled += 1
        except jobsvc.JobActionError:
            pass
    try:
        with mangadex_errors():
            r = (catalog.mark_current_releases_owned(db, client, m) if up_to is None
                 else catalog.mark_owned_up_to(db, client, m, up_to))
    except CatalogError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    what = "everything listed now; only new chapters will be downloaded" if up_to is None \
        else f"up to chapter {up_to:g}; higher chapters are still downloaded"
    record_event(db, "settings", f"Marked as already owned ({what}): {r.get('owned', 0)} chapter(s), "
                 f"{cancelled} download(s) cancelled", manga_id=m.id)
    db.commit()
    return {"owned": r.get("owned", 0), "cancelled": cancelled, "after_chapter": r.get("after_chapter")}


@router.post("/manga/{manga_id}/library-scan")
def library_scan(manga_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    _not_pending(_manga_or_404(db, manga_id))
    from app.tasks import enqueue_library_scan

    queued = enqueue_library_scan(manga_id, queue_missing=False)
    return {"queued": queued}


class BuildVolumeIn(BaseModel):
    language: str = Field(min_length=1, max_length=16)
    volume: str = Field(min_length=1, max_length=32)
    confirm: bool = False  # the packed chapter folders are deleted
    allow_warnings: bool = False
    allow_open: bool = False


@router.get("/manga/{manga_id}/volumes")
def list_volumes(manga_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """Numbered volumes per language: built, ready to pack, open (may still grow) or incomplete."""
    from app.library.storage import StorageUnavailable, require_library
    from app.services.volumes import plan_volumes

    m = _not_pending(_manga_or_404(db, manga_id))
    try:
        root = require_library()
    except StorageUnavailable as exc:
        return {"available": False, "error": str(exc), "items": []}
    plans = plan_volumes(db, root, m)
    db.rollback()
    return {"available": True, "auto": load_settings(db).volume_cbz_auto, "items": [p.to_dict() for p in plans]}


@router.post("/manga/{manga_id}/volumes/build")
def build_volume_route(manga_id: uuid.UUID, body: BuildVolumeIn, db: Session = Depends(get_db)) -> dict:
    from app.services.volumes import enqueue_build

    _not_pending(_manga_or_404(db, manga_id))
    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Confirm: the packed chapter folders will be deleted")
    return {"queued": enqueue_build(manga_id, body.language, body.volume, body.allow_warnings, body.allow_open)}


# ----------------------------------------------------------------- releases


def _release_or_404(db: Session, release_id: uuid.UUID) -> ChapterRelease:
    r = db.get(ChapterRelease, release_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Release not found")
    return r


@router.post("/releases/{release_id}/download")
def download_release(release_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    r = _release_or_404(db, release_id)
    if r.local_status == LocalStatus.SKIPPED:
        r.local_status = LocalStatus.MISSING
    if r.local_status != LocalStatus.MISSING:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This release is already in the library. Use 'Redownload' to fetch a fresh copy.")
    job = create_download_job(db, r, "manual", priority=5)
    if job is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This release cannot be downloaded (external or unavailable)")
    db.commit()
    return {"job_id": job.id, "status": job.status}


@router.post("/releases/download")
def download_releases(body: BulkDownloadIn, db: Session = Depends(get_db)) -> dict:
    """Queue several releases at once (multi-select in the chapter list).

    Releases already in the library are left alone (use Redownload for those);
    skipped releases are un-skipped. Selected chapters are queued even when they
    fall outside the manga's group/start-chapter filters: picking them is explicit.
    """
    queued, already, not_downloadable = 0, 0, 0
    for rid in dict.fromkeys(body.release_ids):
        r = db.get(ChapterRelease, rid)
        if r is None:
            not_downloadable += 1
            continue
        if r.local_status == LocalStatus.SKIPPED:
            r.local_status = LocalStatus.MISSING
        if r.local_status != LocalStatus.MISSING:
            already += 1
            continue
        if open_job_for(db, r.id) is not None:
            already += 1
            continue
        if create_download_job(db, r, "manual", priority=5) is None:
            not_downloadable += 1
        else:
            queued += 1
    db.commit()
    return {"queued": queued, "already": already, "not_downloadable": not_downloadable}


@router.post("/releases/{release_id}/redownload")
def redownload_release(release_id: uuid.UUID, body: ConfirmIn, db: Session = Depends(get_db)) -> dict:
    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Redownload requires explicit confirmation")
    r = _release_or_404(db, release_id)
    try:
        job = request_redownload(db, r)
    except JobActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return {"job_id": job.id, "status": job.status,
            "note": "The current folder will be moved to the replaced folder (outside the library) when the new copy is ready."}


@router.post("/releases/{release_id}/skip")
def skip_release(release_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    r = _release_or_404(db, release_id)
    if r.local_status != LocalStatus.MISSING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only missing releases can be skipped")
    r.local_status = LocalStatus.SKIPPED
    db.commit()
    return {"ok": True}


@router.post("/releases/{release_id}/unskip")
def unskip_release(release_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    r = _release_or_404(db, release_id)
    if r.local_status == LocalStatus.SKIPPED:
        r.local_status = LocalStatus.MISSING
        db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- MangaDex lookup


@router.get("/mangadex/search")
def search(q: str = Query(min_length=1, max_length=200), db: Session = Depends(get_db),
           client: MangaDexClient = Depends(get_client)) -> dict:
    followed = set(db.scalars(select(Manga.id).where(Manga.pending_approval.is_(False))))
    pending = set(db.scalars(select(Manga.id).where(Manga.pending_approval.is_(True))))
    with mangadex_errors():
        try:
            mid = parse_manga_reference(q)
        except InvalidMangaReference:
            mid = None
        from app.mangadex.parse import parse_manga

        from app.services.rename import title_candidates

        if mid is not None:
            raws = [client.get_manga(str(mid))]
            total = 1
        else:
            data = client.search_manga(q, limit=20)
            raws = data.get("data") or []
            total = int(data.get("total") or len(raws))
        items = [(parse_manga(d), d.get("attributes") or {}) for d in raws]
    return {
        "total": total,
        "items": [
            {
                "id": p.id,
                "title": p.title,
                "alt_titles": [next(iter(a.values())) for a in p.alt_titles[:5] if a],
                # Every MangaDex title (main + alternatives) for the folder-name picker.
                "titles": title_candidates(attrs, p.alt_titles, []),
                "year": p.year,
                "status": p.status,
                "content_rating": p.content_rating,
                "description": (p.description or "")[:400],
                "available_languages": p.available_languages,
                "cover_url": f"/api/covers/{p.id}/{p.cover_filename}" if p.cover_filename else None,
                "followed": uuid.UUID(p.id) in followed,
                "pending": uuid.UUID(p.id) in pending,
            }
            for p, attrs in items
        ],
    }


@router.get("/mangadex/manga/{reference:path}/groups")
def preview_groups(reference: str, languages: str = Query(..., max_length=200),
                   db: Session = Depends(get_db), client: MangaDexClient = Depends(get_client)) -> dict:
    """Groups and chapters published in the given languages (for the add form).

    ``chapters`` has one entry per chapter number and language: the groups that
    released it and the first publication date. Official-publisher links and
    releases of excluded groups are left out (they are never downloaded).
    """
    from app.settings_service import load_settings as _load

    excluded_names = {n.lower() for n in _load(db).excluded_group_names}
    try:
        mid = parse_manga_reference(reference)
    except InvalidMangaReference as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    langs = sorted({x.strip() for x in languages.split(",") if x.strip()})
    if not langs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No languages given")
    agg: dict[str, dict] = {}
    unknown: dict[str, int] = defaultdict(int)
    chapters: dict[tuple, dict] = {}
    with mangadex_errors():
        for item in client.iter_manga_feed(str(mid), langs, cache_ttl=600):
            ch = parse_chapter(item)
            if ch.is_external or ch.pages <= 0:
                continue
            if any(g.name.lower() in excluded_names for g in ch.groups):
                continue
            if not ch.groups:
                unknown[ch.language] += 1
            for g in ch.groups:
                e = agg.setdefault(g.id, {"id": g.id, "name": g.name, "languages": defaultdict(int)})
                e["languages"][ch.language] += 1
            num = normalize_number(ch.chapter)
            key = (num, ch.language) if num is not None else (f"x:{ch.id}", ch.language)
            published = ch.publish_at or ch.readable_at
            c = chapters.setdefault(key, {"chapter": num, "volume": ch.volume, "language": ch.language,
                                          "title": ch.title if num is None else None,
                                          "groups": [], "published": None})
            for g in ch.groups or []:
                if g.id not in c["groups"]:
                    c["groups"].append(g.id)
            if not ch.groups and "" not in c["groups"]:
                c["groups"].append("")  # "" = released without a group
            if published and (c["published"] is None or published.isoformat() < c["published"]):
                c["published"] = published.isoformat()
    groups = sorted(agg.values(), key=lambda g: (-sum(g["languages"].values()), g["name"].lower()))
    for g in groups:
        g["languages"] = dict(g["languages"])
    ordered = sorted(chapters.values(), key=lambda c: (chapter_sort_number(c["chapter"]) is None,
                                                       chapter_sort_number(c["chapter"]) or 0, c["language"]))
    return {"groups": groups, "unknown_group_releases": dict(unknown), "chapters": ordered}


@router.get("/mangadex/manga/{reference:path}")
def preview_manga(reference: str, db: Session = Depends(get_db), client: MangaDexClient = Depends(get_client)) -> dict:
    try:
        mid = parse_manga_reference(reference)
    except InvalidMangaReference as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    from app.mangadex.parse import parse_manga
    from app.services.rename import title_candidates

    with mangadex_errors():
        p = parse_manga(client.get_manga(str(mid)))
    settings = load_settings(db)
    return {
        "id": p.id,
        "title": p.title,
        "alt_titles": p.alt_titles,
        "description": p.description,
        "status": p.status,
        "year": p.year,
        "content_rating": p.content_rating,
        "original_language": p.original_language,
        "tags": p.tags,
        "authors": p.authors,
        "available_languages": p.available_languages,
        "cover_url": f"/api/covers/{p.id}/{p.cover_filename}" if p.cover_filename else None,
        "followed": (m := db.get(Manga, mid)) is not None and not m.pending_approval,
        "pending": m is not None and m.pending_approval,
        "titles": title_candidates(p.raw.get("attributes") or {}, p.alt_titles, []),
        "content_rating_allowed": p.content_rating in settings.content_ratings,
    }
