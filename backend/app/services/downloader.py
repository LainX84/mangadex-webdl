"""Download one chapter release as individual page images.

Pages are fetched into a folder in the staging directory
(``{staging}/{chapter uuid}/``, ``MDM_STAGING_ROOT``), outside the library, so the
library only ever holds complete chapters. When every page is there the folder is
moved into the library: a single atomic ``rename`` when staging and library share a
mount, otherwise a copy that only appears under its final name once complete.
Each page is validated, written atomically and recorded in ``page_records``
before the next one, so an interruption at any point resumes with only the
missing pages. Previous copies of a chapter (redownloads, re-uploads, new
versions) are moved to ``MDM_REPLACED_ROOT``, also outside the library. A group's
revision (same-group re-upload or a new MangaDex version with different pages) instead
deletes the old copy when it is still exactly as downloaded; a copy with manual edits
is still kept in the replaced folder.

Optional output formats (Settings → Output format): pages can be converted to PNG, JPEG,
WebP or the space-saving MIXED mode (``app.library.images.convert_page``) as they arrive, and a finished chapter can be packed
into ``{Chapter}.cbz`` (``app.library.entries``) in the staging folder before it is moved
into the library. A revision keeps the kind (folder or CBZ) of the copy it replaces.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select, update

from app.config import get_config
from app.db import session_scope
from app.enums import ErrorKind, JobStatus, LocalStatus, PageStatus, PauseReason, ReviewKind, ReviewStatus
from app.library.entries import (
    comic_info_xml,
    entry_exists,
    entry_name,
    folder_to_cbz,
    is_cbz,
    move_entry,
    remove_entry,
    split_name,
    untouched_cbz,
)
from app.library.images import convert_page, extension_from_filename, validate_image
from app.library.marker import (
    is_legacy_partial,
    list_page_files,
    read_manga_marker,
    read_release_marker,
    write_manga_marker,
    write_release_marker,
)
from app.library.naming import (
    NamingOptions,
    chapter_folder_name,
    page_filename,
    with_collision_suffix,
)
from app.library.storage import (
    StorageUnavailable,
    atomic_write_bytes,
    check_library,
    ensure_work_dir,
    fsync_dir,
    is_fatal_storage_error,
    is_storage_error,
    move_dir,
    replaced_root,
    require_library,
    resolve_rel,
    safe_join,
    staging_root,
)
from app.logging_setup import get_logger
from app.mangadex.client import AtHomeServer, MangaDexClient
from app.mangadex.errors import (
    AccessDeniedError,
    InvalidImageError,
    InvalidResponseError,
    MaintenanceError,
    MangaDexError,
    NetworkError,
    NotFoundError,
    RateLimitedError,
    ServerError,
)
from app.models import ChapterRelease, DownloadJob, Manga, PageRecord, ReviewItem
from app.ratelimit import AccessBlocked, CooldownActive, SlotTimeout, get_limiter
from app.retry import next_attempt_time
from app.services.control import record_event
from app.services.layout import (
    LayoutBusy,
    LayoutConflict,
    ensure_group_suffixes,
    ensure_layout_for_release,
    release_base_parts,
)
from app.settings_service import AppSettings, is_queue_paused, load_settings

log = get_logger(__name__)

AT_HOME_MAX_AGE_S = 10 * 60
LAYOUT_BUSY_DELAY_S = 60
HEARTBEAT_EVERY_S = 30


class JobInterrupted(Exception):
    """The job was paused/cancelled (by the user or the queue) while running."""


class DestinationConflict(Exception):
    def __init__(self, message: str, rel_path: str):
        super().__init__(message)
        self.rel_path = rel_path


@dataclass
class Paths:
    root: Path
    target_rel: str  # relative to the library root
    temp_rel: str  # folder name in the staging directory (the chapter UUID)
    staging: Path

    @property
    def target(self) -> Path:
        return resolve_rel(self.root, self.target_rel)

    @property
    def temp(self) -> Path:
        return safe_join(self.staging, self.temp_rel)


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------- entry


def run_download(job_id: int, client: MangaDexClient | None = None, root: Path | None = None) -> str:
    """Worker entry point. Returns the final job status."""
    with session_scope() as s:
        claimed = s.execute(
            update(DownloadJob)
            .where(DownloadJob.id == job_id, DownloadJob.status == JobStatus.QUEUED)
            .values(status=JobStatus.DOWNLOADING, started_at=_now(), heartbeat_at=_now())
            .returning(DownloadJob.id)
        ).first()
    if not claimed:
        log.info("download_skip_not_queued", job_id=job_id)
        return "skipped"
    if client is None:
        from app.services.control import build_client

        client = build_client()
    stop = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(job_id, stop), daemon=True)
    hb.start()
    try:
        _download(job_id, client, root)
        return JobStatus.COMPLETED
    except Exception as exc:  # noqa: BLE001 - classified below
        return handle_failure(job_id, exc)
    finally:
        stop.set()
        hb.join(timeout=5)


def _heartbeat_loop(job_id: int, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_EVERY_S):
        try:
            with session_scope() as s:
                s.execute(
                    update(DownloadJob)
                    .where(DownloadJob.id == job_id, DownloadJob.status == JobStatus.DOWNLOADING)
                    .values(heartbeat_at=_now())
                )
        except Exception:
            log.warning("heartbeat_failed", job_id=job_id)


def _check_running(job_id: int) -> None:
    paused = False
    with session_scope() as s:
        status = s.scalar(select(DownloadJob.status).where(DownloadJob.id == job_id))
        if status != JobStatus.DOWNLOADING:
            raise JobInterrupted(f"Job is now {status}")
        if is_queue_paused(s):
            s.execute(
                update(DownloadJob)
                .where(DownloadJob.id == job_id, DownloadJob.status == JobStatus.DOWNLOADING)
                .values(status=JobStatus.PAUSED, pause_reason=PauseReason.QUEUE, heartbeat_at=None)
            )
            paused = True
    # Raised only after session_scope has committed: raising inside it would roll
    # the status change back and leave the job "downloading" until recovered.
    if paused:
        raise JobInterrupted("Download queue paused")


# --------------------------------------------------------------------- paths


def _naming(settings: AppSettings) -> NamingOptions:
    return NamingOptions(
        volume_padding=settings.volume_padding,
        chapter_padding_with_volume=settings.chapter_padding_with_volume,
        chapter_padding_without_volume=settings.chapter_padding_without_volume,
        volume_placeholder=settings.volume_placeholder,
        group_in_name=settings.group_in_chapter_name,
    )


def desired_chapter_folder_name(release: ChapterRelease, settings: AppSettings, with_group: bool) -> str:
    """The chapter folder name the current naming settings give ``release``.

    ``with_group``: append " - {Group}" (only when the same chapter is in the library
    from another group too, see ``app.services.layout.group_suffix_ids``).
    """
    opts = _naming(settings)
    from dataclasses import replace

    opts = replace(opts, group_in_name=bool(with_group and settings.group_in_chapter_name))
    return chapter_folder_name(release.volume, release.chapter, release.title, opts, release.group_names or [])


def compute_paths(session, root: Path, job: DownloadJob, release: ChapterRelease, settings: AppSettings,
                  staging: Path | None = None) -> Paths:
    """Decide the final folder (with deterministic collision handling) and temp folder."""
    staging = staging if staging is not None else staging_root(root)
    manga = session.get(Manga, release.manga_id)
    # {Manga}[/{Language}][/{Group}]: levels only exist when needed (app.services.layout).
    base_parts = release_base_parts(session, manga, release)
    safe_join(root, *base_parts)  # validates components
    temp_rel = str(release.id)  # flat folder in the staging directory

    if job.target_rel_path:
        if resolve_rel(root, job.target_rel_path).exists() or job.target_rel_path.rsplit("/", 1)[0] != "/".join(base_parts):
            return Paths(root, job.target_rel_path, temp_rel, staging)
        # Not created yet: name it again (a second group may now need " - {Group}").

    if job.is_refresh and release.folder_rel_path and \
            release.folder_rel_path.rsplit("/", 1)[0] == "/".join(base_parts) and \
            entry_exists(resolve_rel(root, release.folder_rel_path)):
        # New MangaDex version: replace the copy in place, keeping its kind (folder or CBZ).
        return Paths(root, release.folder_rel_path, temp_rel, staging)

    if job.replaces_release_id:
        # Same-group re-upload: the new copy takes the old one's folder (old one -> .mdm-replaced/).
        old = session.get(ChapterRelease, job.replaces_release_id)
        if old is not None and old.folder_rel_path and old.folder_rel_path.rsplit("/", 1)[0] == "/".join(base_parts):
            return Paths(root, old.folder_rel_path, temp_rel, staging)

    from app.services.layout import group_suffix_ids

    name = desired_chapter_folder_name(release, settings,
                                       release.id in group_suffix_ids(session, root, manga.id, release))
    as_cbz = settings.chapter_cbz
    for candidate in (entry_name(name, as_cbz), entry_name(with_collision_suffix(name, str(release.id)), as_cbz)):
        rel = "/".join([*base_parts, candidate])
        owner = session.scalar(
            select(ChapterRelease.id).where(ChapterRelease.folder_rel_path == rel, ChapterRelease.id != release.id)
        )
        if owner is not None:
            continue  # belongs to a different release in the DB
        path = safe_join(root, *base_parts, candidate)
        if path.exists():
            marker = read_release_marker(path)
            if marker and marker.get("chapter_id") == str(release.id):
                return Paths(root, rel, temp_rel, staging)  # our own folder (adopt / redownload)
            if marker:
                continue  # a different release lives here
            raise DestinationConflict(
                f"Folder '{candidate}' already exists but is not linked to this release. "
                "Run a library scan or resolve it in Review.",
                rel,
            )
        return Paths(root, rel, temp_rel, staging)
    raise DestinationConflict("Both the normal and the suffixed folder name are taken",
                              "/".join([*base_parts, entry_name(name, as_cbz)]))


# --------------------------------------------------------------------- download


def _migrate_legacy_partial(root: Path, legacy_rel: str, paths: Paths) -> None:
    """Move a partial download left inside the library (older versions) to the staging folder."""
    src = resolve_rel(root, legacy_rel)
    if not src.is_dir():
        return
    dst = paths.temp
    if dst.exists():
        log.warning("legacy_partial_kept", path=legacy_rel, reason="staging folder already exists")
        return
    move_dir(src, dst)
    log.info("legacy_partial_moved", path=legacy_rel, to=str(dst))


def _download(job_id: int, client: MangaDexClient, root: Path | None) -> None:
    root = require_library(root)
    staging = ensure_work_dir(staging_root(root))
    with session_scope() as s:
        # A new language or scanlation group may need new folder levels: move what is
        # already on disk into the new structure before downloading anything.
        ensure_layout_for_release(s, root, s.get(DownloadJob, job_id).release_id, job_id)
    with session_scope() as s:
        # Same chapter from a second group: the copy already in the library gets " - {Group}" first.
        ensure_group_suffixes(s, root, s.get(DownloadJob, job_id).release_id, job_id)
    with session_scope() as s:
        settings = load_settings(s)
        job = s.get(DownloadJob, job_id)
        release = s.get(ChapterRelease, job.release_id)
        manga = s.get(Manga, release.manga_id)
        paths = compute_paths(s, root, job, release, settings, staging)
        if is_legacy_partial(job.temp_dir_rel):
            _migrate_legacy_partial(root, job.temp_dir_rel, paths)
        job.target_rel_path = paths.target_rel
        job.temp_dir_rel = paths.temp_rel
        release_id = release.id
        redownload = job.is_redownload
        refresh = job.is_refresh
        replaces = job.replaces_release_id
        old_release = s.get(ChapterRelease, replaces) if replaces else None
        old_rel = (old_release.folder_rel_path or None) if old_release is not None else None
        own_rel = release.folder_rel_path
        # When the chapter being revised was downloaded (the revision window counts from it).
        revised_downloaded_at = (old_release if old_release is not None else release).downloaded_at
        revision_days = settings.revision_after_move_days
        known_sources = [m.get("source") for m in (release.page_manifest or []) if isinstance(m, dict)]
        manga_id, manga_title, manga_folder = manga.id, manga.display_title, manga.folder_name
        record_event(s, "started", f"Attempt {job.attempts + 1}", job_id=job.id, release_id=release_id,
                     manga_id=manga_id)

    # A group's revision replaces a copy that is still in the library. If the user already
    # moved the chapter out (e.g. packed into a volume), the revision goes to the replaced
    # folder when the chapter was downloaded within the revision window, else it is ignored.
    moved_out = False
    if refresh or replaces:
        prev_rel = old_rel if replaces else own_rel
        if not prev_rel or not entry_exists(resolve_rel(root, prev_rel)):
            if prev_rel and in_revision_window(revised_downloaded_at, revision_days):
                moved_out = True
            else:
                _revision_skipped(job_id, release_id, replaces, prev_rel)
                return

    # Already present on disk (e.g. downloaded before the DB was restored)?
    target = paths.target
    if target.exists() and not redownload and not replaces:
        _adopt_existing(job_id, release_id, paths)
        return

    at_home = client.get_at_home(str(release_id))
    files = at_home.files(settings.use_data_saver)
    if not files:
        raise InvalidResponseError("MangaDex returned no pages for this chapter")
    if refresh and (target.exists() or moved_out) and known_sources == list(files):
        _version_unchanged(job_id, release_id, moved_out)
        return

    if not moved_out:  # a revision of a chapter moved out never creates anything in the library
        manga_dir = safe_join(root, manga_folder)
        manga_dir.mkdir(exist_ok=True)
        if read_manga_marker(manga_dir) is None:
            write_manga_marker(manga_dir, str(manga_id), manga_title)
    temp = paths.temp
    temp.mkdir(exist_ok=True)  # never parents: the staging folder was checked above
    total = len(files)
    pending = _sync_page_records(release_id, files, temp, total)
    with session_scope() as s:
        s.execute(update(DownloadJob).where(DownloadJob.id == job_id)
                  .values(pages_total=total, pages_done=total - len(pending)))

    if pending:
        _download_pages(job_id, release_id, client, at_home, pending, temp, total, settings)
    _finalize(job_id, release_id, paths, redownload, replaces, refresh, moved_out)


def in_revision_window(downloaded_at: datetime | None, days: int) -> bool:
    """Was the chapter downloaded recently enough for a revision to be kept after it was moved out?"""
    return days > 0 and downloaded_at is not None and _now() - downloaded_at <= timedelta(days=days)


def _finalize_moved_out(s, job_id: int, release: ChapterRelease, old: ChapterRelease | None,
                        paths: Paths, manifest: list[dict]) -> None:
    """Revision of a chapter the user moved out of the library: keep it in the replaced folder only.

    The library is never touched. A review item tells the user that a newer copy arrived.
    """
    base = ensure_work_dir(replaced_root(paths.root))
    parents = [p for p in paths.target_rel.split("/")[:-1] if p]
    dest_dir = safe_join(base, *parents) if parents else base
    dest_dir.mkdir(parents=True, exist_ok=True)  # below the checked replaced folder only
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem, ext = split_name(paths.target_rel.rsplit("/", 1)[-1])
    dest = safe_join(dest_dir, f"{stem} {stamp}{ext}")
    _move_finished(paths, dest)
    replaced_rel = dest.relative_to(base).as_posix()

    revised = old if old is not None else release
    library_rel = revised.folder_rel_path
    first_downloaded = revised.downloaded_at
    if revised.local_status in (LocalStatus.DOWNLOADED, LocalStatus.IMPORTED, LocalStatus.MODIFIED):
        revised.local_status = LocalStatus.MISSING_ON_DISK  # the folder is gone: same as a library scan
    if old is not None:
        release.local_status = LocalStatus.IN_REPLACED
        release.folder_rel_path = None
        release.downloaded_at = _now()
        kind, what = "reupload", "Re-upload by the same group"
    else:
        kind, what = "version", f"New MangaDex version {release.version}"
    release.downloaded_version = release.version
    release.page_manifest = manifest
    release.local_page_count = len(manifest)
    release.local_checked_at = _now()
    job = s.get(DownloadJob, job_id)
    job.status = JobStatus.COMPLETED
    job.finished_at = _now()
    job.pages_done = len(manifest)
    job.last_error = None
    job.error_kind = None
    job.next_attempt_at = None
    groups = [n for n in (release.group_names or []) if n]
    message = (f"{what} of a chapter you had already moved out of the library "
               f"({library_rel or '-'}): saved in the replaced folder as {replaced_rel}")
    path = f"{base.name}/{replaced_rel}"
    if s.scalar(select(ReviewItem.id).where(ReviewItem.path == path, ReviewItem.status == ReviewStatus.OPEN)) is None:
        s.add(ReviewItem(
            kind=ReviewKind.REVISION_AFTER_MOVE, manga_id=release.manga_id, path=path,
            details={
                "revision": kind, "message": message, "release_id": str(release.id),
                "revised_release_id": str(revised.id), "replaced_path": replaced_rel,
                "library_path": library_rel, "volume": release.volume, "chapter": release.chapter,
                "title": release.title, "language": release.language_code, "groups": groups,
                "version": release.version, "pages": len(manifest), "job_id": job_id,
                "first_downloaded_at": first_downloaded.isoformat() if first_downloaded else None,
                "mangadex_url": f"https://mangadex.org/chapter/{release.id}",
            },
        ))
    if old is not None:
        record_event(s, "replaced", f"Superseded by a re-upload saved in the replaced folder: {library_rel or '-'}",
                     job_id=job_id, release_id=old.id, manga_id=old.manga_id)
    record_event(s, "revision_to_replaced", message, job_id=job_id, release_id=release.id,
                 manga_id=release.manga_id)
    log.info("revision_to_replaced", job_id=job_id, release_id=str(release.id), path=replaced_rel)


def _revision_skipped(job_id: int, release_id: uuid.UUID, replaces: uuid.UUID | None,
                      prev_rel: str | None) -> None:
    """A group's revision of a chapter that is no longer in the library: download nothing."""
    with session_scope() as s:
        release = s.get(ChapterRelease, release_id)
        if replaces:
            old = s.get(ChapterRelease, replaces)
            if old is not None and old.local_status in (LocalStatus.DOWNLOADED, LocalStatus.IMPORTED,
                                                        LocalStatus.MODIFIED):
                old.local_status = LocalStatus.MISSING_ON_DISK
            release.local_status = LocalStatus.SKIPPED  # can be un-skipped from the UI
            msg = f"Re-upload by the same group not downloaded: the old copy is no longer in the library ({prev_rel or '-'})"
        else:
            release.local_status = LocalStatus.MISSING_ON_DISK
            msg = (f"New MangaDex version {release.version} not downloaded: the chapter is no longer "
                   f"in the library ({prev_rel or '-'})")
        job = s.get(DownloadJob, job_id)
        job.status = JobStatus.CANCELLED
        job.finished_at = _now()
        job.last_error = None
        job.error_kind = None
        job.next_attempt_at = None
        record_event(s, "revision_skipped", msg, job_id=job_id, release_id=release_id, manga_id=release.manga_id)
    log.info("revision_skipped", job_id=job_id, release_id=str(release_id), previous=prev_rel)


