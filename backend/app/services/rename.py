"""Change a followed manga's title and move its library folder accordingly.

The manga folder (``Manga.folder_name``) is derived from the display title when
the manga is added. Renaming moves ``<library>/<old folder>`` to
``<library>/<new folder>`` and rewrites every stored path below it (releases,
jobs, open review items) in the same transaction.

Concurrency: the rename holds an exclusive per-manga advisory lock while it
moves the folder; the download dispatcher and the library scan take the shared
form of the same lock, so no download starts and no reconciliation runs against
that manga mid-rename. A rename is refused while a chapter of the manga is
queued or downloading (those jobs already resolved their target paths).
"""

from __future__ import annotations

import os
import uuid
import zlib
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.enums import JobStatus, ReviewStatus
from app.library.marker import write_manga_marker
from app.library.storage import fsync_dir, require_library, safe_join
from app.logging_setup import get_logger
from app.models import ChapterRelease, DownloadJob, Manga, ReviewItem

log = get_logger(__name__)

FOLDER_LOCK_CLASS = 7341  # first key of the two-key advisory lock (namespace)
MAX_TITLE_LENGTH = 300


class RenameError(Exception):
    """The rename cannot be done as requested (bad title, name clash)."""


class RenameBusy(RenameError):
    """Chapters of this manga are being downloaded right now."""


def folder_lock_key(manga_id: uuid.UUID) -> int:
    v = zlib.crc32(b"folder:" + manga_id.bytes)
    return v - 2**32 if v >= 2**31 else v  # int4


def lock_manga_folder_shared(session: Session, manga_id: uuid.UUID, wait: bool = True) -> bool:
    """Shared side of the folder lock (released at the end of the transaction)."""
    fn = "pg_advisory_xact_lock_shared" if wait else "pg_try_advisory_xact_lock_shared"
    got = session.execute(text(f"SELECT {fn}(:c, :k)"),
                          {"c": FOLDER_LOCK_CLASS, "k": folder_lock_key(manga_id)}).scalar()
    return True if wait else bool(got)


def _lock_exclusive(session: Session, manga_id: uuid.UUID) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:c, :k)"),
                    {"c": FOLDER_LOCK_CLASS, "k": folder_lock_key(manga_id)})


# --------------------------------------------------------------------- titles

def title_candidates(attributes: dict[str, Any] | None, alt_titles: list[dict] | None,
                     preferred_languages: list[str]) -> list[dict[str, Any]]:
    """MangaDex titles for a manga: the main title plus every alternative title.

    Several titles can share a language. Duplicates (same text) are listed once.
    Preferred languages come first, then romanised Japanese / Japanese, then the rest.
    """
    entries: list[tuple[str, str, str]] = []
    main = (attributes or {}).get("title") or {}
    if isinstance(main, dict):
        entries += [(lang, str(t), "main") for lang, t in main.items() if t]
    for alt in alt_titles or []:
        if isinstance(alt, dict):
            entries += [(lang, str(t), "alt") for lang, t in alt.items() if t]

    pref = [lang.lower() for lang in preferred_languages]

    def rank(lang: str, source: str) -> tuple[int, float]:
        lang = lang.lower()
        base = lang.split("-")[0]
        if lang in pref:
            return (0, pref.index(lang))
        if base in pref:
            return (0, pref.index(base) + 0.5)  # e.g. "pt-br" when "pt" is followed
        if source == "main":
            return (1, 0)
        if lang in ("ja-ro", "en-ro"):
            return (2, 0)
        if lang == "ja":
            return (2, 1)
        return (3, 0)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered = sorted(enumerate(entries), key=lambda e: (*rank(e[1][0], e[1][2]), e[0]))
    for _, (lang, title, source) in ordered:
        title = " ".join(title.split())
        if not title or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        out.append({"title": title, "language": lang, "source": source,
                    "preferred": rank(lang, source)[0] == 0})
    return out


# --------------------------------------------------------------------- rename

def _replace_prefix(value: str | None, old: str, new: str) -> str | None:
    if value is None:
        return None
    if value == old:
        return new
    if value.startswith(old + "/"):
        return new + value[len(old):]
    return value


def plan_folder_name(session: Session, manga: Manga, new_override: str | None) -> str:
    from app.services.catalog import unique_manga_folder

    display = new_override or manga.title
    return unique_manga_folder(session, display, manga.id, exclude_id=manga.id)


