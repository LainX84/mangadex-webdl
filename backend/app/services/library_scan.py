"""Library import and reconciliation between the database and the filesystem.

Neither side is assumed to be correct:

* folders with a ``.mdm-release.json`` marker are matched by chapter UUID;
* folders without a marker are matched by language folder + group folder +
  chapter/volume number (whichever of those levels the layout has), and only
  when exactly one release fits – anything
  ambiguous becomes a Review item instead of a guess;
* releases whose folders vanished are flagged ``missing_on_disk`` (never
  re-downloaded automatically); folders whose pages differ from the original
  manifest are flagged ``modified`` and left untouched.

Nothing in this module deletes or rewrites user files. The only write is adding
a hidden marker file to folders that are positively matched on import.

Chapters stored as ``{Chapter}.cbz`` (``app.library.entries``) are recognised only by the
release marker inside the archive; other ``.cbz`` files (volumes built by the app, your
own archives) are never linked to a release and never sent to Review.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import JobStatus, LocalStatus, ReviewKind, ReviewStatus, ScanStatus
from app.library.entries import entry_exists, is_cbz, page_sizes, split_name
from app.library.marker import (
    PARTIAL_PREFIX,
    list_page_files,
    read_manga_marker,
    read_release_marker,
    write_release_marker,
)
from app.library.naming import group_folder_name, name_ends_with_group, normalize_number, parse_chapter_folder
from app.library.storage import require_library, resolve_rel, safe_join
from app.logging_setup import get_logger
from app.mangadex.client import MangaDexClient
from app.mangadex.errors import NotFoundError
from app.mangadex.parse import parse_chapter
from app.models import ChapterRelease, DownloadJob, Language, LibraryScanRun, Manga, ReviewItem
from app.services.catalog import manga_language_codes, upsert_release
from app.services.control import record_event

log = get_logger(__name__)

_LINKED = {LocalStatus.DOWNLOADED, LocalStatus.IMPORTED, LocalStatus.MODIFIED, LocalStatus.MISSING_ON_DISK}


def _now() -> datetime:
    return datetime.now(UTC)


def _subdirs(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))
    except FileNotFoundError:
        return []


def _open_review(session: Session, kind: ReviewKind, path: str, manga_id, details: dict, candidates: list) -> bool:
    existing = session.scalar(select(ReviewItem).where(ReviewItem.path == path, ReviewItem.status == ReviewStatus.OPEN))
    if existing is not None:
        existing.details = details
        existing.candidates = candidates
        return False
    session.add(ReviewItem(kind=kind, manga_id=manga_id, path=path, details=details, candidates=candidates))
    session.flush()
    return True


def _candidate_dict(r: ChapterRelease) -> dict:
    return {
        "release_id": str(r.id),
        "language": r.language_code,
        "volume": r.volume,
        "chapter": r.chapter,
        "title": r.title,
        "groups": r.group_names,
        "pages": r.pages,
        "local_status": r.local_status,
    }


def link_folder(session: Session, root: Path, release: ChapterRelease, rel_path: str, imported: bool = True) -> None:
    """Associate an existing folder with a release (no file is changed except the marker)."""
    folder = resolve_rel(root, rel_path)
    files = list_page_files(folder)
    marker = read_release_marker(folder)
    if marker and marker.get("chapter_id") == str(release.id) and marker.get("pages"):
        manifest = marker["pages"]
        modified = sorted(m.get("file") for m in manifest) != files
        status = LocalStatus.MODIFIED if modified else (LocalStatus.IMPORTED if imported and marker.get("imported") else LocalStatus.DOWNLOADED)
    else:
        sizes = page_sizes(folder)
        manifest = [{"index": i, "file": f, "size": sizes.get(f)} for i, f in enumerate(files, 1)]
        write_release_marker(folder, {
            "chapter_id": str(release.id),
            "manga_id": str(release.manga_id),
            "language": release.language_code,
            "volume": release.volume,
            "chapter": release.chapter,
            "title": release.title,
            "groups": [{"id": g, "name": n} for g, n in zip(release.group_ids, release.group_names, strict=False)],
            "imported": True,
            "pages": manifest,
        })
        status = LocalStatus.IMPORTED
    release.local_status = status
    release.folder_rel_path = rel_path
    release.page_manifest = manifest
    release.local_page_count = len(files)
    release.local_checked_at = _now()
    # A job that was waiting on this folder is now satisfied.
    for job in session.scalars(select(DownloadJob).where(
        DownloadJob.release_id == release.id,
        DownloadJob.status.in_([JobStatus.PENDING, JobStatus.RETRY_SCHEDULED, JobStatus.NEEDS_ATTENTION,
                                JobStatus.FAILED, JobStatus.PAUSED]),
    )):
        job.status = JobStatus.COMPLETED
        job.finished_at = _now()
        job.last_error = None
    record_event(session, "linked", f"Existing folder linked: {rel_path}", release_id=release.id, manga_id=release.manga_id)


def verify_linked_releases(session: Session, root: Path, manga_id: uuid.UUID) -> dict:
    """Re-check every release that has a folder. Detects deletions and manual edits."""
    stats = {"verified": 0, "modified": 0, "missing_on_disk": 0, "restored": 0}
    for r in session.scalars(select(ChapterRelease).where(
        ChapterRelease.manga_id == manga_id, ChapterRelease.folder_rel_path.is_not(None)
    )):
        folder = resolve_rel(root, r.folder_rel_path)
        if not entry_exists(folder):
            if r.local_status != LocalStatus.MISSING_ON_DISK:
                r.local_status = LocalStatus.MISSING_ON_DISK
                stats["missing_on_disk"] += 1
                record_event(session, "missing_on_disk", r.folder_rel_path, release_id=r.id, manga_id=manga_id)
            continue
        files = list_page_files(folder)
        expected = sorted(m.get("file") for m in (r.page_manifest or []))
        was_missing = r.local_status == LocalStatus.MISSING_ON_DISK
        if expected and files != expected:
            r.local_status = LocalStatus.MODIFIED
            stats["modified"] += 1
        elif was_missing or r.local_status == LocalStatus.MODIFIED:
            marker = read_release_marker(folder) or {}
            r.local_status = LocalStatus.IMPORTED if marker.get("imported") else LocalStatus.DOWNLOADED
        if was_missing:
            stats["restored"] += 1
        r.local_page_count = len(files)
        r.local_checked_at = _now()
        stats["verified"] += 1
    return stats


def _chapter_dirs(manga_dir: Path) -> list[tuple[list[str], Path]]:
    """Chapter folders below a manga folder, whatever its layout.

    Returns ``(context, folder)`` pairs where ``context`` holds the 0–2 folder names
    between the manga folder and the chapter folder ({Language} and/or {Group}).
    A folder is a chapter folder when it contains page images or a release marker;
    otherwise it is descended into (at most two levels).
    """
    out: list[tuple[list[str], Path]] = []

    def walk(d: Path, ctx: list[str]) -> None:
        for cbz in _chapter_cbz_files(d):
            out.append((ctx, cbz))
        for sub in _subdirs(d):
            if sub.name.startswith(PARTIAL_PREFIX):
                continue
            if list_page_files(sub) or read_release_marker(sub) is not None:
                out.append((ctx, sub))
            elif len(ctx) < 2:
                walk(sub, [*ctx, sub.name])

    walk(manga_dir, [])
    return out


def _chapter_cbz_files(path: Path) -> list[Path]:
    """Chapter CBZ files in ``path``: only those carrying a release marker."""
    try:
        files = sorted(p for p in path.iterdir()
                       if is_cbz(p) and p.is_file() and not p.is_symlink() and not p.name.startswith("."))
    except FileNotFoundError:
        return []
    return [p for p in files if read_release_marker(p) is not None]


def import_manga_folder(session: Session, root: Path, manga: Manga, client: MangaDexClient | None = None) -> dict:
    """Recognise chapter folders under ``{root}/{manga folder}`` and link them to releases.

    Works with every layout: ``{Language}/{Group}/{Chapter}``, ``{Language}/{Chapter}``,
    ``{Group}/{Chapter}`` and ``{Chapter}`` directly in the manga folder.
    """
    stats = {"matched_marker": 0, "matched_name": 0, "review": 0, "skipped_linked": 0}
    manga_dir = safe_join(root, manga.folder_name)
    if not manga_dir.is_dir():
        return stats
    languages = {lang.folder_code.lower(): lang.code for lang in session.scalars(select(Language))}
    languages.update({lang.code.lower(): lang.code for lang in session.scalars(select(Language))})
    selected = manga_language_codes(session, manga.id)
    linked_paths = set(session.scalars(select(ChapterRelease.folder_rel_path).where(
        ChapterRelease.manga_id == manga.id, ChapterRelease.folder_rel_path.is_not(None))))
    releases = list(session.scalars(select(ChapterRelease).where(ChapterRelease.manga_id == manga.id)))
    by_id = {r.id: r for r in releases}
    from app.settings_service import load_settings

    placeholder = load_settings(session).volume_placeholder

    for ctx, ch_dir in _chapter_dirs(manga_dir):
        rel = "/".join([manga.folder_name, *ctx, ch_dir.name])
        if rel in linked_paths:
            stats["skipped_linked"] += 1
            continue
        if not list_page_files(ch_dir):
            continue
        marker = read_release_marker(ch_dir)
        if marker:
            try:
                rid = uuid.UUID(str(marker["chapter_id"]))
            except ValueError:
                rid = None
            release = by_id.get(rid) if rid else None
            if release is None and rid and client is not None:
                try:
                    release, _ = upsert_release(session, manga.id, parse_chapter(client.get_chapter(str(rid))))
                    by_id[rid] = release
                except NotFoundError:
                    release = None
            if release is not None and release.folder_rel_path in (None, rel) and release.manga_id == manga.id:
                link_folder(session, root, release, rel, imported=bool(marker.get("imported")))
                linked_paths.add(rel)
                stats["matched_marker"] += 1
                continue
            reason = ("Marker references a chapter that is already linked to another folder (duplicate copy)"
                      if release is not None else "Marker references an unknown chapter")
            if _open_review(session, ReviewKind.AMBIGUOUS_MATCH, rel, manga.id,
                            {"reason": reason, "marker_chapter_id": str(marker.get("chapter_id"))},
                            [_candidate_dict(release)] if release else []):
                stats["review"] += 1
            continue

        # Context folders: {Language}/{Group}, {Language}, {Group} or nothing.
        lang_folder = group_folder = None
        if len(ctx) == 2:
            lang_folder, group_folder = ctx
        elif len(ctx) == 1:
            if ctx[0].lower() in languages:
                lang_folder = ctx[0]
            else:
                group_folder = ctx[0]
        if lang_folder is not None:
            lang_code = languages.get(lang_folder.lower())
            allowed_langs = {lang_code} if lang_code else set()
        else:
            lang_code = selected[0] if len(selected) == 1 else None
            allowed_langs = {lang_code} if lang_code else set(selected) or {r.language_code for r in releases}

        parsed = parse_chapter_folder(split_name(ch_dir.name)[0], placeholder)
        candidates: list[ChapterRelease] = []
        if allowed_langs and parsed:
            for r in releases:
                if (r.language_code not in allowed_langs or r.local_status in _LINKED or r.folder_rel_path
                        or r.local_status == LocalStatus.REPLACED):
                    continue
                if group_folder is not None and group_folder_name(r.group_names or []).lower() != group_folder.lower():
                    continue
                if parsed.oneshot:
                    if r.chapter is not None:
                        continue
                elif normalize_number(r.chapter) != normalize_number(parsed.chapter):
                    continue
                if parsed.volume and r.volume and normalize_number(r.volume) != normalize_number(parsed.volume):
                    continue
                if parsed.uuid_prefix and not str(r.id).replace("-", "").startswith(parsed.uuid_prefix):
                    continue
                candidates.append(r)
        if len(candidates) > 1:
            # "… - Team X": the group in the chapter folder name tells same-number releases apart.
            by_suffix = [c for c in candidates if name_ends_with_group(split_name(ch_dir.name)[0], c.group_names)]
            if by_suffix:
                candidates = by_suffix
        if len(candidates) == 1:
            link_folder(session, root, candidates[0], rel)
            linked_paths.add(rel)
            stats["matched_name"] += 1
            continue
        details = {
            "reason": ("No release matches this folder" if not candidates
                       else f"{len(candidates)} releases match this folder"),
            "language_folder": lang_folder,
            "language": lang_code,
            "group_folder": group_folder,
            "parsed": parsed.__dict__ if parsed else None,
            "page_files": len(list_page_files(ch_dir)),
        }
        kind = ReviewKind.AMBIGUOUS_MATCH if candidates else ReviewKind.UNMATCHED_FOLDER
        if _open_review(session, kind, rel, manga.id, details, [_candidate_dict(c) for c in candidates]):
            stats["review"] += 1
    return stats


def detect_languages_on_disk(session: Session, root: Path, manga: Manga) -> set[str]:
    manga_dir = safe_join(root, manga.folder_name)
    mapping = {lang.folder_code.lower(): lang.code for lang in session.scalars(select(Language))}
    mapping.update({lang.code.lower(): lang.code for lang in session.scalars(select(Language))})
    return {mapping[d.name.lower()] for d in _subdirs(manga_dir) if d.name.lower() in mapping}


def scan_unknown_manga_folders(session: Session, root: Path) -> int:
    """Top-level folders that do not belong to any followed manga → review items."""
    # Titles waiting for approval own no folder yet: a matching folder stays in Review,
    # from where it can be imported while approving the title.
    known = set(session.scalars(select(Manga.folder_name).where(Manga.pending_approval.is_(False))))
    known_ids = {str(m) for m in session.scalars(select(Manga.id).where(Manga.pending_approval.is_(False)))}
    added = 0
    for d in _subdirs(root):
        if d.name in known:
            continue
        marker = read_manga_marker(d)
        if marker and marker.get("manga_id") in known_ids:
            continue
        if _open_review(session, ReviewKind.UNKNOWN_MANGA_FOLDER, d.name, None,
                        {"folder": d.name, "manga_id": (marker or {}).get("manga_id"),
                         "reason": "Folder is not linked to a followed manga"}, []):
            added += 1
    return added


def run_library_scan(manga_id: uuid.UUID | None, client: MangaDexClient | None = None, sync_feed=None,
                     root: Path | None = None) -> dict:
    """Full reconciliation for one manga (or every manga + unknown folders)."""
    from app.db import session_scope

    root = require_library(root)
    with session_scope() as s:
        run = LibraryScanRun(manga_id=manga_id, status=ScanStatus.RUNNING)
        s.add(run)
        s.flush()
        run_id = run.id
        ids = [manga_id] if manga_id else list(s.scalars(select(Manga.id).where(Manga.pending_approval.is_(False))))
    summary: dict = {"manga": {}}
    try:
        for mid in ids:
            if sync_feed is not None:
                with session_scope() as s:
                    manga = s.get(Manga, mid)
                    langs = set(manga_language_codes(s, mid)) | detect_languages_on_disk(s, root, manga)
                sync_feed(mid, sorted(langs))
            with session_scope() as s:
                from app.services.rename import lock_manga_folder_shared

                lock_manga_folder_shared(s, mid)  # never reconcile mid-rename
                manga = s.get(Manga, mid)
                verify = verify_linked_releases(s, root, mid)
                imp = import_manga_folder(s, root, manga, client)
                summary["manga"][str(mid)] = {**verify, **imp}
        if manga_id is None:
            with session_scope() as s:
                summary["unknown_folders"] = scan_unknown_manga_folders(s, root)
        with session_scope() as s:
            run = s.get(LibraryScanRun, run_id)
            run.status = ScanStatus.SUCCESS
            run.finished_at = _now()
            run.summary = summary
        return summary
    except Exception as exc:
        with session_scope() as s:
            run = s.get(LibraryScanRun, run_id)
            run.status = ScanStatus.FAILED
            run.finished_at = _now()
            run.error = f"{exc.__class__.__name__}: {exc}"[:2000]
        raise