def _version_unchanged(job_id: int, release_id: uuid.UUID, moved_out: bool = False) -> None:
    """Version check: MangaDex lists the same pages we have, nothing to download."""
    with session_scope() as s:
        release = s.get(ChapterRelease, release_id)
        release.downloaded_version = release.version
        if moved_out and release.local_status in (LocalStatus.DOWNLOADED, LocalStatus.MODIFIED):
            release.local_status = LocalStatus.MISSING_ON_DISK  # the folder is gone: same as a library scan
        job = s.get(DownloadJob, job_id)
        job.status = JobStatus.COMPLETED
        job.finished_at = _now()
        job.pages_done = job.pages_total or 0
        job.last_error = None
        job.error_kind = None
        record_event(s, "unchanged", f"New MangaDex version {release.version}: pages unchanged, nothing downloaded",
                     job_id=job_id, release_id=release_id, manga_id=release.manga_id)


def _backup(root: Path, rel: str) -> Path:
    """Move a chapter folder out of the library into the replaced-copies folder.

    Kept (it may contain manual edits) as ``{replaced}/{Manga}/[...]/{Chapter} {timestamp}``.
    """
    src = resolve_rel(root, rel)
    base = ensure_work_dir(replaced_root(root))
    parents = [p for p in rel.split("/")[:-1] if p]
    dest_dir = safe_join(base, *parents) if parents else base
    dest_dir.mkdir(parents=True, exist_ok=True)  # below the checked replaced folder only
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem, ext = (src.name, "") if src.is_dir() else split_name(src.name)
    dest = safe_join(dest_dir, f"{stem} {stamp}{ext}")
    move_entry(src, dest)
    return dest


