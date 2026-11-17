from __future__ import annotations

import hmac
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import AuthContext, client_ip, cookie_secure, require_auth
from app.api.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    create_session,
    hash_password,
    login_delay_seconds,
    needs_rehash,
    record_login_attempt,
    revoke_user_sessions,
    token_hash,
    validate_password_strength,
    verify_password,
)
from app.config import get_config
from app.db import get_db
from app.logging_setup import get_logger
from app.models import User
from app.settings_service import SETUP_TOKEN_KEY, delete_raw, get_raw

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = get_logger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SetupIn(BaseModel):
    setup_token: str = Field(min_length=10, max_length=200)
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


def _set_session_cookies(request: Request, response: Response, raw: str, csrf: str) -> None:
    cfg = get_config()
    secure = cookie_secure(request)
    max_age = cfg.session_absolute_days * 86400
    response.set_cookie(SESSION_COOKIE, raw, max_age=max_age, httponly=True, secure=secure, samesite="lax", path="/")
    # Readable by the SPA so it can echo it in the X-CSRF-Token header.
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=secure, samesite="strict", path="/")


def _clear_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _user_out(user: User, csrf: str) -> dict:
    return {"id": user.id, "username": user.username, "is_admin": user.is_admin, "csrf_token": csrf}


@router.get("/status")
def auth_status(db: Session = Depends(get_db)) -> dict:
    has_users = (db.scalar(select(func.count()).select_from(User)) or 0) > 0
    return {"setup_required": not has_users}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    ip = client_ip(request)
    username = body.username.strip()
    wait = login_delay_seconds(db, username, ip)
    if wait > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many failed attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    user = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    ok = verify_password(user.password_hash if user else None, body.password)
    if not ok or user is None or not user.is_active:
        record_login_attempt(db, username, ip, False)
        db.commit()
        log.warning("login_failed", username=username[:64], ip=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    record_login_attempt(db, username, ip, True)
    user.last_login_at = datetime.now(UTC)
    raw, sess = create_session(db, user, ip, request.headers.get("user-agent"))
    db.commit()
    _set_session_cookies(request, response, raw, sess.csrf_token)
    log.info("login_succeeded", username=user.username, ip=ip)
    return _user_out(user, sess.csrf_token)


@router.post("/logout")
def logout(response: Response, ctx: AuthContext = Depends(require_auth), db: Session = Depends(get_db)) -> dict:
    sess = db.get(type(ctx.session), ctx.session.id)
    if sess is not None:
        db.delete(sess)
        db.commit()
    _clear_cookies(response)
    return {"ok": True}


@router.post("/logout-all")
def logout_all(response: Response, ctx: AuthContext = Depends(require_auth), db: Session = Depends(get_db)) -> dict:
    revoke_user_sessions(db, ctx.user.id)
    db.commit()
    _clear_cookies(response)
    return {"ok": True}


@router.get("/me")
def me(ctx: AuthContext = Depends(require_auth)) -> dict:
    return _user_out(ctx.user, ctx.session.csrf_token)


@router.post("/change-password")
def change_password(body: ChangePasswordIn, request: Request, response: Response,
                    ctx: AuthContext = Depends(require_auth), db: Session = Depends(get_db)) -> dict:
    user = db.get(User, ctx.user.id)
    if not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    try:
        validate_password_strength(body.new_password, user.username)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = datetime.now(UTC)
    revoke_user_sessions(db, user.id)
    raw, sess = create_session(db, user, client_ip(request), request.headers.get("user-agent"))
    db.commit()
    _set_session_cookies(request, response, raw, sess.csrf_token)
    return _user_out(user, sess.csrf_token)


@router.post("/setup")
def first_run_setup(body: SetupIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    """Create the first administrator. Requires the one-time token printed in the API logs."""
    if (db.scalar(select(func.count()).select_from(User)) or 0) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed")
    ip = client_ip(request)
    wait = login_delay_seconds(db, "__setup__", ip)
    if wait > 0:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Too many attempts. Try again in {wait} seconds.",
                            headers={"Retry-After": str(wait)})
    expected = get_raw(db, SETUP_TOKEN_KEY)
    if not expected or not hmac.compare_digest(expected, token_hash(body.setup_token.strip())):
        record_login_attempt(db, "__setup__", ip, False)
        db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid setup token")
    if not USERNAME_RE.match(body.username):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Username must be 3-64 characters: letters, digits, . _ @ -")
    try:
        validate_password_strength(body.password, body.username)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    user = User(username=body.username, password_hash=hash_password(body.password), is_admin=True,
                password_changed_at=datetime.now(UTC))
    db.add(user)
    delete_raw(db, SETUP_TOKEN_KEY)
    db.flush()
    raw, sess = create_session(db, user, ip, request.headers.get("user-agent"))
    db.commit()
    _set_session_cookies(request, response, raw, sess.csrf_token)
    log.info("admin_created_via_setup", username=user.username)
    return _user_out(user, sess.csrf_token)
