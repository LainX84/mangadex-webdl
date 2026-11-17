"""Volume CBZ: one archive per complete volume, next to the chapters in the manga folder.

::

    {library}/{Manga}/[{Language}/]Volume 03.cbz
        Volume 03 Chapter 12 - A New Challenge/001.jpg …
        Volume 03 Chapter 13/001.jpg …
        ComicInfo.xml

Each chapter keeps its folder name (without ``.cbz``) as a folder inside the archive, so
readers and the archive index (``app.services.archive``) see the chapter boundaries.

**When is a volume complete?** For one language, a numbered volume *V* of a title is
*ready* when

* it is **closed**: MangaDex lists a chapter in a later volume in that language, or the
  title is completed and *V* is its last volume;
* every chapter MangaDex lists in *V* (any group, external links excluded) has a copy in
  the library right now (downloaded, imported or edited locally); and
* the chapter numbers show **no gap** — inside the volume, and from the end of the
  previous volume (titles numbered per volume start again at 1).

A gap only means MangaDex lists no chapter for that number: the uploaders did not tag it,
or it was never uploaded. Such a volume is *ready with warnings* and can still be built on
request; a volume that is not closed yet can be built on request too. The automatic mode
(``AppSettings.volume_cbz_auto``) only builds volumes that are ready without warnings.

**Building**: the archive is written in the staging folder (stored, not deflated) and checked
(``verify_volume_cbz``: ZIP structure and CRCs, member list, size and SHA-256 of every page
re-read from disk, every page decoded as an image), moved into the manga folder (never over an
existing file) and checked again there. Only then is it recorded, and each packed chapter
folder / chapter CBZ is **deleted** — only if it is still exactly what was packed and holds
nothing else (a chapter folder with other files in it is kept). Their releases become
*Folder deleted*, exactly
like chapters moved out by hand, so they are never downloaded again and a later revision
follows the usual rules (``revision_after_move_days``). When the same chapter is in the
library from two groups, one copy goes into the volume and the other stays.

Runs under the exclusive per-manga folder lock and never while a chapter of the manga is
downloading.
"""

from __future__ import annotations

import hashlib
import re
import uuid
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ACTIVE_JOB_STATUSES, JobStatus, LocalStatus
from app.library.entries import (
    comic_info_xml,
    entry_exists,
    iter_pages,
    move_entry,
    remove_entry,
    split_name,
    write_cbz,
)
from app.library.marker import list_page_files
from app.library.naming import _pad_number, chapter_sort_number, normalize_number
from app.library.storage import ensure_work_dir, require_library, resolve_rel, safe_join, staging_root
from app.logging_setup import get_logger
from app.models import ChapterRelease, DownloadJob, Manga, VolumeArchive

log = get_logger(__name__)

AUTO_PENDING_KEY = "mdm:volumes_auto"
BUILD_PENDING_KEY = "mdm:volume_build"
AUTO_SWEEP_SECONDS = 3600
_PRESENT = (LocalStatus.DOWNLOADED, LocalStatus.IMPORTED, LocalStatus.MODIFIED)
_SUPERSEDED = (LocalStatus.REPLACED, LocalStatus.IN_REPLACED)
_NUMERIC = re.compile(r"^\d+(?:\.\d+)?$")
_IGNORED_FILES = frozenset({"comicinfo.xml", "thumbs.db", "desktop.ini", ".ds_store"})


class VolumeError(Exception):
    """The volume cannot be built (not complete, destination taken, …)."""


class VolumeBusy(VolumeError):
    """A chapter of this manga is downloading right now."""


def _now() -> datetime:
    return datetime.now(UTC)


def _vol_key(volume: str | None) -> str | None:
    if volume is None or not str(volume).strip():
        return None
    v = normalize_number(str(volume).strip())
    return v if v and _NUMERIC.match(v) else None


def _vol_float(v: str) -> float:
    return float(v)


