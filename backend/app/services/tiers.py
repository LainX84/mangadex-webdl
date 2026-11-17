"""Adaptive monitoring levels ("update tiers") and the upload-complete flag.

Titles that no longer receive chapters are checked less often, so scans of
active titles are not delayed behind dormant ones.

The level is per manga (not per scanlation group): every chapter number in the
selected languages counts, whoever uploaded it, including chapters that were
removed from MangaDex later (DMCA history, ``is_unavailable``). Official
publisher link-outs are ignored. A chapter's date is the first time any release
of that chapter number was published.

    cadence = max(time since the newest chapter, gap between the two newest chapters)

* Lv.1 – cadence <= ``tier1_max_days`` (90): the normal monitoring interval;
* Lv.2 – cadence <= ``tier2_max_days`` (365): at most every ``tier2_interval_minutes`` (1 day);
* Lv.3 – older, or upload complete: at most every ``tier3_interval_minutes`` (7 days).

Using the larger of the two values means a title slows down when it stops
updating, and a single late extra after a long pause does not wake it up; it
goes back to Lv.1 as soon as two chapters arrive close together again.

"Upload complete": MangaDex marks the manga completed/cancelled with a final
chapter (or volume) number, and a chapter with at least that number exists.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, not_, select
from sqlalchemy.orm import Session, selectinload

from app.library.naming import chapter_sort_number
from app.logging_setup import get_logger
from app.models import ChapterRelease, ExternalChapter, LogicalChapter, Manga, MangaLanguage, MonitoringConfig
from app.settings_service import AppSettings, load_settings

log = get_logger(__name__)

FINAL_STATUSES = {"completed", "cancelled"}
TIER_LABELS = {1: "active", 2: "slow", 3: "dormant"}


@dataclass(frozen=True)
class TierInfo:
    tier: int
    complete: bool
    last_chapter_at: datetime | None
    prev_chapter_at: datetime | None


def is_upload_complete(status: str | None, last_chapter: str | None, last_volume: str | None,
                       max_chapter: float | None, max_volume: float | None) -> bool:
    if (status or "").lower() not in FINAL_STATUSES:
        return False
    final_ch = chapter_sort_number(last_chapter) if last_chapter else None
    if final_ch is not None:
        return max_chapter is not None and max_chapter >= final_ch
    final_vol = chapter_sort_number(last_volume) if last_volume else None
    if final_vol is not None:
        return max_volume is not None and max_volume >= final_vol
    return False


def classify(chapter_dates: Iterable[datetime], complete: bool, now: datetime,
             settings: AppSettings) -> TierInfo:
    # Chapters uploaded together (same day) give a gap of ~0: the title is clearly active.
    dates = sorted((min(d, now) for d in chapter_dates if d is not None), reverse=True)
    last = dates[0] if dates else None
    prev = dates[1] if len(dates) > 1 else None
    if complete:
        return TierInfo(3, True, last, prev)
    if last is None:
        # Nothing translated yet in the selected languages: keep checking normally.
        return TierInfo(1, False, None, None)
    age = now - last
    gap = (last - prev) if prev is not None else age
    cadence = max(age, gap)
    if cadence <= timedelta(days=settings.tier1_max_days):
        tier = 1
    elif cadence <= timedelta(days=settings.tier2_max_days):
        tier = 2
    else:
        tier = 3
    return TierInfo(tier, False, last, prev)


def effective_interval(cfg: MonitoringConfig | None, settings: AppSettings) -> int:
    """Minutes between scans for this title, taking its level into account."""
    base = (cfg.interval_minutes if cfg is not None and cfg.interval_minutes else None) \
        or settings.monitoring_interval_minutes
    if not settings.adaptive_monitoring or cfg is None:
        return base
    if cfg.update_tier == 2:
        return max(base, settings.tier2_interval_minutes)
    if cfg.update_tier == 3:
        return max(base, settings.tier3_interval_minutes)
    return base


def _chapter_rows(session: Session, ids: list[uuid.UUID]):
    """(manga_id, chapter, volume, first date) per logical chapter in the selected languages."""
    first_date = func.min(func.coalesce(ChapterRelease.publish_at, ChapterRelease.readable_at,
                                        ChapterRelease.first_seen_at))
    official_link = and_(ChapterRelease.external_url.is_not(None), ChapterRelease.pages <= 1)
    return session.execute(
        select(ChapterRelease.manga_id, LogicalChapter.chapter, LogicalChapter.volume, first_date)
        .join(LogicalChapter, LogicalChapter.id == ChapterRelease.logical_chapter_id)
        .join(MangaLanguage, and_(MangaLanguage.manga_id == ChapterRelease.manga_id,
                                  MangaLanguage.language_code == ChapterRelease.language_code))
        .where(ChapterRelease.manga_id.in_(ids), not_(official_link))
        .group_by(ChapterRelease.manga_id, LogicalChapter.id, LogicalChapter.chapter, LogicalChapter.volume)
    ).all()


def compute_tiers(session: Session, mangas: list[Manga], now: datetime,
                  settings: AppSettings) -> dict[uuid.UUID, TierInfo]:
    ids = [m.id for m in mangas]
    if not ids:
        return {}
    dates: dict[uuid.UUID, list[datetime]] = defaultdict(list)
    max_ch: dict[uuid.UUID, float] = {}
    max_vol: dict[uuid.UUID, float] = {}

    def _bump(d: dict, mid, value) -> None:
        if value is not None and (mid not in d or value > d[mid]):
            d[mid] = value

    for mid, chapter, volume, first in _chapter_rows(session, ids):
        if first is not None:
            dates[mid].append(first if first.tzinfo else first.replace(tzinfo=UTC))
        # Plain chapter number (not the per-volume sort number): compared with the
        # manga's last chapter for "upload complete".
        _bump(max_ch, mid, chapter_sort_number(chapter) if chapter else None)
        _bump(max_vol, mid, chapter_sort_number(volume) if volume else None)
    # Chapters owned outside this library (e.g. imported from JAM) count for completion too.
    for mid, chapter in session.execute(
        select(ExternalChapter.manga_id, ExternalChapter.chapter).where(ExternalChapter.manga_id.in_(ids))
    ):
        _bump(max_ch, mid, chapter_sort_number(chapter) if chapter else None)

    out = {}
    for m in mangas:
        complete = is_upload_complete(m.status, m.last_chapter, m.last_volume, max_ch.get(m.id), max_vol.get(m.id))
        out[m.id] = classify(dates.get(m.id, []), complete, now, settings)
    return out


def refresh_tiers(session: Session, manga_ids: Iterable[uuid.UUID] | None = None,
                  now: datetime | None = None, batch: int = 200) -> dict[str, int]:
    """Recompute and store the level of the given titles (all when ``None``)."""
    from app.services.control import record_event
    from app.services.jobs import next_scan_time

    now = now or datetime.now(UTC)
    settings = load_settings(session)
    stmt = select(Manga).options(selectinload(Manga.monitoring)).order_by(Manga.id)
    if manga_ids is not None:
        manga_ids = list(manga_ids)
        if not manga_ids:
            return {"updated": 0, "changed": 0}
        stmt = stmt.where(Manga.id.in_(manga_ids))
    mangas = [m for m in session.scalars(stmt) if m.monitoring is not None]
    changed = 0
    for start in range(0, len(mangas), batch):
        chunk = mangas[start:start + batch]
        infos = compute_tiers(session, chunk, now, settings)
        for m in chunk:
            cfg = m.monitoring
            info = infos[m.id]
            old = cfg.update_tier
            cfg.last_chapter_at = info.last_chapter_at
            cfg.prev_chapter_at = info.prev_chapter_at
            cfg.upload_complete = info.complete
            cfg.tier_computed_at = now
            if old == info.tier:
                continue
            cfg.update_tier = info.tier
            changed += 1
            if old is not None:
                note = " (upload complete)" if info.complete else ""
                record_event(session, "tier", f"Monitoring level {old} → {info.tier}{note}", manga_id=m.id)
                if info.tier < old and cfg.monitoring_enabled:
                    # Checked more often from now on: do not wait for the old, longer interval.
                    interval = effective_interval(cfg, settings)
                    if cfg.next_scan_at is None or cfg.next_scan_at > now + timedelta(minutes=interval):
                        cfg.next_scan_at = next_scan_time(interval, now)
        session.flush()
    if changed:
        log.info("update_tiers_refreshed", titles=len(mangas), changed=changed)
    return {"updated": len(mangas), "changed": changed}
