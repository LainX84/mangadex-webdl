"""MangaDex account access (personal API client, read-only).

MangaDex personal clients use the OAuth2 *password* grant: client id + secret plus
the account's username and password are exchanged at ``auth.mangadex.org`` for a
short-lived access token (~15 min) and a refresh token. Credentials come from Docker
secrets (one file per value, see ``app.config``); nothing is stored in the database.

Tokens are kept in Redis so every process (API, worker, scheduler) shares one login
instead of logging in separately. Only these read-only endpoints are used:

* ``GET /user/me``                  – connection test
* ``GET /user/follows/manga``       – the followed list
* ``GET /manga/status``             – reading status of every manga
* ``GET /user/follows/manga/feed``  – new chapters of every followed manga, in one feed

Account failures raise :class:`AccountAuthError`; they never stop anonymous traffic.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_config
from app.logging_setup import get_logger
from app.mangadex.client import MangaDexClient
from app.mangadex.errors import AccountAuthError, AccountNotConfigured, InvalidResponseError

log = get_logger(__name__)

TOKEN_KEY = "mdm:md_account:token"
TOKEN_LOCK_KEY = "mdm:md_account:token_lock"
# Refresh a little before MangaDex considers the token expired.
EXPIRY_MARGIN_S = 60
FOLLOWS_PAGE_SIZE = 100
FEED_PAGE_SIZE = 500
MAX_OFFSET_WINDOW = 10_000


@dataclass(frozen=True)
class AccountCredentials:
    client_id: str
    client_secret: str
    username: str
    password: str

    @property
    def fingerprint(self) -> str:
        """Identifies the credential set without revealing it (cached tokens are tied to it)."""
        import hashlib

        return hashlib.sha256(f"{self.client_id}\0{self.username}".encode()).hexdigest()[:16]


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def credentials_status() -> dict[str, bool]:
    """Which of the four secrets are present (values are never returned)."""
    cfg = get_config()
    return {
        "client_id": bool(_read(cfg.md_client_id_file)),
        "client_secret": bool(_read(cfg.md_client_secret_file)),
        "username": bool(_read(cfg.md_username_file)),
        "password": bool(_read(cfg.md_password_file)),
    }


def load_credentials() -> AccountCredentials | None:
    cfg = get_config()
    values = [_read(cfg.md_client_id_file), _read(cfg.md_client_secret_file),
              _read(cfg.md_username_file), _read(cfg.md_password_file)]
    if not all(values):
        return None
    return AccountCredentials(*values)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class MangaDexAccount:
    def __init__(self, client: MangaDexClient, creds: AccountCredentials, redis=None):
        self.client = client
        self.creds = creds
        self.redis = redis if redis is not None else client.cache

    # ------------------------------------------------------------------ tokens
    def _load_token(self) -> dict | None:
        if self.redis is None:
            return getattr(self, "_mem_token", None)
        raw = self.redis.get(TOKEN_KEY)
        if not raw:
            return None
        try:
            tok = json.loads(raw)
        except ValueError:
            return None
        return tok if tok.get("fp") == self.creds.fingerprint else None

    def _store_token(self, data: dict) -> dict:
        now = time.time()
        tok = {
            "fp": self.creds.fingerprint,
            "access": data["access_token"],
            "access_exp": now + float(data.get("expires_in") or 900),
            "refresh": data.get("refresh_token"),
            "refresh_exp": now + float(data.get("refresh_expires_in") or 0) if data.get("refresh_expires_in") else None,
        }
        if self.redis is None:
            self._mem_token = tok
        else:
            ttl = int(max(tok["access_exp"], tok["refresh_exp"] or 0) - now) + 60
            self.redis.set(TOKEN_KEY, json.dumps(tok), ex=max(ttl, 120))
        return tok

    def invalidate(self) -> None:
        if self.redis is None:
            self._mem_token = None
        else:
            self.redis.delete(TOKEN_KEY)

    def _grant(self, tok: dict | None) -> dict:
        base = {"client_id": self.creds.client_id, "client_secret": self.creds.client_secret}
        now = time.time()
        if tok and tok.get("refresh") and (tok.get("refresh_exp") is None or tok["refresh_exp"] - EXPIRY_MARGIN_S > now):
            try:
                return self._store_token(self.client.token_request(
                    {**base, "grant_type": "refresh_token", "refresh_token": tok["refresh"]}))
            except AccountAuthError:
                log.info("md_account_refresh_rejected_relogin")
        data = self.client.token_request({**base, "grant_type": "password", "username": self.creds.username,
                                          "password": self.creds.password})
        log.info("md_account_logged_in")
        return self._store_token(data)

    def access_token(self, force: bool = False) -> str:
        tok = None if force else self._load_token()
        if tok and tok["access_exp"] - EXPIRY_MARGIN_S > time.time():
            return tok["access"]
        # One process refreshes; the others wait briefly and reuse its token.
        got_lock = True
        if self.redis is not None:
            got_lock = bool(self.redis.set(TOKEN_LOCK_KEY, b"1", nx=True, ex=30))
            if not got_lock:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    time.sleep(0.5)
                    tok = self._load_token()
                    if tok and tok["access_exp"] - EXPIRY_MARGIN_S > time.time():
                        return tok["access"]
        try:
            return self._grant(tok or self._load_token())["access"]
        finally:
            if got_lock and self.redis is not None:
                self.redis.delete(TOKEN_LOCK_KEY)

    # ------------------------------------------------------------------ requests
    def get(self, path: str, params=None) -> dict[str, Any]:
        """Authenticated GET; a rejected token is renewed once before giving up."""
        try:
            return self.client.api_get(path, params, bearer=self.access_token())
        except AccountAuthError as exc:
            if exc.status != 401:
                raise
            self.invalidate()
            return self.client.api_get(path, params, bearer=self.access_token(force=True))

    def me(self) -> dict[str, Any]:
        data = self.get("/user/me").get("data") or {}
        attrs = data.get("attributes") or {}
        return {"id": data.get("id"), "username": attrs.get("username")}

    def _paged(self, path: str, base: list[tuple[str, Any]], page_size: int) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            data = self.get(path, [("limit", page_size), ("offset", offset), *base])
            items = data.get("data") or []
            if not isinstance(items, list):
                raise InvalidResponseError(f"{path}: response has no data list")
            yield from items
            total = int(data.get("total") or 0)
            offset += len(items)
            if not items or offset >= total:
                return
            if offset + page_size > MAX_OFFSET_WINDOW:
                log.warning("md_account_offset_window_reached", path=path, total=total)
                return

    def iter_follows(self) -> Iterator[dict[str, Any]]:
        """Every manga the account follows (MangaDex manga entities with cover/author)."""
        params = [("includes[]", "cover_art"), ("includes[]", "author"), ("includes[]", "artist")]
        yield from self._paged("/user/follows/manga", params, FOLLOWS_PAGE_SIZE)

    def reading_statuses(self) -> dict[str, str]:
        """``{manga_id: status}`` (reading, plan_to_read, completed, on_hold, dropped, re_reading)."""
        data = self.get("/manga/status")
        statuses = data.get("statuses") or {}
        return {str(k): str(v) for k, v in statuses.items() if v} if isinstance(statuses, dict) else {}

    def iter_follows_feed(self, since: datetime, languages: list[str],
                          content_ratings: list[str]) -> Iterator[dict[str, Any]]:
        """Chapters of all followed manga updated since ``since`` (oldest first)."""
        params: list[tuple[str, Any]] = [
            ("includes[]", "scanlation_group"),
            ("order[updatedAt]", "asc"),
            ("updatedAtSince", _fmt(since)),
            ("includeFuturePublishAt", 0),
        ]
        params += [("translatedLanguage[]", lang) for lang in sorted(set(languages))]
        params += [("contentRating[]", r) for r in content_ratings]
        yield from self._paged("/user/follows/manga/feed", params, FEED_PAGE_SIZE)


def get_account(client: MangaDexClient) -> MangaDexAccount:
    creds = load_credentials()
    if creds is None:
        raise AccountNotConfigured("MangaDex account is not configured (Docker secrets md_client_id, "
                                   "md_client_secret, md_username, md_password)")
    return MangaDexAccount(client, creds)