DISCARD_PREFIX = ".mdm-discard-"
_IGNORED_EXTRA_FILES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})


def is_untouched_copy(folder: Path) -> bool:
    """True when ``folder`` holds exactly the pages its release marker lists, unchanged.

    Every page must be there with the recorded size and SHA-256, and nothing else may be in
    the folder except the marker and file-manager junk. Anything else (a page removed,
    edited or added, a subfolder, no marker) counts as a manual edit.
    """
    from app.library.marker import RELEASE_MARKER

    marker = read_release_marker(folder)
    pages = (marker or {}).get("pages") or []
    expected = {m.get("file"): m for m in pages if isinstance(m, dict) and m.get("file")}
    if not expected or len(expected) != len(pages):
        return False
    if is_cbz(folder) and not folder.is_dir():
        return untouched_cbz(folder, expected, RELEASE_MARKER)
    try:
        for entry in folder.iterdir():
            if entry.name == RELEASE_MARKER or entry.name.lower() in _IGNORED_EXTRA_FILES:
                continue
            if entry.is_symlink() or not entry.is_file() or entry.name not in expected:
                return False
        for name, m in expected.items():
            f = folder / name
            if not f.is_file() or f.stat().st_size != m.get("size") or _sha256(f) != m.get("sha256"):
                return False
    except OSError:
        return False
    return True


