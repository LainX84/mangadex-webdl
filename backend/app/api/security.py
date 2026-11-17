"""Password hashing (Argon2id), DB-backed sessions, CSRF, login throttling, proxy trust."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Config, get_config
from app.models import LoginAttempt, User, UserSession

SESSION_COOKIE = "mdm_session"
CSRF_COOKIE = "mdm_csrf"
CSRF_HEADER = "x-csrf-token"
MIN_PASSWORD_LENGTH = 12

# Argon2id (the argon2-cffi default type) with RFC 9106 "low memory" parameters.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)
# Used to equalise timing when the username does not exist.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str, username: str | None = None) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
    if len(password) > 1024:
        raise ValueError("Password is too long")
    if username and password.lower() == username.lower():
        raise ValueError("Password must not equal the username")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ sessions


def create_session(db: Session, user: User, ip: str | None, user_agent: str | None, cfg: Config | None = None) -> tuple[str, UserSession]:
    cfg = cfg or get_config()
    raw = secrets.token_urlsafe(32)
    sess = UserSession(
        id=token_hash(raw),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        created_at=_now(),
        last_seen_at=_now(),
        expires_at=_now() + timedelta(days=cfg.session_absolute_days),
        ip=(ip or "")[:64] or None,
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(sess)
    db.flush()
    return raw, sess


def load_session(db: Session, raw_token: str | None, cfg: Config | None = None) -> UserSession | None:
    if not raw_token or len(raw_token) > 200:
        return None
    cfg = cfg or get_config()
    sess = db.get(UserSession, token_hash(raw_token))
    if sess is None:
        return None
    now = _now()
    if sess.expires_at <= now or sess.last_seen_at + timedelta(hours=cfg.session_idle_hours) <= now:
        db.delete(sess)
        db.commit()
        return None
    user = sess.user
    if user is None or not user.is_active:
        return None
    if user.password_changed_at and sess.created_at < user.password_changed_at:
        return None
    if now - sess.last_seen_at > timedelta(minutes=5):
        sess.last_seen_at = now
        db.commit()
    return sess


def revoke_user_sessions(db: Session, user_id: int, except_id: str | None = None) -> None:
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user_id)):
        if s.id != except_id:
            db.delete(s)


def csrf_ok(sess: UserSession, header_value: str | None) -> bool:
    return bool(header_value) and hmac.compare_digest(sess.csrf_token, header_value or "")


# ------------------------------------------------------------------ throttling


def login_delay_seconds(db: Session, username: str, ip: str, cfg: Config | None = None, now: datetime | None = None) -> int:
    """Seconds the client must wait before another login attempt (0 = allowed).

    Failures are counted per username and per IP within the window, since the
    last success. After ``login_max_failures`` the lockout doubles with every
    further failure, capped at ``login_lockout_max_seconds``.
    """
    cfg = cfg or get_config()
    now = now or _now()
    window_start = now - timedelta(minutes=cfg.login_window_minutes)
    worst = 0
    for col, value in ((LoginAttempt.username, username.lower()), (LoginAttempt.ip, ip)):
        last_success = db.scalar(
            select(func.max(LoginAttempt.attempted_at)).where(col == value, LoginAttempt.success.is_(True))
        )
        since = max(window_start, last_success) if last_success else window_start
        failures, last_failure = db.execute(
            select(func.count(), func.max(LoginAttempt.attempted_at)).where(
                col == value, LoginAttempt.success.is_(False), LoginAttempt.attempted_at > since
            )
        ).one()
        if failures >= cfg.login_max_failures and last_failure is not None:
            lock = min(cfg.login_lockout_max_seconds, 30 * 2 ** (failures - cfg.login_max_failures))
            remaining = int((last_failure + timedelta(seconds=lock) - now).total_seconds())
            worst = max(worst, remaining)
    return max(0, worst)


def record_login_attempt(db: Session, username: str, ip: str, success: bool) -> None:
    db.add(LoginAttempt(username=username.lower()[:64], ip=ip[:64], success=success, attempted_at=_now()))


# ------------------------------------------------------------------ proxy trust


def _is_trusted(ip: str, networks) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


class TrustedProxyMiddleware:
    """Apply X-Forwarded-For / X-Forwarded-Proto only from configured proxies.

    The client address is the right-most address in X-Forwarded-For that is
    not itself a trusted proxy. Requests that do not come from a trusted proxy
    keep their socket address and scheme, so headers cannot be spoofed.
    """

    def __init__(self, app: ASGIApp, networks=None):
        self.app = app
        self.networks = networks if networks is not None else get_config().trusted_proxy_networks

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and self.networks:
            client = scope.get("client")
            if client and _is_trusted(client[0], self.networks):
                headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
                xff = headers.get("x-forwarded-for")
                if xff:
                    hops = [h.strip() for h in xff.split(",") if h.strip()]
                    real = None
                    for hop in reversed(hops):
                        if not _is_trusted(hop, self.networks):
                            real = hop
                            break
                    real = real or (hops[0] if hops else None)
                    if real:
                        scope = dict(scope)
                        scope["client"] = (real, 0)
                proto = headers.get("x-forwarded-proto")
                if proto in ("http", "https"):
                    scope = dict(scope)
                    scope["scheme"] = proto
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in headers}

                def add(k: str, v: str):
                    if k.encode() not in existing:
                        headers.append((k.encode(), v.encode()))

                add("x-content-type-options", "nosniff")
                add("x-frame-options", "DENY")
                add("referrer-policy", "same-origin")
                add("permissions-policy", "camera=(), microphone=(), geolocation=()")
                add(
                    "content-security-policy",
                    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
                )
                if scope.get("scheme") == "https":
                    add("strict-transport-security", "max-age=31536000")
                if scope.get("path", "").startswith("/api/"):
                    add("cache-control", "no-store")
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, _send)
