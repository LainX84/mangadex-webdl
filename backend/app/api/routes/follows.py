"""MangaDex account: status, manual sync, and titles waiting for approval."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.clients import get_client
from app.api.errors import mangadex_errors
from app.api.serializers import cover_url, iso
from app.db import get_db
from app.mangadex.client import MangaDexClient
from app.mangadex.errors import AccountAuthError, AccountNotConfigured
from app.models import Manga, MangaDexFollow
from app.services.catalog import CatalogError
from app.services.follows import (
    account_status,
    dismiss_pending,
    enqueue_follow_sync,
    restore_dismissed,
)

router = APIRouter(prefix="/api", tags=["mangadex-account"])

READING_STATUS = ("reading", "plan_to_read", "on_hold", "re_reading", "completed", "dropped")


class SyncIn(BaseModel):
    kind: Literal["list", "feed", "all"] = "all"


@router.get("/mangadex-account")
def get_account_status(db: Session = Depends(get_db)) -> dict:
    return account_status(db)


@router.post("/mangadex-account/test")
def test_account(client: MangaDexClient = Depends(get_client)) -> dict:
    """Log in with the configured secrets and read the account name (2 requests at most)."""
    from app.mangadex.account import get_account

    with mangadex_errors():
        try:
            me = get_account(client).me()
        except AccountNotConfigured as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except AccountAuthError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"MangaDex login failed: {exc}") from exc
    return {"ok": True, "username": me.get("username")}


@router.post("/mangadex-account/sync")
def sync_now(body: SyncIn, db: Session = Depends(get_db)) -> dict:
    st = account_status(db)
    if not st["configured"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MangaDex account is not configured")
    kinds = ["list", "feed"] if body.kind == "all" else [body.kind]
    return {"queued": {k: enqueue_follow_sync(k, force=True) for k in kinds}}


def english_title(m: Manga) -> str | None:
    """The English title: the main MangaDex title when it is English, else the first English alternative title."""
    main = ((m.raw or {}).get("attributes") or {}).get("title") or {}
    if isinstance(main, dict) and isinstance(main.get("en"), str) and main["en"].strip():
        return main["en"].strip()
    for alt in m.alt_titles or []:
        if isinstance(alt, dict) and isinstance(alt.get("en"), str) and alt["en"].strip():
            return alt["en"].strip()
    return None


def _pending_out(m: Manga, follow: MangaDexFollow | None) -> dict:
    cfg = m.monitoring
    return {
        "id": str(m.id),
        "title": m.display_title,
        "english_title": english_title(m),
        "canonical_title": m.title,
        "cover_url": cover_url(m.id, m.cover_filename),
        "description": (m.description or "")[:500],
        "status": m.status,
        "year": m.year,
        "content_rating": m.content_rating,
        "original_language": m.original_language,
        "last_chapter": m.last_chapter,
        "available_languages": m.available_languages,
        "languages": sorted(lang.language_code for lang in m.languages),
        "tags": list(m.tags or []),
        "pending_since": iso(m.pending_since),
        "reading_status": follow.reading_status if follow else None,
        "followed": bool(follow and follow.followed),
        "monitoring_enabled": cfg.monitoring_enabled if cfg else False,
        "mangadex_url": f"https://mangadex.org/title/{m.id}",
    }


@router.get("/approvals")
def list_approvals(db: Session = Depends(get_db)) -> dict:
    """Titles from the MangaDex followed list waiting for approval, plus dismissed ones."""
    pending = list(db.scalars(
        select(Manga).options(selectinload(Manga.monitoring), selectinload(Manga.languages))
        .where(Manga.pending_approval.is_(True))
    ))
    follows = {f.manga_id: f for f in db.scalars(select(MangaDexFollow))}
    order = {s: i for i, s in enumerate(READING_STATUS)}
    items = [_pending_out(m, follows.get(m.id)) for m in pending]
    # Reading first, then plan to read, ...; newest additions first within a status.
    items.sort(key=lambda x: x["pending_since"] or "", reverse=True)
    items.sort(key=lambda x: order.get(x["reading_status"] or "", len(order)))
    dismissed = [
        {"id": str(f.manga_id), "title": f.title, "reading_status": f.reading_status, "followed": f.followed,
         "dismissed_at": iso(f.dismissed_at)}
        for f in sorted(follows.values(), key=lambda f: f.dismissed_at or f.first_seen_at, reverse=True)
        if f.dismissed_at is not None and f.followed
    ]
    return {"items": items, "total": len(items), "dismissed": dismissed}


@router.post("/approvals/{manga_id}/dismiss")
def dismiss(manga_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    m = db.get(Manga, manga_id)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Manga not found")
    try:
        dismiss_pending(db, m)
    except CatalogError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return {"ok": True}


@router.post("/approvals/{manga_id}/restore")
def restore(manga_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    try:
        m = restore_dismissed(db, manga_id)
    except CatalogError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    return {"ok": True, "id": str(m.id)}