@dataclass
class VolumePlan:
    language: str
    volume: str
    status: str = "incomplete"  # built | ready | ready_with_warnings | open | incomplete
    chapters: list[ChapterRelease] = field(default_factory=list)  # one copy per chapter, in reading order
    problems: list[str] = field(default_factory=list)  # block any build
    warnings: list[str] = field(default_factory=list)  # block the automatic build
    leftovers: list[ChapterRelease] = field(default_factory=list)  # duplicate copies that stay
    closed: bool = False
    dest_rel: str | None = None
    archive: VolumeArchive | None = None

    @property
    def buildable(self) -> bool:
        return not self.problems and self.status != "built" and bool(self.chapters)

    def to_dict(self) -> dict[str, Any]:
        a = self.archive
        return {
            "language": self.language, "volume": self.volume, "status": self.status,
            "closed": self.closed, "buildable": self.buildable,
            "problems": self.problems, "warnings": self.warnings,
            "chapters": [{"release_id": str(r.id), "chapter": r.chapter, "title": r.title,
                          "groups": r.group_names or [], "folder": r.folder_rel_path,
                          "edited": r.local_status == LocalStatus.MODIFIED} for r in self.chapters],
            "kept_duplicates": [r.folder_rel_path for r in self.leftovers],
            "destination": self.dest_rel,
            "archive": None if a is None else {
                "path": a.rel_path, "chapters": a.chapters, "pages": a.pages, "size_bytes": a.size_bytes,
                "automatic": a.automatic, "created_at": a.created_at.isoformat() if a.created_at else None,
            },
        }


def volume_file_name(manga: Manga, volume: str, padding: int) -> str:
    """``Volume 03.cbz`` (padded like the chapter folders, Settings → Volume digits)."""
    return f"Volume {_pad_number(volume, padding)}.cbz"


def _volume_base(session: Session, manga: Manga, language: str) -> list[str]:
    from app.services.layout import Layout, language_folders

    layout = Layout.from_json(manga.folder_layout)
    parts = [manga.folder_name]
    if layout.language:
        parts.append(language_folders(session).get(language, language.upper()))
    return parts


def _int_numbers(releases: list[ChapterRelease], per_volume: bool = False) -> list[int]:
    out = set()
    for r in releases:
        n = chapter_sort_number(r.chapter)
        if n is not None and n == int(n):
            out.add(int(n))
    return sorted(out)


def _missing_between(nums: list[int], start: int | None) -> list[int]:
    if not nums:
        return []
    lo = nums[0] if start is None else min(start, nums[0])
    have = set(nums)
    return [n for n in range(lo, nums[-1] + 1) if n not in have]


def _fmt_numbers(nums: list[int], limit: int = 12) -> str:
    shown = ", ".join(str(n) for n in nums[:limit])
    return shown + (f" and {len(nums) - limit} more" if len(nums) > limit else "")


