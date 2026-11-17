"""Weekly library check: bring the database in line with folders moved out of the library by hand.

It runs the same full library scan as *Library → Scan library* (``queue_missing=False``: nothing is
downloaded): chapters whose folder is gone become *Folder deleted*, edited ones *Edited locally*,
unknown folders go to Review. Files are never touched.

Schedule: ``library_check_weekday`` (0 = Monday … 6 = Sunday, -1 = off) at ``library_check_hour``
local time (the container's ``TZ``). If the scheduler is down at that moment, the check runs when it
comes back, as long as it is within the same day (12 h). A full scan started by hand after the slot
counts as this week's check.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LibraryScanRun
from app.settings_service import AppSettings, load_settings

CATCH_UP = timedelta(hours=12)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def last_slot(settings: AppSettings, now: datetime | None = None) -> datetime | None:
    """The most recent scheduled moment at or before ``now`` (None when the check is off)."""
    if settings.library_check_weekday < 0:
        return None
    now = now or _local_now()
    slot = now.replace(hour=settings.library_check_hour, minute=0, second=0, microsecond=0)
    slot -= timedelta(days=(now.weekday() - settings.library_check_weekday) % 7)
    if slot > now:
        slot -= timedelta(days=7)
    return slot


def next_slot(settings: AppSettings, now: datetime | None = None) -> datetime | None:
    slot = last_slot(settings, now)
    return slot + timedelta(days=7) if slot is not None else None


def last_full_scan(session: Session) -> LibraryScanRun | None:
    return session.scalar(select(LibraryScanRun).where(LibraryScanRun.manga_id.is_(None))
                          .order_by(LibraryScanRun.started_at.desc()).limit(1))


def check_due(session: Session, now: datetime | None = None) -> bool:
    now = now or _local_now()
    slot = last_slot(load_settings(session), now)
    if slot is None or now - slot > CATCH_UP:
        return False
    last = last_full_scan(session)
    return last is None or last.started_at < slot


def status(session: Session) -> dict:
    settings = load_settings(session)
    last = last_full_scan(session)
    summary = (last.summary or {}) if last else {}
    totals: dict[str, int] = {}
    for per in (summary.get("manga") or {}).values():
        for k, v in (per or {}).items():
            if isinstance(v, int):
                totals[k] = totals.get(k, 0) + v
    nxt = next_slot(settings)
    return {
        "enabled": settings.library_check_weekday >= 0,
        "weekday": settings.library_check_weekday,
        "hour": settings.library_check_hour,
        "next_run": nxt.isoformat() if nxt else None,
        "last_run": None if last is None else {
            "status": last.status,
            "started_at": last.started_at.isoformat(),
            "finished_at": last.finished_at.isoformat() if last.finished_at else None,
            "error": last.error,
            "moved_out": totals.get("missing_on_disk", 0),
            "edited": totals.get("modified", 0),
            "restored": totals.get("restored", 0),
            "review": totals.get("review", 0) + int(summary.get("unknown_folders") or 0),
        },
    }
