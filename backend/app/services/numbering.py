"""Per-volume chapter numbering (anthologies and similar).

Most titles number chapters across the whole series, so "chapter 2" from any group
or language is the same chapter (logical key ``c:2``). Some titles restart at
chapter 1 in every volume (Vol.1 Ch.1-5, Vol.2 Ch.1-6, ...). With series keys every
"chapter 2" would collapse into one logical chapter: owning one of them (JAM import,
"New chapters only", one release per chapter) would hide all the others.

Detection: a title switches to per-volume numbering when chapter 1 appears in at
least two different volumes of the same language. It never switches back on its own
(``mdm chapter-numbering --set series`` does that).

Switching re-keys every logical chapter (``v15:c:2``), the owned-elsewhere chapters
(by the volume of their MangaDex release) and converts ``start_chapter`` into the
volume * 1000 + chapter scale used for ordering. Files and folders are never touched
(chapter folders already carry the volume in their name).
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.library.naming import (
    VOLUME_FACTOR,
    chapter_kind,
    chapter_sort_number,
    logical_chapter_key,
    logical_sort_number,
)
from app.logging_setup import get_logger
from app.models import ChapterRelease, ExternalChapter, LogicalChapter, Manga

log = get_logger(__name__)


def numbering_resets(rows) -> bool:
    """``rows``: (language, volume, chapter). True when chapter 1 starts two volumes of a language."""
    volumes: dict[str, set[float]] = defaultdict(set)
    for lang, volume, chapter in rows:
        if chapter_sort_number(chapter) != 1 or chapter_kind(chapter) != "regular":
            continue
        v = chapter_sort_number(volume)
        if v is not None:
            volumes[lang].add(v)
    return any(len(v) >= 2 for v in volumes.values())


def detect_per_volume(session: Session, manga_id: uuid.UUID) -> bool:
    rows = session.execute(
        select(ChapterRelease.language_code, ChapterRelease.volume, ChapterRelease.chapter)
        .where(ChapterRelease.manga_id == manga_id, ChapterRelease.volume.is_not(None),
               ChapterRelease.chapter.is_not(None))
    ).all()
    return numbering_resets(rows)


def ensure_numbering(session: Session, manga: Manga) -> dict | None:
    """Switch ``manga`` to per-volume numbering if its releases call for it."""
    if manga.chapters_per_volume or not detect_per_volume(session, manga.id):
        return None
    report = apply_numbering(session, manga, True)
    from app.services.control import record_event

    msg = "Chapter numbering restarts every volume: chapters are now identified by volume + chapter"
    if report.get("start_volume") is not None:
        msg += f"; start set to Vol.{report['start_volume']:g} Ch.{report['start_chapter']:g}"
    record_event(session, "numbering", msg, manga_id=manga.id)
    log.info("chapter_numbering_per_volume", manga_id=str(manga.id),
             **{k: v for k, v in report.items() if not isinstance(v, (list, dict))})
    return report


def _convert_start(manga: Manga, releases: list[ChapterRelease], per_volume: bool,
                   ext_numbers: list[float]) -> float | None:
    cfg = manga.monitoring
    start = float(cfg.start_chapter) if cfg is not None and cfg.start_chapter is not None else None
    if start is None:
        return None
    if not per_volume:
        return start % VOLUME_FACTOR if start >= VOLUME_FACTOR else start
    if start >= VOLUME_FACTOR:
        return start  # already on the per-volume scale
    # The start chapter was picked as a plain number. Owned chapters tell where the
    # collection ends; otherwise the newest volume that has this chapter number is
    # used (the safe side: older volumes are not downloaded).
    if ext_numbers:
        return max(ext_numbers)
    candidates = [logical_sort_number(r.chapter, r.volume, True) for r in releases
                  if chapter_sort_number(r.chapter) == start and chapter_sort_number(r.volume) is not None]
    if candidates:
        return max(candidates)
    numbered = [logical_sort_number(r.chapter, r.volume, True) for r in releases
                if chapter_sort_number(r.chapter) is not None and chapter_sort_number(r.volume) is not None]
    # No release with that number: start after everything listed now.
    return max(numbered) + 0.0001 if numbered else start


def apply_numbering(session: Session, manga: Manga, per_volume: bool) -> dict:
    """Re-key logical chapters, owned chapters and ``start_chapter`` of ``manga``."""
    releases = list(session.scalars(select(ChapterRelease).where(ChapterRelease.manga_id == manga.id)))
    by_id = {r.id: r for r in releases}
    lcs = {lc.chapter_key: lc for lc in session.scalars(
        select(LogicalChapter).where(LogicalChapter.manga_id == manga.id))}
    used: set[int] = set()
    moved = 0
    for r in releases:
        key = logical_chapter_key(r.chapter, str(r.id), r.volume, per_volume)
        lc = lcs.get(key)
        if lc is None:
            lc = LogicalChapter(manga_id=manga.id, chapter_key=key, chapter=r.chapter, volume=r.volume,
                                kind=chapter_kind(r.chapter))
            session.add(lc)
            lcs[key] = lc
        lc.sort_number = logical_sort_number(r.chapter, r.volume, per_volume)
        if per_volume and r.volume:
            lc.volume = r.volume
        session.flush()
        if r.logical_chapter_id != lc.id:
            r.logical_chapter_id = lc.id
            moved += 1
        used.add(lc.id)
    session.flush()
    removed = session.execute(
        delete(LogicalChapter).where(LogicalChapter.manga_id == manga.id, LogicalChapter.id.notin_(used or {-1}))
        .execution_options(synchronize_session=False)
    ).rowcount or 0

    ext_numbers: list[float] = []
    rekeyed = 0
    for e in session.scalars(select(ExternalChapter).where(ExternalChapter.manga_id == manga.id)):
        r = by_id.get(e.release_id) if e.release_id else None
        volume = r.volume if r is not None else None
        chapter = r.chapter if r is not None and r.chapter is not None else e.chapter
        if r is None and per_volume:
            key = e.chapter_key  # release unknown: its volume cannot be told, keep the key
        else:
            key = logical_chapter_key(chapter, str(e.release_id), volume, per_volume)
        if e.chapter_key != key:
            e.chapter_key = key
            rekeyed += 1
        n = logical_sort_number(chapter, volume, per_volume)
        if n is not None and (not per_volume or volume):
            ext_numbers.append(n)

    before = manga.monitoring.start_chapter if manga.monitoring is not None else None
    start = _convert_start(manga, releases, per_volume, ext_numbers)
    if manga.monitoring is not None:
        manga.monitoring.start_chapter = start
    manga.chapters_per_volume = per_volume
    session.flush()
    report = {"per_volume": per_volume, "releases_moved": moved, "logical_chapters_removed": removed,
              "external_rekeyed": rekeyed,
              "start_before": float(before) if before is not None else None, "start_after": start}
    if per_volume and start is not None:
        vol = int(start // VOLUME_FACTOR)
        report["start_volume"] = float(vol)
        report["start_chapter"] = round(start - vol * VOLUME_FACTOR, 4)
    return report