def plan_volumes(session: Session, root: Path, manga: Manga, language: str | None = None) -> list[VolumePlan]:
    """What the app knows about every numbered volume of ``manga`` (per language)."""
    from app.services.catalog import excluded_group_ids
    from app.settings_service import load_settings

    settings = load_settings(session)
    excluded = excluded_group_ids(session)
    stmt = select(ChapterRelease).where(ChapterRelease.manga_id == manga.id)
    if language:
        stmt = stmt.where(ChapterRelease.language_code == language)
    releases = [r for r in session.scalars(stmt)
                if not r.is_external and r.local_status not in _SUPERSEDED
                and not (r.group_ids and set(r.group_ids) <= set(excluded) and r.local_status not in _PRESENT)]
    open_jobs = {j for j in session.scalars(
        select(DownloadJob.release_id).join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == manga.id, DownloadJob.status.in_([s.value for s in ACTIVE_JOB_STATUSES])))}
    archives = {(a.language_code, a.volume): a for a in session.scalars(
        select(VolumeArchive).where(VolumeArchive.manga_id == manga.id))}
    per_volume = bool(manga.chapters_per_volume)

    from app.services.catalog import manga_language_codes

    shown = set(manga_language_codes(session, manga.id)) | {lang for lang, _ in archives}
    by_lang: dict[str, dict[str | None, list[ChapterRelease]]] = defaultdict(lambda: defaultdict(list))
    for r in releases:
        if r.language_code not in shown:
            continue
        by_lang[r.language_code][_vol_key(r.volume)].append(r)

    plans: list[VolumePlan] = []
    for lang, vols in sorted(by_lang.items()):
        numbered = sorted((v for v in vols if v is not None), key=_vol_float)
        last_md = _vol_key(manga.last_volume)
        completed = (manga.status or "").lower() == "completed"
        no_volume = vols.get(None, [])
        for idx, v in enumerate(numbered):
            plan = VolumePlan(language=lang, volume=v)
            plan.dest_rel = "/".join([*_volume_base(session, manga, lang),
                                      volume_file_name(manga, v, settings.volume_padding)])
            later = any(_vol_float(x) > _vol_float(v) for x in numbered)
            plan.closed = later or (completed and last_md is not None and _vol_float(last_md) == _vol_float(v))
            rs = vols[v]
            by_chapter: dict[int, list[ChapterRelease]] = defaultdict(list)
            for r in rs:
                by_chapter[r.logical_chapter_id].append(r)
            chosen: list[list[ChapterRelease]] = []
            missing: dict[str, list[str]] = defaultdict(list)
            for copies in by_chapter.values():
                present = [r for r in copies if r.local_status in _PRESENT and r.folder_rel_path
                           and entry_exists(resolve_rel(root, r.folder_rel_path))]
                if present:
                    chosen.append(present)
                    continue
                label = copies[0].chapter or "(no number)"
                if any(r.id in open_jobs for r in copies):
                    missing["still downloading or queued"].append(label)
                elif any(r.local_status == LocalStatus.MISSING_ON_DISK for r in copies):
                    missing["moved out of the library"].append(label)
                elif any(r.local_status == LocalStatus.EXTERNAL for r in copies):
                    missing["owned outside the library"].append(label)
                elif any(r.local_status == LocalStatus.SKIPPED for r in copies):
                    missing["skipped"].append(label)
                else:
                    missing["not downloaded"].append(label)
            # One copy per chapter: the groups that did most of the volume win, then the oldest download.
            votes = Counter(tuple(sorted(r.group_ids or [])) for copies in chosen for r in copies)
            picked: list[ChapterRelease] = []
            for copies in chosen:
                copies = sorted(copies, key=lambda r: (-votes[tuple(sorted(r.group_ids or []))],
                                                       r.downloaded_at or _now(), str(r.id)))
                picked.append(copies[0])
                plan.leftovers.extend(copies[1:])
            picked.sort(key=lambda r: (chapter_sort_number(r.chapter) is None,
                                       chapter_sort_number(r.chapter) or 0, r.publish_at or _now()))
            plan.chapters = picked
            for why, labels in missing.items():
                plan.problems.append(f"Chapter {', '.join(sorted(labels, key=lambda x: chapter_sort_number(x) or 0))}: {why}")

            nums = _int_numbers(rs, per_volume)
            if per_volume:
                start = 1
            else:
                prev = numbered[idx - 1] if idx > 0 else None
                prev_nums = _int_numbers(vols[prev], per_volume) if prev else []
                start = prev_nums[-1] + 1 if prev_nums else None
            gaps = _missing_between(nums, start)
            if gaps:
                plan.warnings.append(f"MangaDex lists no chapter {_fmt_numbers(gaps)} in this volume "
                                     "(not tagged with the volume, or never uploaded)")
            if not per_volume and nums:
                untagged = sorted({int(n) for r in no_volume
                                   if (n := chapter_sort_number(r.chapter)) is not None and nums[0] <= n <= nums[-1]})
                if untagged:
                    plan.warnings.append(f"Chapter {_fmt_numbers(untagged)} has no volume on MangaDex but is "
                                         "numbered inside this volume")
            if plan.leftovers:
                plan.warnings.append(f"{len(plan.leftovers)} chapter(s) are in the library from two groups: "
                                     "one copy goes into the volume, the other stays in the library")

            arch = archives.get((lang, v))
            if arch is not None and entry_exists(resolve_rel(root, arch.rel_path)):
                plan.archive = arch
                plan.status = "built"
            elif plan.problems or not plan.chapters:
                plan.status = "incomplete"
            elif not plan.closed:
                plan.status = "open"
            else:
                plan.status = "ready_with_warnings" if plan.warnings else "ready"
            plans.append(plan)
    return plans


# --------------------------------------------------------------------- building


def _lock(session: Session, manga_id: uuid.UUID) -> None:
    from app.services.rename import _lock_exclusive

    _lock_exclusive(session, manga_id)