def _set_aside(root: Path, rel: str, staging: Path) -> tuple[Path | None, Path | None]:
    """Take the previous copy of a revised chapter out of the library.

    Untouched copy: moved to a hidden ``.mdm-discard-*`` folder in staging, deleted once the
    new copy is in place -> (discard, None). Edited copy: kept in the replaced folder -> (None, backup).
    """
    src = resolve_rel(root, rel)
    if is_untouched_copy(src):
        ext = "" if src.is_dir() else split_name(src.name)[1]
        dest = safe_join(staging, f"{DISCARD_PREFIX}{uuid.uuid4().hex[:12]}{ext}")
        move_entry(src, dest)
        return dest, None
    return None, _backup(root, rel)


def _old_copy_fate(kept_backup: bool, discard: list[Path]) -> str:
    if kept_backup:
        return "old copy had manual edits, kept in the replaced folder"
    return "old copy deleted" if discard else "old copy was no longer in the library"


def _drop_discarded(discard: list[Path]) -> None:
    for d in discard:
        try:
            remove_entry(d)
        except OSError as exc:
            log.warning("discard_remove_failed", path=str(d), error=str(exc))


def _sync_page_records(release_id: uuid.UUID, files: list[str], temp: Path, total: int) -> list[PageRecord]:
    """Create/reuse page records; re-verify pages already marked complete."""
    with session_scope() as s:
        records = {r.page_index: r for r in s.scalars(select(PageRecord).where(PageRecord.release_id == release_id))}
        mismatch = len(records) != len(files) or any(
            records.get(i + 1) is None or records[i + 1].source_filename != f for i, f in enumerate(files)
        )
        if records and mismatch:
            # The chapter was re-uploaded with different pages: start the page list over.
            s.execute(delete(PageRecord).where(PageRecord.release_id == release_id))
            s.flush()
            records = {}
        if not records:
            for i, f in enumerate(files, start=1):
                s.add(PageRecord(release_id=release_id, page_index=i, source_filename=f, status=PageStatus.PENDING))
            s.flush()
            records = {r.page_index: r for r in s.scalars(select(PageRecord).where(PageRecord.release_id == release_id))}
        pending = []
        for idx in sorted(records):
            r = records[idx]
            if r.status == PageStatus.COMPLETED:
                p = temp / (r.local_filename or "")
                if r.local_filename and p.is_file() and p.stat().st_size == r.size_bytes and _sha256(p) == r.sha256:
                    continue
                r.status = PageStatus.PENDING  # missing or corrupt on disk: fetch again
                r.completed_at = None
            pending.append(r)
        s.flush()
        s.expunge_all()
        return pending


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class _AtHomeHolder:
    def __init__(self, client: MangaDexClient, release_id: uuid.UUID, server: AtHomeServer):
        self.client = client
        self.release_id = release_id
        self.server = server
        self.lock = threading.Lock()

    def get(self) -> AtHomeServer:
        with self.lock:
            if time.time() - self.server.fetched_at > AT_HOME_MAX_AGE_S:
                self.server = self.client.get_at_home(str(self.release_id))
            return self.server

    def refresh(self, stale: AtHomeServer) -> AtHomeServer:
        with self.lock:
            if self.server is stale:
                self.server = self.client.get_at_home(str(self.release_id))
            return self.server