def normalise_title(title: str | None, canonical: str) -> str | None:
    """Return the override to store (None = use the MangaDex main title)."""
    if title is None:
        return None
    title = " ".join(str(title).split())
    if len(title) > MAX_TITLE_LENGTH:
        raise RenameError(f"Title is too long (max {MAX_TITLE_LENGTH} characters)")
    if not title or title == canonical:
        return None
    return title


def rename_manga(session: Session, manga_id: uuid.UUID, title: str | None, *,
                 dry_run: bool = False, root: Path | None = None) -> dict[str, Any]:
    """Set the manga's title (``None`` = MangaDex main title) and move its folder.

    Commits on success (the folder move and the DB update succeed or fail together).
    """
    _lock_exclusive(session, manga_id)
    manga = session.get(Manga, manga_id, with_for_update=True)
    if manga is None:
        raise RenameError("Manga not found")
    override = normalise_title(title, manga.title)
    old_folder = manga.folder_name
    new_title = override or manga.title
    new_folder = old_folder if new_title == manga.display_title else plan_folder_name(session, manga, override)
    busy = session.scalar(
        select(func.count()).select_from(DownloadJob)
        .join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
        .where(ChapterRelease.manga_id == manga_id,
               DownloadJob.status.in_([JobStatus.QUEUED, JobStatus.DOWNLOADING]))
    ) or 0
    linked = session.scalar(
        select(func.count()).select_from(ChapterRelease)
        .where(ChapterRelease.manga_id == manga_id, ChapterRelease.folder_rel_path.is_not(None))
    ) or 0
    report: dict[str, Any] = {
        "title": new_title, "title_override": override,
        "folder_from": old_folder, "folder_to": new_folder,
        "folder_changes": new_folder != old_folder,
        "chapters_on_disk": linked, "busy_downloads": busy,
        "moved": False, "dry_run": dry_run,
    }
    if dry_run:
        session.rollback()  # releases the lock
        return report

    if new_folder == old_folder:
        manga.title_override = override
        session.commit()
        return report

    if busy:
        session.rollback()
        raise RenameBusy(f"{busy} chapter(s) of this manga are downloading right now. "
                         "Wait for them to finish (or pause them) and try again.")

    lib = require_library(root)
    old_dir = safe_join(lib, old_folder)
    new_dir = safe_join(lib, new_folder)
    same_dir = old_dir.exists() and new_dir.exists() and os.path.samefile(old_dir, new_dir)
    if new_dir.exists() and not same_dir:
        if new_dir.is_dir() and not any(new_dir.iterdir()):
            new_dir.rmdir()  # an empty leftover folder is not worth refusing for
        else:
            session.rollback()
            raise RenameError(f"A folder named “{new_folder}” already exists in the library. "
                              "Move or rename it first, or choose another title.")

    moved = False
    if old_dir.exists():
        os.rename(old_dir, new_dir)
        fsync_dir(lib)
        moved = True
    try:
        for r in session.scalars(select(ChapterRelease).where(ChapterRelease.manga_id == manga_id,
                                                             ChapterRelease.folder_rel_path.is_not(None))):
            r.folder_rel_path = _replace_prefix(r.folder_rel_path, old_folder, new_folder)
        for j in session.scalars(select(DownloadJob).join(ChapterRelease, ChapterRelease.id == DownloadJob.release_id)
                                 .where(ChapterRelease.manga_id == manga_id)):
            j.target_rel_path = _replace_prefix(j.target_rel_path, old_folder, new_folder)
            j.temp_dir_rel = _replace_prefix(j.temp_dir_rel, old_folder, new_folder)
        for item in session.scalars(select(ReviewItem).where(ReviewItem.manga_id == manga_id,
                                                            ReviewItem.status == ReviewStatus.OPEN)):
            item.path = _replace_prefix(item.path, old_folder, new_folder)
        manga.folder_name = new_folder
        manga.title_override = override
        from app.services.control import record_event

        record_event(session, "renamed", f"Title “{new_title}”, folder “{old_folder}” → “{new_folder}”",
                     manga_id=manga_id)
        session.commit()
    except Exception:
        session.rollback()
        if moved:
            try:
                os.rename(new_dir, old_dir)
            except OSError:
                log.exception("rename_rollback_failed", manga_id=str(manga_id))
        raise
    if new_dir.exists():
        try:
            write_manga_marker(new_dir, str(manga_id), new_title)
        except OSError:
            log.warning("manga_marker_update_failed", manga_id=str(manga_id))
    report["moved"] = moved
    log.info("manga_renamed", manga_id=str(manga_id), folder_from=old_folder, folder_to=new_folder, moved=moved)
    return report