def _snapshot(src: Path) -> dict[str, tuple[int, str]] | None:
    """Pages of a chapter entry -> (size, SHA-256). ``None`` when the entry holds anything the
    volume would not keep (another file, a sub-folder), so deleting it could lose user data."""
    from app.library.marker import RELEASE_MARKER

    if src.is_dir():
        pages = set(list_page_files(src))
        for e in src.iterdir():
            if e.name in pages or e.name == RELEASE_MARKER or e.name.lower() in _IGNORED_FILES:
                continue
            return None
    else:
        pages = set(list_page_files(src))
        with zipfile.ZipFile(src) as zf:
            for i in zf.infolist():
                if i.filename in pages or i.filename == RELEASE_MARKER or i.filename.lower() in _IGNORED_FILES:
                    continue
                return None
    return {name: (len(data), hashlib.sha256(data).hexdigest()) for name, data in iter_pages(src, pages)}


def verify_volume_cbz(path: Path, expected: dict[str, tuple[int, str]]) -> None:
    """Raise ``VolumeError`` unless ``path`` holds exactly ``expected`` (+ ComicInfo.xml), byte for byte.

    Checks the ZIP structure and every CRC, the member list, the size and SHA-256 of every
    page re-read from disk, and that every page decodes completely as an image.
    """
    import io
    import warnings

    from PIL import Image

    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise VolumeError(f"Volume CBZ check failed: CRC error at {bad}")
            infos = {i.filename: i for i in zf.infolist() if not i.is_dir()}
            if set(infos) - {"ComicInfo.xml"} != set(expected) or "ComicInfo.xml" not in infos:
                raise VolumeError("Volume CBZ check failed: the list of pages differs")
            for name, (size, digest) in expected.items():
                data = zf.read(name)
                if len(data) != size or infos[name].file_size != size or hashlib.sha256(data).hexdigest() != digest:
                    raise VolumeError(f"Volume CBZ check failed: {name} differs from the chapter page")
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", Image.DecompressionBombWarning)
                        with Image.open(io.BytesIO(data)) as img:
                            img.load()
                except Exception as exc:  # noqa: BLE001
                    raise VolumeError(f"Volume CBZ check failed: {name} does not decode ({exc})") from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise VolumeError(f"Volume CBZ check failed: {exc}") from exc


