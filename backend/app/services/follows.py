"""MangaDex account sync: followed list → titles waiting for approval; followed feed → fast updates.

Two independent jobs, both scheduled by the scheduler and run on the worker's
``follows`` queue:

* **follow list** (every few hours): mirrors the account's followed list and reading
  statuses into ``mangadex_follows``. Followed titles that are not in the library yet
  are added with ``pending_approval`` (nothing is scanned or downloaded until the user
  approves them and picks languages, groups and the first chapter).
* **follow feed** (every ~15 min): one request to ``/user/follows/manga/feed`` returns
  the chapters updated since the last run for *every* followed manga. New releases of
  approved, monitored titles are stored and – with auto-download on – queued right away.

The per-manga monitoring scans are unchanged and keep running on their own schedule:
they cover titles that are not followed on MangaDex and act as a safety net.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import session_scope
from app.logging_setup import get_logger
from app.mangadex.account import MangaDexAccount, credentials_status, get_account, load_credentials
from app.mangadex.client import MangaDexClient
from app.mangadex.errors import AccountAuthError, AccountNotConfigured
from app.mangadex.parse import parse_chapter, parse_manga
from app.models import Manga, MangaDexFollow, MangaLanguage, MonitoringConfig
from app.redis_client import get_redis
from app.settings_service import MD_ACCOUNT_STATE_KEY, get_raw, load_settings, set_raw

log = get_logger(__name__)

QUEUE_FOLLOWS = "follows"
PENDING_KEY = "mdm:follow_sync_pending:"
# The feed is asked for chapters updated since the previous run minus this overlap
# (clock skew, chapters becoming readable late). Duplicates are harmless.
FEED_OVERLAP = timedelta(minutes=10)
# First run, or after a long outage: older chapters are left to the per-manga scans.
FEED_MAX_LOOKBACK = timedelta(days=2)


def now_utc() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# ----------------------------------------------------------------- state


def get_state(session: Session) -> dict:
    return dict(get_raw(session, MD_ACCOUNT_STATE_KEY, {}) or {})


def update_state(session: Session, **changes) -> dict:
    state = get_state(session) | {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in changes.items()}
    set_raw(session, MD_ACCOUNT_STATE_KEY, state)
    return state


def _record_error(kind: str, exc: Exception) -> None:
    msg = f"{exc.__class__.__name__}: {exc}"[:500]
    with session_scope() as s:
        update_state(s, last_error=msg, last_error_at=now_utc(), last_error_kind=kind,
                     auth_failed=isinstance(exc, AccountAuthError))
    log.warning("follow_sync_failed", kind=kind, error=msg)


def account_status(session: Session) -> dict:
    settings = load_settings(session)
    state = get_state(session)
    secrets = credentials_status()
    configured = all(secrets.values())
    pending = session.scalar(select(func.count()).select_from(Manga).where(Manga.pending_approval.is_(True))) or 0
    followed = session.scalar(select(func.count()).select_from(MangaDexFollow)
                              .where(MangaDexFollow.followed.is_(True))) or 0
    return {
        "configured": configured,
        "secrets": secrets,
        "enabled": settings.mangadex_account_enabled,
        "active": configured and settings.mangadex_account_enabled,
        "username": state.get("username"),
        "followed": followed,
        "pending_approval": pending,
        "last_list_sync_at": state.get("last_list_ok_at"),
        "last_feed_sync_at": state.get("last_feed_ok_at"),
        "feed_cursor": state.get("feed_cursor"),
        "last_feed_result": state.get("last_feed_result"),
        "last_list_result": state.get("last_list_result"),
        "last_error": state.get("last_error"),
        "last_error_at": state.get("last_error_at"),
        "auth_failed": bool(state.get("auth_failed")),
        "feed_interval_minutes": settings.follow_feed_interval_minutes,
        "list_interval_minutes": settings.follow_list_interval_minutes,
    }


# ----------------------------------------------------------------- scheduling


def account_active(session: Session) -> bool:
    return load_settings(session).mangadex_account_enabled and load_credentials() is not None


def due_syncs(session: Session, now: datetime | None = None) -> list[str]:
    """Which syncs are due ("list", "feed"). After an auth failure, retry at most hourly."""
    now = now or now_utc()
    if not account_active(session):
        return []
    settings = load_settings(session)
    state = get_state(session)
    if state.get("auth_failed"):
        err_at = _parse_dt(state.get("last_error_at"))
        if err_at and now - err_at < timedelta(hours=1):
            return []
    due = []
    list_at = _parse_dt(state.get("last_list_at"))
    if list_at is None or now - list_at >= timedelta(minutes=settings.follow_list_interval_minutes):
        due.append("list")
    feed_at = _parse_dt(state.get("last_feed_at"))
    if feed_at is None or now - feed_at >= timedelta(minutes=settings.follow_feed_interval_minutes):
        due.append("feed")
    return due


def enqueue_follow_sync(kind: str, force: bool = False) -> bool:
    """Queue one follow sync; de-duplicated so the same kind never runs twice at once."""
    from app.services.jobs import get_queue

    r = get_redis()
    key = PENDING_KEY + kind
    if not r.set(key, "1", nx=True, ex=3600):
        return False
    try:
        get_queue(QUEUE_FOLLOWS).enqueue("app.tasks.task_follow_sync", kind, force,
                                         job_timeout=3600, result_ttl=3600, failure_ttl=7 * 86400)
    except Exception:
        r.delete(key)
        raise
    return True


def schedule_follow_syncs(session: Session, now: datetime | None = None, enqueue=None) -> list[str]:
    from app.ratelimit import get_limiter

    limiter = get_limiter()
    if limiter.block_state() or limiter.cooldown_state():
        return []
    started = []
    for kind in due_syncs(session, now):
        if (enqueue or enqueue_follow_sync)(kind):
            started.append(kind)
    return started


# ----------------------------------------------------------------- follow list


def sync_follow_list(client: MangaDexClient, account: MangaDexAccount | None = None) -> dict:
    """Mirror the followed list; add unknown titles as pending approval."""
    started = now_utc()
    with session_scope() as s:
        update_state(s, last_list_at=started)
        settings = load_settings(s)
    try:
        account = account or get_account(client)
        me = account.me()
        follows = list(account.iter_follows())
        statuses = account.reading_statuses()
    except AccountNotConfigured:
        return {"status": "not_configured"}
    except Exception as exc:
        _record_error("list", exc)
        raise

    added: list[str] = []
    added_ids: list[str] = []
    seen: set[uuid.UUID] = set()
    with session_scope() as s:
        existing = {f.manga_id: f for f in s.scalars(select(MangaDexFollow))}
        in_library = set(s.scalars(select(Manga.id)))
        for data in follows:
            try:
                mid = uuid.UUID(str(data.get("id")))
            except ValueError:
                continue
            seen.add(mid)
            parsed = parse_manga(data)
            row = existing.get(mid)
            if row is None:
                row = MangaDexFollow(manga_id=mid, title=parsed.title, first_seen_at=started)
                s.add(row)
            row.title = parsed.title
            row.data = {"id": str(mid), "type": "manga", "attributes": data.get("attributes") or {},
                        "relationships": data.get("relationships") or []}
            row.reading_status = statuses.get(str(mid))
            row.followed = True
            row.last_seen_at = started
            row.unfollowed_at = None
            if settings.follow_add_pending and mid not in in_library and row.dismissed_at is None:
                try:
                    with s.begin_nested():
                        from app.services.catalog import add_pending_manga
                        from app.services.control import record_event

                        add_pending_manga(s, parsed, settings.follow_default_languages)
                        record_event(s, "follow_pending", "Followed on MangaDex; waiting for approval", manga_id=mid)
                    in_library.add(mid)
                    added.append(parsed.title)
                    added_ids.append(str(mid))
                except IntegrityError:
                    log.info("follow_pending_add_conflict", manga_id=str(mid))
        for mid, row in existing.items():
            if mid not in seen and row.followed:
                row.followed = False
                row.unfollowed_at = started
        result = {"followed": len(seen), "added_pending": len(added), "at": started.isoformat()}
        update_state(s, last_list_ok_at=now_utc(), username=me.get("username"), last_list_result=result,
                     auth_failed=False, last_error=None, last_error_at=None)
    log.info("follow_list_synced", followed=len(seen), added_pending=len(added), requests=client.requests_made)
    return {"status": "success", **result, "titles": added[:50], "added_ids": added_ids}


# ----------------------------------------------------------------- follow feed


def _scan_lock_key(manga_id: uuid.UUID) -> int:
    from app.services.monitor import _lock_id

    return _lock_id(manga_id)


def sync_follow_feed(client: MangaDexClient, account: MangaDexAccount | None = None,
                     now: datetime | None = None) -> dict:
    """Fetch new chapters of every followed manga in one feed and queue the wanted ones."""
    from app.services.catalog import upsert_release
    from app.services.control import record_event
    from app.services.monitor import queue_missing
    from app.services.tiers import refresh_tiers

    started = now or now_utc()
    with session_scope() as s:
        state = update_state(s, last_feed_at=started)
        settings = load_settings(s)
        # Approved, monitored titles and their languages: the only ones the feed may update.
        targets: dict[uuid.UUID, set[str]] = defaultdict(set)
        for mid, code in s.execute(
            select(MangaLanguage.manga_id, MangaLanguage.language_code)
            .join(Manga, Manga.id == MangaLanguage.manga_id)
            .join(MonitoringConfig, MonitoringConfig.manga_id == Manga.id)
            .where(Manga.pending_approval.is_(False), MonitoringConfig.monitoring_enabled.is_(True))
        ):
            targets[mid].add(code)
    cursor = _parse_dt(state.get("feed_cursor"))
    since = max(cursor - FEED_OVERLAP, started - FEED_MAX_LOOKBACK) if cursor else started - FEED_MAX_LOOKBACK
    languages = sorted({c for codes in targets.values() for c in codes})
    if not languages:
        with session_scope() as s:
            update_state(s, feed_cursor=started, last_feed_ok_at=now_utc(),
                         last_feed_result={"seen": 0, "new": 0, "queued": 0, "at": started.isoformat()})
        return {"status": "success", "seen": 0, "new": 0, "queued": 0}

    try:
        account = account or get_account(client)
        items = list(account.iter_follows_feed(since, languages, settings.content_ratings))
    except AccountNotConfigured:
        return {"status": "not_configured"}
    except Exception as exc:
        _record_error("feed", exc)
        raise

    by_manga: dict[uuid.UUID, list] = defaultdict(list)
    ignored = 0
    for item in items:
        ch = parse_chapter(item)
        try:
            mid = uuid.UUID(ch.manga_id) if ch.manga_id else None
        except ValueError:
            mid = None
        if mid is None or mid not in targets or ch.language not in targets[mid]:
            ignored += 1  # not in the library, pending, not monitored, or another language
            continue
        by_manga[mid].append(ch)

    seen = new = queued = 0
    busy: list[str] = []
    updated: list[uuid.UUID] = []
    for mid, chapters in by_manga.items():
        with session_scope() as s:
            # A per-manga scan running right now fetches these chapters itself.
            if not s.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _scan_lock_key(mid)}).scalar():
                busy.append(str(mid))
                continue
            manga = s.get(Manga, mid)
            if manga is None or manga.pending_approval:
                continue
            n_new = 0
            for ch in chapters:
                _, is_new = upsert_release(s, mid, ch)
                n_new += int(is_new)
            from app.services.numbering import ensure_numbering

            ensure_numbering(s, manga)
            seen += len(chapters)
            new += n_new
            n_queued = queue_missing(s, manga, "follow_feed") if manga.monitoring.auto_download else 0
            queued += n_queued
            if n_new or n_queued:
                record_event(s, "follow_feed", f"{n_new} new release(s), {n_queued} queued (MangaDex followed feed)",
                             manga_id=mid)
            updated.append(mid)
    if updated:
        try:
            with session_scope() as s:
                refresh_tiers(s, updated, now_utc())
        except Exception:
            log.exception("follow_feed_tier_refresh_failed")
        from app.services.layout import rename_after_volume_changes

        for mid in updated:  # "Volume TBD …" folders whose volume MangaDex just set
            rename_after_volume_changes(mid)
    result = {"seen": seen, "new": new, "queued": queued, "ignored": ignored, "manga": len(updated),
              "busy": len(busy), "since": since.isoformat(), "at": started.isoformat()}
    with session_scope() as s:
        # Titles skipped because a scan held them are picked up by that scan, so the cursor moves on.
        update_state(s, feed_cursor=started, last_feed_ok_at=now_utc(), last_feed_result=result,
                     auth_failed=False, last_error=None, last_error_at=None)
    log.info("follow_feed_synced", **result, requests=client.requests_made)
    return {"status": "success", **result}


# ----------------------------------------------------------------- approvals


def restore_dismissed(session: Session, manga_id: uuid.UUID) -> Manga:
    """Bring back a rejected follow as a title waiting for approval."""
    from app.services.catalog import CatalogError, add_pending_manga

    row = session.get(MangaDexFollow, manga_id)
    if row is None or row.dismissed_at is None:
        raise CatalogError("This title was not dismissed")
    if session.get(Manga, manga_id) is not None:
        raise CatalogError("This manga is already in the library")
    row.dismissed_at = None
    manga = add_pending_manga(session, parse_manga(row.data), load_settings(session).follow_default_languages)
    return manga


def remember_removed(session: Session, manga: Manga) -> None:
    """A title removed from the library must not come back through the followed list."""
    row = session.get(MangaDexFollow, manga.id)
    if row is None:
        row = MangaDexFollow(manga_id=manga.id, title=manga.title, followed=False,
                             data={"id": str(manga.id), "type": "manga", **(manga.raw or {})})
        session.add(row)
    row.dismissed_at = now_utc()


def dismiss_pending(session: Session, manga: Manga) -> None:
    """Reject a title waiting for approval: removed from the library, never proposed again."""
    from app.services.catalog import CatalogError

    if not manga.pending_approval:
        raise CatalogError("Only titles waiting for approval can be dismissed")
    remember_removed(session, manga)
    session.delete(manga)
    session.flush()
