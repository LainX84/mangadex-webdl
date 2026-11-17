"""MangaDex API client.

Every request is funnelled through :mod:`app.ratelimit`, so the whole deployment
respects one shared budget. Only documented API endpoints are used:

* ``GET /manga`` (search), ``GET /manga/{id}``, ``GET /manga/{id}/feed``
* ``GET /chapter/{id}``, ``GET /group/{id}``
* ``GET /at-home/server/{chapterId}`` and the returned image URLs
* ``POST https://api.mangadex.network/report`` (MangaDex@Home node health reports)
* cover images from ``uploads.mangadex.org``
* with a configured account (``app.mangadex.account``): the OAuth token endpoint on
  ``auth.mangadex.org``, ``GET /user/follows/manga``, ``GET /user/follows/manga/feed``,
  ``GET /manga/status`` and ``GET /user/me`` (read-only)
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import get_config
from app.logging_setup import get_logger
from app.mangadex.errors import (
    AccessDeniedError,
    AccountAuthError,
    BadRequestError,
    InvalidResponseError,
    NetworkError,
    NotFoundError,
    MaintenanceError,
    RateLimitedError,
    ServerError,
)
from app.ratelimit import BucketConfig, RateLimiter
from app.settings_service import AppSettings

log = get_logger(__name__)

FEED_PAGE_SIZE = 500
MAX_OFFSET_WINDOW = 10_000
MAX_IMAGE_BYTES = 60 * 1024 * 1024
API_HOST_CONCURRENCY = 2


@dataclass
class AtHomeServer:
    base_url: str
    hash: str
    data: list[str]
    data_saver: list[str]
    fetched_at: float

    def page_url(self, filename: str, data_saver: bool = False) -> str:
        folder = "data-saver" if data_saver else "data"
        return f"{self.base_url}/{folder}/{self.hash}/{filename}"

    def files(self, data_saver: bool = False) -> list[str]:
        return self.data_saver if data_saver else self.data


@dataclass
class ImageResponse:
    content: bytes
    content_type: str | None
    content_length: int | None
    duration_ms: int
    cached: bool
    url: str


def parse_retry_after(headers: httpx.Headers, now: float | None = None) -> float | None:
    """Seconds to wait according to ``Retry-After`` / ``X-RateLimit-Retry-After``."""
    now = now if now is not None else time.time()
    value = headers.get("Retry-After")
    if value:
        value = value.strip()
        if value.isdigit():
            return float(value)
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return max(0.0, dt.timestamp() - now)
        except (TypeError, ValueError):
            pass
    md = headers.get("X-RateLimit-Retry-After")
    if md and md.strip().isdigit():
        ts = float(md.strip())
        # MangaDex sends an absolute unix timestamp; tolerate a relative value too.
        return max(0.0, ts - now) if ts > 1_000_000_000 else ts
    return None


class MangaDexClient:
    def __init__(
        self,
        limiter: RateLimiter,
        settings_provider: Callable[[], AppSettings],
        http: httpx.Client | None = None,
        on_access_blocked: Callable[[dict], None] | None = None,
        cache=None,
    ):
        cfg = get_config()
        self.cfg = cfg
        self.limiter = limiter
        self.settings = settings_provider
        self.cache = cache
        self.on_access_blocked = on_access_blocked
        self.api_host = urlparse(cfg.mangadex_api_url).hostname or "api.mangadex.org"
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(cfg.http_timeout_seconds, connect=10.0),
            headers={"User-Agent": cfg.user_agent, "Accept": "application/json"},
            follow_redirects=False,
        )
        self.requests_made = 0
        self._report_queue: queue.Queue | None = None
        self._report_lock = threading.Lock()
        self._report_off_until = 0.0

    # ------------------------------------------------------------------ core
    def _bucket(self, name: str) -> BucketConfig:
        s = self.settings()
        if name == "api":
            return BucketConfig(s.api_requests_per_second, s.api_min_interval_ms)
        if name == "at_home":
            return BucketConfig(s.at_home_requests_per_minute / 60.0, s.api_min_interval_ms, burst=1)
        if name in {"image", "cover"}:
            return BucketConfig(s.image_requests_per_second, s.image_min_interval_ms)
        if name == "report":
            return BucketConfig(5.0, 0)
        raise ValueError(name)

    def _raise_for_status(self, resp: httpx.Response, url: str, is_api: bool, account: str | None = None) -> None:
        status = resp.status_code
        if status < 400:
            return
        if account and (status in (401, 403) or (status == 400 and account == "token")):
            # Account requests (token endpoint, /user/*): a rejection means the login or
            # the token is wrong, not that this IP is restricted. Never trigger the global stop.
            raise AccountAuthError(f"MangaDex account request rejected (HTTP {status}): {_error_detail(resp)}",
                                   status=status, url=url)
        if status == 429:
            retry_after = parse_retry_after(resp.headers)
            s = self.settings()
            self.limiter.register_rate_limit(
                retry_after, s.rate_limit_base_cooldown_seconds, s.rate_limit_max_cooldown_seconds,
                reason=f"HTTP 429 from {urlparse(url).hostname}",
            )
            raise RateLimitedError("Rate limited by MangaDex (HTTP 429)", retry_after=retry_after, status=status, url=url)
        if status in (401, 403):
            msg = f"Access restricted by MangaDex (HTTP {status})"
            if is_api:
                detail = _error_detail(resp)
                info = {
                    "status": status,
                    "message": msg + (f": {detail}" if detail else ""),
                    "url": _strip_query(url),
                    "at": datetime.now(UTC).isoformat(),
                }
                self.limiter.set_block(info)
                if self.on_access_blocked:
                    try:
                        self.on_access_blocked(info)
                    except Exception:  # pragma: no cover - persistence is best effort
                        log.exception("access_block_persist_failed")
                log.error("mangadex_access_blocked", status=status)
            raise AccessDeniedError(msg, status=status, url=url)
        if status == 404 or status == 410:
            raise NotFoundError(f"Not found on MangaDex (HTTP {status})", status=status, url=url)
        if status >= 500 and _is_maintenance_page(resp, url):
            until = self.limiter.set_maintenance(MAINTENANCE_RECHECK_SECONDS)
            raise MaintenanceError("MangaDex is down for maintenance", until_ms=until, status=status, url=url)
        if status >= 500:
            raise ServerError(f"MangaDex server error (HTTP {status})", status=status, url=url)
        raise BadRequestError(f"Request rejected by MangaDex (HTTP {status}): {_error_detail(resp)}", status=status, url=url)

    def _proactive_throttle(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.strip() == "0":
            wait = parse_retry_after(httpx.Headers({"X-RateLimit-Retry-After": resp.headers.get("X-RateLimit-Retry-After", "")}))
            if wait and wait > 0:
                self.limiter.set_cooldown(min(wait, 3600), "MangaDex rate-limit budget exhausted")

    def _send(self, method: str, url: str, bucket: str, *, params=None, json_body=None, host_limit: int | None = None,
              headers: dict | None = None, form: dict | None = None, account: str | None = None) -> httpx.Response:
        host = urlparse(url).hostname or ""
        is_api = host == self.api_host
        self.limiter.acquire(bucket, self._bucket(bucket))
        limit = host_limit or (API_HOST_CONCURRENCY if is_api else self.settings().max_concurrent_pages)
        with self.limiter.slot(f"host:{host}", limit, ttl_s=self.cfg.http_timeout_seconds * 4):
            self.requests_made += 1
            try:
                resp = self.http.request(method, url, params=params, json=json_body, data=form, headers=headers)
            except httpx.TimeoutException as exc:
                raise NetworkError(f"Timeout contacting {host}", url=url) from exc
            except httpx.TransportError as exc:
                raise NetworkError(f"Network error contacting {host}: {exc.__class__.__name__}", url=url) from exc
        if is_api:
            self._proactive_throttle(resp)
        self._raise_for_status(resp, url, is_api, account=account)
        return resp

    def api_get(self, path: str, params: dict | list | None = None, cache_ttl: int | None = None,
                bearer: str | None = None) -> dict[str, Any]:
        """GET an API path. With ``bearer`` the request is an account request (never cached)."""
        url = f"{self.cfg.mangadex_api_url}{path}"
        cache_key = None
        if bearer:
            resp = self._send("GET", url, "api", params=params, headers={"Authorization": f"Bearer {bearer}"},
                              account="api")
            return self._json(resp, url)
        if cache_ttl and self.cache is not None:
            digest = hashlib.sha256(json.dumps([path, _norm_params(params)], sort_keys=True).encode()).hexdigest()
            cache_key = f"mdm:cache:{digest}"
            hit = self._cache_get_or_lock(cache_key)
            if hit is not None:
                return hit
        try:
            resp = self._send("GET", url, "api", params=params)
            try:
                data = resp.json()
            except ValueError as exc:
                raise InvalidResponseError("MangaDex returned invalid JSON", url=url) from exc
            if not isinstance(data, dict) or data.get("result") == "error":
                raise InvalidResponseError(f"Unexpected MangaDex response: {_error_detail_json(data)}", url=url)
            if cache_key:
                self.cache.set(cache_key, json.dumps(data), ex=cache_ttl)
            return data
        finally:
            if cache_key:
                self.cache.delete(cache_key + ":lock")

    @staticmethod
    def _json(resp: httpx.Response, url: str) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise InvalidResponseError("MangaDex returned invalid JSON", url=url) from exc
        if not isinstance(data, dict) or data.get("result") == "error":
            raise InvalidResponseError(f"Unexpected MangaDex response: {_error_detail_json(data)}", url=url)
        return data

    def token_request(self, form: dict[str, str]) -> dict[str, Any]:
        """POST to the MangaDex OAuth token endpoint (personal client password/refresh grant)."""
        url = self.cfg.mangadex_auth_url
        resp = self._send("POST", url, "api", form=form, host_limit=1, account="token")
        try:
            data = resp.json()
        except ValueError as exc:
            raise InvalidResponseError("MangaDex auth returned invalid JSON", url=url) from exc
        if not isinstance(data, dict) or not data.get("access_token"):
            raise AccountAuthError("MangaDex auth response has no access token", url=url)
        return data

    def _cache_get_or_lock(self, key: str) -> dict | None:
        """Return cached JSON, or take the fetch lock (request de-duplication)."""
        deadline = time.monotonic() + 30
        while True:
            raw = self.cache.get(key)
            if raw:
                return json.loads(raw)
            if self.cache.set(key + ":lock", b"1", nx=True, ex=30):
                return None
            if time.monotonic() > deadline:
                return None
            time.sleep(0.25)

    # ------------------------------------------------------------------ API calls
    def get_manga(self, manga_id: str, use_cache: bool = True) -> dict[str, Any]:
        params = [("includes[]", "cover_art"), ("includes[]", "author"), ("includes[]", "artist")]
        return self.api_get(f"/manga/{manga_id}", params, cache_ttl=3600 if use_cache else None)["data"]

    def search_manga(self, title: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        s = self.settings()
        params: list[tuple[str, Any]] = [
            ("title", title),
            ("limit", min(max(1, limit), 50)),
            ("offset", max(0, offset)),
            ("includes[]", "cover_art"),
            ("order[relevance]", "desc"),
        ]
        params += [("contentRating[]", r) for r in s.content_ratings]
        return self.api_get("/manga", params, cache_ttl=600)

    def iter_manga_feed(
        self,
        manga_id: str,
        languages: list[str],
        updated_since: datetime | None = None,
        cache_ttl: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        s = self.settings()
        offset = 0
        while True:
            params: list[tuple[str, Any]] = [
                ("limit", FEED_PAGE_SIZE),
                ("offset", offset),
                ("includes[]", "scanlation_group"),
                ("order[volume]", "asc"),
                ("order[chapter]", "asc"),
                ("includeFuturePublishAt", 0),
                # Also list chapters removed from MangaDex (DMCA etc.), flagged isUnavailable:
                # they are history (never downloadable) but count for tiers and completion.
                ("includeUnavailable", 1),
                # NB: do NOT send includeExternalUrl: MangaDex treats it as a filter
                # (1 = only external chapters, 0 = only hosted ones). Omitting it
                # returns both, and external chapters are flagged locally.
            ]
            params += [("translatedLanguage[]", lang) for lang in sorted(languages)]
            params += [("contentRating[]", r) for r in s.content_ratings]
            if updated_since:
                params.append(("updatedAtSince", updated_since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")))
            data = self.api_get(f"/manga/{manga_id}/feed", params, cache_ttl=cache_ttl)
            items = data.get("data") or []
            if not isinstance(items, list):
                raise InvalidResponseError("Feed response has no data list")
            yield from items
            total = int(data.get("total") or 0)
            offset += len(items)
            if not items or offset >= total:
                return
            if offset + FEED_PAGE_SIZE > MAX_OFFSET_WINDOW:
                log.warning("feed_offset_window_reached", manga_id=manga_id, total=total)
                return

    def get_chapter(self, chapter_id: str) -> dict[str, Any]:
        params = [("includes[]", "scanlation_group")]
        return self.api_get(f"/chapter/{chapter_id}", params)["data"]

    def get_group(self, group_id: str) -> dict[str, Any]:
        return self.api_get(f"/group/{group_id}", None, cache_ttl=24 * 3600)["data"]

    def get_at_home(self, chapter_id: str) -> AtHomeServer:
        url = f"{self.cfg.mangadex_api_url}/at-home/server/{chapter_id}"
        # Counts against both the at-home budget and the global API budget.
        self.limiter.acquire("at_home", self._bucket("at_home"))
        resp = self._send("GET", url, "api")
        try:
            data = resp.json()
            ch = data["chapter"]
            return AtHomeServer(
                base_url=str(data["baseUrl"]).rstrip("/"),
                hash=str(ch["hash"]),
                data=list(ch.get("data") or []),
                data_saver=list(ch.get("dataSaver") or []),
                fetched_at=time.time(),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidResponseError("Invalid at-home server response", url=url) from exc

    def download_image(self, url: str, bucket: str = "image") -> ImageResponse:
        started = time.monotonic()
        resp = self._send("GET", url, bucket)
        content = resp.content
        if len(content) > MAX_IMAGE_BYTES:
            raise InvalidResponseError("Image larger than allowed maximum", url=url)
        length = resp.headers.get("Content-Length")
        return ImageResponse(
            content=content,
            content_type=resp.headers.get("Content-Type"),
            content_length=int(length) if length and length.isdigit() else None,
            duration_ms=int((time.monotonic() - started) * 1000),
            cached=resp.headers.get("X-Cache", "").upper().startswith("HIT"),
            url=url,
        )

    def report(self, url: str, success: bool, num_bytes: int, duration_ms: int, cached: bool) -> None:
        """Report MangaDex@Home node health, as required by the at-home docs.

        Fire-and-forget: reports are sent by a background thread so a slow or
        unreachable report endpoint never delays downloads. After a failure,
        reporting pauses for 10 minutes.
        """
        if not self.settings().report_to_mangadex_at_home:
            return
        host = urlparse(url).hostname or ""
        if host.endswith("mangadex.org") or time.time() < self._report_off_until:
            return  # reports are only for MD@Home nodes
        with self._report_lock:
            if self._report_queue is None:
                self._report_queue = queue.Queue(maxsize=200)
                threading.Thread(target=self._report_loop, name="md-report", daemon=True).start()
        try:
            self._report_queue.put_nowait(
                {"url": url, "success": success, "bytes": num_bytes, "duration": duration_ms, "cached": cached}
            )
        except queue.Full:
            pass

    def _report_loop(self) -> None:
        assert self._report_queue is not None
        while True:
            payload = self._report_queue.get()
            if time.time() < self._report_off_until:
                continue
            try:
                self.limiter.acquire("report", self._bucket("report"), max_wait_s=10)
                self.http.post(self.cfg.mangadex_report_url, json=payload, timeout=5)
            except Exception:  # never affect downloads; back off instead
                self._report_off_until = time.time() + 600
                log.info("md_at_home_report_unavailable", retry_in_s=600)

    def cover_url(self, manga_id: str, filename: str, size: int = 256) -> str:
        return f"{self.cfg.mangadex_uploads_url}/covers/{manga_id}/{filename}.{size}.jpg"


def _strip_query(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _norm_params(params) -> list:
    if params is None:
        return []
    items = params.items() if isinstance(params, dict) else params
    return sorted([str(k), str(v)] for k, v in items)


def _error_detail(resp: httpx.Response) -> str:
    try:
        return _error_detail_json(resp.json())
    except ValueError:
        return ""


def _error_detail_json(data: Any) -> str:
    if isinstance(data, dict) and (data.get("error_description") or isinstance(data.get("error"), str)):
        # OAuth (Keycloak) error format of auth.mangadex.org
        return str(data.get("error_description") or data.get("error"))[:300]
    if isinstance(data, dict):
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            e = errs[0] if isinstance(errs[0], dict) else {}
            return str(e.get("detail") or e.get("title") or "")[:300]
    return ""


# How long to pause all MangaDex traffic after seeing the maintenance page before checking again.
MAINTENANCE_RECHECK_SECONDS = 300


def _is_maintenance_page(resp: httpx.Response, url: str) -> bool:
    """True when a *.mangadex.org host answered with the "Down for maintenance" HTML page.

    MD@Home nodes are excluded: a single broken node must not pause everything.
    """
    host = (urlparse(url).hostname or "").lower()
    if not (host == "mangadex.org" or host.endswith(".mangadex.org")):
        return False
    if resp.status_code not in (502, 503, 504):
        return False
    if "html" not in resp.headers.get("Content-Type", "").lower():
        return False
    try:
        body = resp.text[:20000].lower()
    except Exception:  # noqa: BLE001 - unread/undecodable body: not detectable
        return False
    return "maintenance" in body