def _download_pages(job_id, release_id, client, at_home, pending, temp, total, settings: AppSettings) -> None:
    holder = _AtHomeHolder(client, release_id, at_home)
    limiter = get_limiter()
    workers = settings.max_concurrent_pages

    def fetch(rec: PageRecord) -> tuple[PageRecord, str, int, str]:
        last_exc: Exception | None = None
        for attempt in range(1, settings.page_retry_limit + 1):
            server = holder.get()
            url = server.page_url(rec.source_filename, settings.use_data_saver)
            try:
                with limiter.slot("pages", settings.max_concurrent_pages, ttl_s=300):
                    resp = client.download_image(url)
                ext = validate_image(resp.content, resp.content_type, resp.content_length, rec.source_filename)
                client.report(url, True, len(resp.content), resp.duration_ms, resp.cached)
                src_ext = extension_from_filename(rec.source_filename)
                final_ext = src_ext if src_ext == ext else ext
                data = resp.content
                if settings.page_format != "original":
                    data, final_ext = convert_page(data, final_ext, settings.page_format, settings.webp_quality,
                                                   settings.jpeg_quality)
                name = page_filename(rec.page_index, final_ext, total)
                atomic_write_bytes(temp / name, data)
                return rec, name, len(data), hashlib.sha256(data).hexdigest()
            except MaintenanceError:
                raise  # all of MangaDex is offline: no point trying other nodes
            except (InvalidImageError, NetworkError, ServerError, AccessDeniedError) as exc:
                # Typical for a misbehaving MD@Home node: report it, get a fresh node, retry this page only.
                last_exc = exc
                client.report(url, False, 0, 0, False)
                _page_failed(rec.id, exc)
                if attempt < settings.page_retry_limit:
                    time.sleep(min(2 ** attempt, 10))
                    holder.refresh(server)
        assert last_exc is not None
        raise last_exc

    queue = list(pending)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"job{job_id}") as pool:
        running: set[Future] = set()
        try:
            while queue or running:
                while queue and len(running) < workers:
                    _check_running(job_id)
                    running.add(pool.submit(fetch, queue.pop(0)))
                done, running = wait(running, return_when=FIRST_COMPLETED)
                for fut in done:
                    rec, name, size, digest = fut.result()  # re-raises page errors
                    _page_completed(job_id, rec.id, name, size, digest)
        except BaseException:
            for fut in running:
                fut.cancel()
            raise