def build_volume(session: Session, manga_id: uuid.UUID, language: str, volume: str, *,
                 allow_warnings: bool = False, allow_open: bool = False, automatic: bool = False,
                 root: Path | None = None) -> dict[str, Any]:
    """Build the CBZ of one volume, then delete the packed chapters. Commits.

    The originals are deleted only when the archive has been checked twice — in the staging
    folder and again at its final place in the library (``verify_volume_cbz``) — and each
    chapter is, just before it is deleted, still exactly what was packed.
    """
    root = require_library(root)
    _lock(session, manga_id)
    manga = session.get(Manga, manga_id, with_for_update=True)
    if manga is None:
        raise VolumeError("Manga not found")
    vkey = _vol_key(volume)
    plan = next((p for p in plan_volumes(session, root, manga, language) if p.volume == vkey), None)
    if plan is None:
        raise VolumeError(f"No numbered volume {volume} in {language} for this title")
    if plan.status == "built":
        raise VolumeError(f"Volume {vkey} is already built: {plan.archive.rel_path}")
    if plan.problems or not plan.chapters:
        raise VolumeError("Volume not complete: " + "; ".join(plan.problems or ["no chapters in the library"]))
    if plan.status == "open" and not allow_open:
        raise VolumeError("MangaDex has no later volume yet: this volume may still grow")
    if plan.warnings and not allow_warnings:
        raise VolumeError("Volume has warnings: " + "; ".join(plan.warnings))
    busy = session.scalar(
        select(func.count()).select_from(DownloadJob)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == manga_id, DownloadJob.status == JobStatus.DOWNLOADING)) or 0
    if busy:
        raise VolumeBusy("A chapter of this title is downloading; try again when it has finished")

    dest = resolve_rel(root, plan.dest_rel)
    if dest.exists():
        raise VolumeError(f"“{plan.dest_rel}” already exists in the library; move or rename it first")
    staging = ensure_work_dir(staging_root(root))
    packed = safe_join(staging, f".mdm-volume-{uuid.uuid4().hex[:12]}.cbz")

    # What goes in: every page of every chapter, read once, with its size and hash.
    sources: list[tuple[ChapterRelease, Path, str, dict[str, tuple[int, str]] | None]] = []
    for r in plan.chapters:
        src = resolve_rel(root, r.folder_rel_path)
        if not list_page_files(src):
            raise VolumeError(f"“{r.folder_rel_path}” has no pages")
        sources.append((r, src, split_name(src.name)[0], _snapshot(src)))

    expected: dict[str, tuple[int, str]] = {}
    pages_total = 0

    def members():
        nonlocal pages_total
        for _r, src, folder, _snap in sources:
            for name, data in iter_pages(src, list_page_files(src)):
                arc = f"{folder}/{name}"
                expected[arc] = (len(data), hashlib.sha256(data).hexdigest())
                pages_total += 1
                yield arc, data
        yield "ComicInfo.xml", _volume_comic_info(manga, plan, pages_total)

    moved = False
    try:
        write_cbz(packed, members())
        verify_volume_cbz(packed, expected)  # 1st check, in the staging folder
        dest.parent.mkdir(parents=True, exist_ok=True)
        move_entry(packed, dest)
        moved = True
        verify_volume_cbz(dest, expected)  # 2nd check, re-read from its final place
    except BaseException:
        packed.unlink(missing_ok=True)
        if moved:
            dest.unlink(missing_ok=True)  # our own file, never verified: nothing else is touched
        session.rollback()
        raise
    size = dest.stat().st_size

    from app.services.control import record_event

    arch = session.scalar(select(VolumeArchive).where(
        VolumeArchive.manga_id == manga_id, VolumeArchive.language_code == language, VolumeArchive.volume == vkey))
    if arch is None:
        arch = VolumeArchive(manga_id=manga_id, language_code=language, volume=vkey)
        session.add(arch)
    arch.rel_path = plan.dest_rel
    arch.release_ids = [str(r.id) for r in plan.chapters]
    arch.chapters = len(plan.chapters)
    arch.pages = pages_total
    arch.size_bytes = size
    arch.automatic = automatic
    arch.created_at = _now()
    session.commit()  # the verified volume is recorded before anything is deleted

    # Delete the originals, each only if it is still exactly what went into the volume.
    _lock(session, manga_id)
    deleted: list[ChapterRelease] = []
    kept: list[dict[str, str]] = []
    for r, src, folder, snap in sources:
        packed_pages = {n.split("/", 1)[1]: v for n, v in expected.items() if n.split("/", 1)[0] == folder}
        try:
            if snap is None:
                kept.append({"folder": r.folder_rel_path, "reason": "holds other files besides the pages"})
                continue
            now = _snapshot(src) if entry_exists(src) else None
            if now != snap or snap != packed_pages:
                kept.append({"folder": r.folder_rel_path, "reason": "changed while the volume was being built"})
                continue
            remove_entry(src)
            deleted.append(r)
        except OSError as exc:
            kept.append({"folder": r.folder_rel_path, "reason": f"could not be deleted: {exc}"})
            log.warning("volume_chapter_delete_failed", path=r.folder_rel_path, error=str(exc))
    for r in deleted:
        row = session.get(ChapterRelease, r.id)
        row.local_status = LocalStatus.MISSING_ON_DISK  # same as a chapter moved out by hand
        row.local_checked_at = _now()
    note = f"; {len(kept)} chapter folder(s) kept: " + ", ".join(k["folder"] for k in kept) if kept else ""
    record_event(session, "volume_cbz",
                 f"Volume {vkey} ({language}) packed{' automatically' if automatic else ''} and checked: "
                 f"{len(plan.chapters)} chapter(s), {pages_total} pages → {plan.dest_rel}; "
                 f"{len(deleted)} chapter folder(s) deleted{note}", manga_id=manga_id)
    session.commit()
    log.info("volume_cbz_built", manga_id=str(manga_id), language=language, volume=vkey, path=plan.dest_rel,
             chapters=len(plan.chapters), pages=pages_total, size=size, automatic=automatic,
             deleted=len(deleted), kept=len(kept))
    return {"path": plan.dest_rel, "chapters": len(plan.chapters), "pages": pages_total, "size_bytes": size,
            "deleted": len(deleted), "not_deleted": kept,
            "kept_duplicates": [r.folder_rel_path for r in plan.leftovers]}


