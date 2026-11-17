from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.security import CSRF_HEADER, SESSION_COOKIE, csrf_ok, load_session
from app.config import get_config
from app.db import get_db
from app.models import User, UserSession

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class AuthContext:
    user: User
    session: UserSession


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _origin_allowed(request: Request) -> bool:
    """Reject cross-site state-changing requests (defence in depth on top of CSRF tokens)."""
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        if not referer:
            return True  # non-browser clients; the CSRF token is still required
        origin = referer
    o = urlparse(origin)
    allowed = {request.headers.get("host", "")}
    cfg = get_config()
    if cfg.public_url:
        allowed.add(urlparse(cfg.public_url).netloc)
    return o.netloc in allowed


def require_auth(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    sess = load_session(db, request.cookies.get(SESSION_COOKIE))
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    if request.method not in SAFE_METHODS:
        if not _origin_allowed(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request rejected")
        if not csrf_ok(sess, request.headers.get(CSRF_HEADER)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token")
    return AuthContext(user=sess.user, session=sess)


def require_admin(ctx: AuthContext = Depends(require_auth)) -> AuthContext:
    if not ctx.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator required")
    return ctx


def cookie_secure(request: Request) -> bool:
    mode = get_config().cookie_secure
    if mode == "true":
        return True
    if mode == "false":
        return False
    return request.url.scheme == "https"