def _page_completed(job_id: int, page_id: int, name: str, size: int, digest: str) -> None:
    with session_scope() as s:
        s.execute(
            update(PageRecord).where(PageRecord.id == page_id).values(
                status=PageStatus.COMPLETED, local_filename=name, size_bytes=size, sha256=digest,
                completed_at=_now(), last_error=None,
            )
        )
        s.execute(
            update(DownloadJob).where(DownloadJob.id == job_id)
            .values(pages_done=DownloadJob.pages_done + 1, heartbeat_at=_now())
        )


def _page_failed(page_id: int, exc: Exception) -> None:
    try:
        with session_scope() as s:
            s.execute(update(PageRecord).where(PageRecord.id == page_id)
                      .values(attempts=PageRecord.attempts + 1, last_error=str(exc)[:500]))
    except Exception:
        pass


def _finalize(job_id: int, release_id: uuid.UUID, paths: Paths, redownload: bool,
              replaces: uuid.UUID | None = None, refresh: bool = False, moved_out: bool = False) -> None:
    temp, target = paths.temp, paths.target
    discard: list[Path] = []  # untouched previous copies, deleted after the commit
    kept_backup = False  # an edited previous copy went to the replaced folder
    with session_scope() as s:
        old = s.get(ChapterRelease, replaces) if replaces else None
        records = list(s.scalars(select(PageRecord).where(PageRecord.release_id == release_id)
                                 .order_by(PageRecord.page_index)))
        release = s.get(ChapterRelease, release_id)
        manifest = []
        for r in records:
            if r.status != PageStatus.COMPLETED or not r.local_filename:
                raise InvalidResponseError(f"Page {r.page_index} is not complete")
            p = temp / r.local_filename
            if not p.is_file() or p.stat().st_size != r.size_bytes:
                raise InvalidImageError(f"Page {r.page_index} is missing or incomplete on disk")
            manifest.append({"index": r.page_index, "file": r.local_filename, "size": r.size_bytes,
                             "sha256": r.sha256, "source": r.source_filename})
        expected = {m["file"] for m in manifest}
        # Remove stale page files from an earlier, different page list (our own temp data only).
        for name in list_page_files(temp):
            if name not in expected:
                (temp / name).unlink()
        for leftover in temp.glob(".*.tmp"):
            leftover.unlink(missing_ok=True)
        marker_manga = s.get(Manga, release.manga_id)
        _drop_stale_pack(paths)
        write_release_marker(temp, {
            "chapter_id": str(release.id),
            "manga_id": str(release.manga_id),
            "manga_title": marker_manga.display_title if marker_manga else None,
            "language": release.language_code,
            "volume": release.volume,
            "chapter": release.chapter,
            "title": release.title,
            "scanlation_group": " & ".join(n for n in (release.group_names or []) if n) or None,
            "groups": [{"id": g, "name": n} for g, n in zip(release.group_ids, release.group_names, strict=False)],
            "mangadex_url": f"https://mangadex.org/chapter/{release.id}",
            "published_at": release.publish_at.isoformat() if release.publish_at else None,
            "pages": manifest,
        })
        fsync_dir(temp)
        if is_cbz(paths.target_rel):
            # One .cbz per chapter: packed in the staging folder, moved in like a folder.
            folder_to_cbz(temp, _packed(paths),
                          {"ComicInfo.xml": _chapter_comic_info(release, marker_manga, len(manifest))})

        # The NAS may have gone away during the download: never create library folders then.
        lib = check_library(paths.root, write_probe=False)
        if not lib.ok:
            raise StorageUnavailable(lib.error or "Library unavailable")
        if moved_out:
            _finalize_moved_out(s, job_id, release, old, paths, manifest)
            return
        replacing_here = old is not None and old.folder_rel_path == paths.target_rel
        if target.exists():
            marker = read_release_marker(target)
            own = redownload and marker and marker.get("chapter_id") == str(release.id)
            if not (own or replacing_here):
                raise DestinationConflict("Destination folder appeared during download", paths.target_rel)
            if replacing_here or refresh:
                # The group's revision: an untouched old copy is deleted, an edited one kept.
                d, b = _set_aside(paths.root, paths.target_rel, paths.staging)
                if d is not None:
                    discard.append(d)
                kept_backup = kept_backup or b is not None
            else:
                # Manual redownload: keep the previous copy (it may contain manual edits).
                _backup(paths.root, paths.target_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            _move_finished(paths, target)
        except BaseException:
            if len(discard) == 1 and not target.exists() and is_cbz(discard[0]) == is_cbz(target):
                move_entry(discard.pop(), target)  # put the old copy back
            raise
        if old is not None:
            old_rel = old.folder_rel_path
            if old_rel and not replacing_here:
                old_path = resolve_rel(paths.root, old_rel)
                if old_path.exists():
                    d, b = _set_aside(paths.root, old_rel, paths.staging)
                    if d is not None:
                        discard.append(d)
                    kept_backup = kept_backup or b is not None
            old.local_status = LocalStatus.REPLACED
            old.folder_rel_path = None
            s.flush()  # frees the folder path (unique) before the new release takes it
            how = _old_copy_fate(kept_backup, discard)
            record_event(s, "replaced", f"Replaced by a re-upload of the same group ({how}): "
                         f"{old_rel or '-'}", job_id=job_id, release_id=old.id, manga_id=old.manga_id)
        elif refresh:
            how = _old_copy_fate(kept_backup, discard)
            record_event(s, "revised", f"New MangaDex version {release.version} ({how}): {paths.target_rel}",
                         job_id=job_id, release_id=release_id, manga_id=release.manga_id)

        release.local_status = LocalStatus.DOWNLOADED
        release.folder_rel_path = paths.target_rel
        release.downloaded_version = release.version
        release.downloaded_at = _now()
        release.page_manifest = manifest
        release.local_page_count = len(manifest)
        release.local_checked_at = _now()
        job = s.get(DownloadJob, job_id)
        job.status = JobStatus.COMPLETED
        job.finished_at = _now()
        job.pages_done = len(manifest)
        job.last_error = None
        job.error_kind = None
        job.next_attempt_at = None
        record_event(s, "completed", f"{len(manifest)} pages → {paths.target_rel}", job_id=job_id,
                     release_id=release_id, manga_id=release.manga_id)
    _drop_discarded(discard)
    log.info("download_completed", job_id=job_id, release_id=str(release_id), pages=len(manifest))
    try:
        from app.services.volumes import enqueue_auto_volumes

        enqueue_auto_volumes(_manga_of(release_id))
    except Exception:  # noqa: BLE001  (never fail a finished download for this)
        log.warning("auto_volume_enqueue_failed", release_id=str(release_id), exc_info=True)


def _manga_of(release_id: uuid.UUID) -> uuid.UUID | None:
    with session_scope() as s:
        r = s.get(ChapterRelease, release_id)
        return r.manga_id if r is not None else None


def _packed(paths: Paths) -> Path:
    """The chapter CBZ built in the staging folder before it moves into the library."""
    return safe_join(paths.staging, f"{paths.temp_rel}.cbz")


def _drop_stale_pack(paths: Paths) -> None:
    p = _packed(paths)
    if p.is_file():
        p.unlink()  # from an earlier attempt: rebuilt from the verified pages


def _move_finished(paths: Paths, dest: Path) -> None:
    """Move the finished chapter (its folder, or the CBZ packed from it) to ``dest``."""
    if is_cbz(dest):
        move_entry(_packed(paths), dest)
        shutil.rmtree(paths.temp, ignore_errors=True)  # its pages are in the CBZ now
    else:
        move_dir(paths.temp, dest)


def _chapter_comic_info(release: ChapterRelease, manga: Manga | None, pages: int) -> bytes:
    pub = release.publish_at
    return comic_info_xml({
        "Title": release.title,
        "Series": manga.display_title if manga else None,
        "Number": release.chapter,
        "Volume": release.volume,
        "Year": pub.year if pub else None, "Month": pub.month if pub else None, "Day": pub.day if pub else None,
        "Web": f"https://mangadex.org/chapter/{release.id}",
        "ScanInformation": " & ".join(n for n in (release.group_names or []) if n) or None,
        "LanguageISO": release.language_code,
        "PageCount": pages,
        "Manga": "Yes",
        "Notes": "Downloaded from MangaDex by MangaDex WebDL",
    })


def _adopt_existing(job_id: int, release_id: uuid.UUID, paths: Paths) -> None:
    """The release folder already exists with our marker: record it, download nothing."""
    target = paths.target
    marker = read_release_marker(target) or {}
    files = list_page_files(target)
    manifest = marker.get("pages") or []
    modified = bool(manifest) and sorted(m.get("file") for m in manifest) != files
    with session_scope() as s:
        release = s.get(ChapterRelease, release_id)
        release.local_status = LocalStatus.MODIFIED if modified else LocalStatus.DOWNLOADED
        release.folder_rel_path = paths.target_rel
        release.page_manifest = manifest or release.page_manifest
        release.local_page_count = len(files)
        release.local_checked_at = _now()
        release.downloaded_at = release.downloaded_at or _now()
        if release.downloaded_version is None:
            release.downloaded_version = release.version
        job = s.get(DownloadJob, job_id)
        job.status = JobStatus.COMPLETED
        job.finished_at = _now()
        job.pages_done = len(files)
        record_event(s, "adopted", f"Existing folder recognised: {paths.target_rel}", job_id=job_id,
                     release_id=release_id, manga_id=release.manga_id)


# --------------------------------------------------------------------- failures


def classify(exc: BaseException) -> tuple[str, ErrorKind | None, str]:
    """Map an exception to (action, error kind, message).

    Actions: retry | cooldown | pause_storage | pause_blocked | attention | interrupted
    """
    if isinstance(exc, JobInterrupted):
        return "interrupted", None, str(exc)
    if isinstance(exc, CooldownActive):
        return "cooldown", ErrorKind.MAINTENANCE if exc.is_maintenance else ErrorKind.RATE_LIMIT, str(exc)
    if isinstance(exc, MaintenanceError) and exc.until_ms:
        return "cooldown", ErrorKind.MAINTENANCE, "MangaDex is down for maintenance"
    if isinstance(exc, AccessBlocked):
        return "pause_blocked", ErrorKind.ACCESS, str(exc)
    if isinstance(exc, DestinationConflict):
        return "attention", ErrorKind.CONFLICT, str(exc)
    if isinstance(exc, LayoutBusy):
        return "defer", None, str(exc)
    if isinstance(exc, LayoutConflict):
        return "attention", ErrorKind.CONFLICT, f"Cannot reorganise the manga folders: {exc}"
    if isinstance(exc, StorageUnavailable) or is_storage_error(exc):
        return "pause_storage", ErrorKind.STORAGE, f"Library unavailable: {exc}"
    if is_fatal_storage_error(exc):
        return "attention", ErrorKind.STORAGE, f"Filesystem error (check permissions/free space): {exc}"
    if isinstance(exc, RateLimitedError):
        return "retry", ErrorKind.RATE_LIMIT, str(exc)
    if isinstance(exc, AccessDeniedError):
        if get_limiter().block_state():
            return "pause_blocked", ErrorKind.ACCESS, str(exc)
        return "attention", ErrorKind.ACCESS, f"{exc} while fetching images"
    if isinstance(exc, NotFoundError):
        return "attention", ErrorKind.NOT_FOUND, "Chapter not found on MangaDex (removed or unavailable)"
    if isinstance(exc, MangaDexError) and exc.status and 400 <= exc.status < 500:
        return "attention", ErrorKind.INVALID_RESPONSE, str(exc)
    if isinstance(exc, MangaDexError):
        return "retry", exc.kind, str(exc)
    if isinstance(exc, SlotTimeout):
        return "retry", ErrorKind.NETWORK, str(exc)
    if isinstance(exc, OSError):
        return "attention", ErrorKind.STORAGE, f"Filesystem error: {exc}"
    return "retry", ErrorKind.INTERNAL, f"Unexpected error: {exc.__class__.__name__}: {exc}"


def handle_failure(job_id: int, exc: BaseException) -> str:
    action, kind, message = classify(exc)
    log_fn = log.info if action in {"interrupted", "cooldown", "pause_storage", "defer"} else log.warning
    log_fn("download_failed", job_id=job_id, action=action, kind=str(kind), error=message[:300])
    with session_scope() as s:
        job = s.get(DownloadJob, job_id, with_for_update=True)
        if job is None:
            return "missing"
        if action == "interrupted":
            return job.status
        if job.status != JobStatus.DOWNLOADING:
            return job.status  # changed by the user meanwhile; keep their decision
        settings = load_settings(s)
        job.last_error = message[:2000]
        job.error_kind = str(kind) if kind else None
        job.heartbeat_at = None
        now = _now()
        if action == "defer":  # not a failure: try again shortly without using up an attempt
            job.status = JobStatus.RETRY_SCHEDULED
            job.next_attempt_at = now + timedelta(seconds=LAYOUT_BUSY_DELAY_S)
        elif action == "cooldown":
            job.status = JobStatus.RETRY_SCHEDULED
            job.next_attempt_at = datetime.fromtimestamp(exc.until_ms / 1000, UTC)  # type: ignore[attr-defined]
        elif action == "pause_storage":
            job.status = JobStatus.PAUSED
            job.pause_reason = PauseReason.STORAGE
        elif action == "pause_blocked":
            job.status = JobStatus.PAUSED
            job.pause_reason = PauseReason.BLOCKED
        elif action == "attention":
            job.status = JobStatus.NEEDS_ATTENTION
            job.finished_at = now
            if kind == ErrorKind.NOT_FOUND:
                release = s.get(ChapterRelease, job.release_id)
                if release:
                    release.is_unavailable = True
            if isinstance(exc, DestinationConflict):
                _open_conflict_review(s, job, exc.rel_path, message)
        else:  # retry
            job.attempts += 1
            if job.attempts > settings.max_retries:
                job.status = JobStatus.FAILED
                job.finished_at = now
                job.last_error = f"Gave up after {job.attempts} attempts: {message}"[:2000]
            else:
                job.status = JobStatus.RETRY_SCHEDULED
                nxt = next_attempt_time(job.attempts, settings.initial_retry_delay_seconds,
                                        settings.max_retry_delay_seconds, now)
                cd = get_limiter().cooldown_state()
                if cd:
                    nxt = max(nxt, datetime.fromtimestamp(cd["until_ms"] / 1000, UTC))
                job.next_attempt_at = nxt
        record_event(s, job.status, message[:500], job_id=job.id, release_id=job.release_id)
        return job.status


def _open_conflict_review(s, job: DownloadJob, rel_path: str, message: str) -> None:
    existing = s.scalar(select(ReviewItem).where(ReviewItem.path == rel_path, ReviewItem.status == ReviewStatus.OPEN))
    if existing:
        return
    release = s.get(ChapterRelease, job.release_id)
    s.add(ReviewItem(
        kind=ReviewKind.DESTINATION_CONFLICT,
        manga_id=release.manga_id if release else None,
        path=rel_path,
        details={"message": message, "job_id": job.id},
        candidates=[{"release_id": str(job.release_id)}],
    ))


def downloads_root() -> Path:
    return get_config().downloads_root
