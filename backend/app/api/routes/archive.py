"""Read-only comics archive: index status, rescan, and "continue from your archive" suggestions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.clients import get_client
from app.api.errors import mangadex_errors
from app.db import get_db
from app.library.naming import chapter_sort_number
from app.mangadex.client import MangaDexClient
from app.mangadex.parse import parse_chapter, parse_manga
from app.models import ArchiveFolder, ChapterRelease, Manga
from app.services import archive
from app.settings_service import load_settings

router = APIRouter(prefix="/api", tags=["archive"])


@router.get("/archive")
def archive_status(db: Session = Depends(get_db)) -> dict:
    settings = load_settings(db)
    root = archive.archive_root()
    state = archive.index_state(db)
    return {
        "configured": root is not None,
        "enabled": settings.archive_enabled,
        "root": str(root) if root else None,
        "dirs": settings.archive_dirs,
        "folders": db.scalar(select(func.count()).select_from(ArchiveFolder)) or 0,
        "indexed_at": state.get("at"),
        "last_result": state or None,
        "rescan_hours": settings.archive_rescan_hours,
    }


@router.post("/archive/rescan")
def archive_rescan(db: Session = Depends(get_db)) -> dict:
    if archive.archive_root() is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The archive is not mounted (MDM_ARCHIVE_ROOT)")
    return {"queued": archive.enqueue_index(force=True)}


class FolderRescanIn(BaseModel):
    folder: str = Field(min_length=1, max_length=1000)


@router.post("/archive/folder/rescan")
def archive_folder_rescan(body: FolderRescanIn, db: Session = Depends(get_db)) -> dict:
    """Re-read one archive folder already found by the index (e.g. after adding volumes to it)."""
    if not load_settings(db).archive_enabled or archive.archive_root() is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The archive is not mounted (MDM_ARCHIVE_ROOT)")
    result = archive.refresh_folder(db, body.folder)
    if result["status"] == "unknown_folder":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This folder is not in the archive index")
    if result["status"] == "unreadable":
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"The folder can't be read ({result['error']})")
    db.commit()
    return result


def _md_chapters_db(db: Session, manga_id: uuid.UUID, languages: list[str]) -> list[tuple[float, str | None]]:
    out = []
    for chapter, volume, url, pages in db.execute(
        select(ChapterRelease.chapter, ChapterRelease.volume, ChapterRelease.external_url, ChapterRelease.pages)
        .where(ChapterRelease.manga_id == manga_id, ChapterRelease.language_code.in_(languages))
    ):
        n = chapter_sort_number(chapter)
        if n is not None and not (url and (pages or 0) <= 1):
            out.append((n, volume))
    return out


@router.get("/archive/suggest/{manga_id}")
def suggest(manga_id: uuid.UUID, languages: str = Query(..., max_length=200), db: Session = Depends(get_db),
            client: MangaDexClient = Depends(get_client)) -> dict:
    """Where your archive ends for this manga, and the MangaDex chapter to continue from.

    Works for titles in the library (also those waiting for approval) and for titles
    being added: the chapter list comes from the database when the title has been
    scanned, otherwise from the MangaDex feed already fetched by the add form (cached).
    """
    langs = sorted({x.strip() for x in languages.split(",") if x.strip()})
    if not langs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No languages given")
    settings = load_settings(db)
    if not settings.archive_enabled or archive.archive_root() is None:
        return {"configured": False, "candidates": [], "best": None}
    m = db.get(Manga, manga_id)
    with mangadex_errors():
        if m is not None:
            titles, alts = [m.title, m.title_override, m.folder_name], m.alt_titles
        else:
            p = parse_manga(client.get_manga(str(manga_id)))
            titles, alts = [p.title], p.alt_titles
        pairs = _md_chapters_db(db, manga_id, langs) if m is not None and not m.pending_approval else []
        if not pairs:
            for item in client.iter_manga_feed(str(manga_id), langs, cache_ttl=600):
                ch = parse_chapter(item)
                n = chapter_sort_number(ch.chapter)
                if n is not None and not ch.is_external:
                    pairs.append((n, ch.volume))
    result = archive.suggest(db, titles, alts, langs, archive.MangaDexChapters.build(pairs))
    db.commit()  # cached folder analyses
    return result
