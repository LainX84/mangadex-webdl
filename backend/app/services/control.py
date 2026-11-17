"""Cross-cutting helpers: client factory, access-block persistence, events, mount status."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db import session_scope
from app.library.storage import MountStatus, check_library
from app.mangadex.client import MangaDexClient
from app.models import JobEvent
from app.ratelimit import get_limiter
from app.redis_client import get_redis
from app.settings_service import ACCESS_BLOCK_KEY, delete_raw, get_raw, load_settings, set_raw

MOUNT_STATUS_KEY = "mdm:mount_status"
HEARTBEAT_PREFIX = "mdm:heartbeat:"


def _settings_provider():
    with session_scope() as s:
        return load_settings(s)


def persist_access_block(info: dict) -> None:
    with session_scope() as s:
        set_raw(s, ACCESS_BLOCK_KEY, info)


def build_client(http=None) -> MangaDexClient:
    return MangaDexClient(
        limiter=get_limiter(),
        settings_provider=_settings_provider,
        http=http,
        on_access_blocked=persist_access_block,
        cache=get_redis(),
    )


def sync_access_block_to_redis(session: Session) -> None:
    """Redis may have been flushed; the database is the durable record."""
    info = get_raw(session, ACCESS_BLOCK_KEY)
    limiter = get_limiter()
    if info:
        limiter.set_block(info)
    else:
        limiter.clear_block()


def clear_access_block(session: Session) -> None:
    delete_raw(session, ACCESS_BLOCK_KEY)
    get_limiter().clear_block()


def record_event(
    session: Session,
    event: str,
    message: str | None = None,
    *,
    job_id: int | None = None,
    release_id: uuid.UUID | None = None,
    manga_id: uuid.UUID | None = None,
) -> None:
    session.add(JobEvent(event=event, message=(message or "")[:2000] or None, job_id=job_id, release_id=release_id, manga_id=manga_id))


def refresh_mount_status() -> MountStatus:
    st = check_library()
    payload = st.to_dict() | {"checked_at": datetime.now(UTC).isoformat()}
    try:
        get_redis().set(MOUNT_STATUS_KEY, json.dumps(payload), ex=600)
    except Exception:
        pass
    return st


def cached_mount_status() -> dict | None:
    raw = get_redis().get(MOUNT_STATUS_KEY)
    return json.loads(raw) if raw else None


def heartbeat(component: str, ttl: int = 90) -> None:
    get_redis().set(HEARTBEAT_PREFIX + component, str(time.time()), ex=ttl)


def last_heartbeat(component: str) -> float | None:
    raw = get_redis().get(HEARTBEAT_PREFIX + component)
    return float(raw) if raw else None