def _volume_comic_info(manga: Manga, plan: VolumePlan, pages: int) -> bytes:
    groups = []
    for r in plan.chapters:
        for g in r.group_names or []:
            if g and g not in groups:
                groups.append(g)
    numbers = [r.chapter for r in plan.chapters if r.chapter]
    return comic_info_xml({
        "Title": f"Volume {plan.volume}",
        "Series": manga.display_title,
        "Volume": plan.volume,
        "Summary": f"Chapters {numbers[0]}–{numbers[-1]}" if len(numbers) > 1 else None,
        "Web": f"https://mangadex.org/title/{manga.id}",
        "ScanInformation": " & ".join(groups) or None,
        "LanguageISO": plan.language,
        "PageCount": pages,
        "Manga": "Yes",
        "Notes": "Built by MangaDex WebDL from the chapters: "
                 + ", ".join(split_name((r.folder_rel_path or "").rsplit("/", 1)[-1])[0] for r in plan.chapters),
    })


# --------------------------------------------------------------------- queueing


def enqueue_build(manga_id: uuid.UUID, language: str, volume: str, allow_warnings: bool, allow_open: bool) -> bool:
    from app.redis_client import get_redis
    from app.services.jobs import QUEUE_DEFAULT, get_queue

    key = f"{BUILD_PENDING_KEY}:{manga_id}:{language}:{volume}"
    r = get_redis()
    if not r.set(key, "1", nx=True, ex=3600):
        return False
    try:
        get_queue(QUEUE_DEFAULT).enqueue("app.tasks.task_build_volume", str(manga_id), language, volume,
                                         allow_warnings, allow_open, job_timeout=3600, result_ttl=3600,
                                         failure_ttl=7 * 86400)
    except Exception:
        r.delete(key)
        raise
    return True


def enqueue_auto_volumes(manga_id: uuid.UUID | None) -> bool:
    """Queue the automatic volume check (only when the setting is on). ``None`` = every title, hourly."""
    from app.db import session_scope
    from app.redis_client import get_redis
    from app.services.jobs import QUEUE_DEFAULT, get_queue
    from app.settings_service import load_settings

    with session_scope() as s:
        if not load_settings(s).volume_cbz_auto:
            return False
    key = f"{AUTO_PENDING_KEY}:{manga_id or 'all'}"
    r = get_redis()
    if not r.set(key, "1", nx=True, ex=AUTO_SWEEP_SECONDS if manga_id is None else 600):
        return False
    try:
        get_queue(QUEUE_DEFAULT).enqueue("app.tasks.task_auto_volumes", str(manga_id) if manga_id else None,
                                         job_timeout=4 * 3600, result_ttl=3600, failure_ttl=7 * 86400)
    except Exception:
        r.delete(key)
        raise
    return True


def run_auto_volumes(manga_id: uuid.UUID | None) -> dict[str, Any]:
    """Build every volume that is ready (closed, complete, no warnings). Never raises per volume."""
    from app.db import session_scope
    from app.settings_service import load_settings

    root = require_library()
    built, skipped = [], []
    with session_scope() as s:
        if not load_settings(s).volume_cbz_auto:
            return {"built": [], "skipped": "setting off"}
        ids = [manga_id] if manga_id else list(s.scalars(select(Manga.id).where(Manga.pending_approval.is_(False))))
    for mid in ids:
        with session_scope() as s:
            manga = s.get(Manga, mid)
            if manga is None:
                continue
            ready = [(p.language, p.volume) for p in plan_volumes(s, root, manga) if p.status == "ready"]
        for lang, vol in ready:
            try:
                with session_scope() as s:
                    built.append({"manga_id": str(mid), "language": lang, "volume": vol,
                                  **build_volume(s, mid, lang, vol, automatic=True, root=root)})
            except VolumeError as exc:
                skipped.append({"manga_id": str(mid), "language": lang, "volume": vol, "reason": str(exc)})
            except Exception as exc:  # noqa: BLE001
                log.warning("auto_volume_failed", manga_id=str(mid), volume=vol, exc_info=True)
                skipped.append({"manga_id": str(mid), "language": lang, "volume": vol, "reason": str(exc)[:300]})
    return {"built": built, "skipped": skipped}


def volume_release_map(session: Session, manga_id: uuid.UUID) -> dict[str, str]:
    """release id -> volume CBZ path, for the chapter list."""
    out: dict[str, str] = {}
    for a in session.scalars(select(VolumeArchive).where(VolumeArchive.manga_id == manga_id)):
        for rid in a.release_ids or []:
            out[str(rid)] = a.rel_path
    return out
