"""Test helpers: an in-memory MangaDex served through the real HTTP client, plus shortcuts.

``FakeMangaDex`` answers the MangaDex API, the MD@Home image nodes, covers and the health
report through an ``httpx.MockTransport``, so every test exercises the real
``MangaDexClient`` (parsing, error handling, caching) without network access. Only the
token-bucket waits are skipped (``FastLimiter``); slots, cooldowns and blocks are real.
"""

from __future__ import annotations

import io
import itertools
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import get_config
from app.ratelimit import RateLimiter

GROUP_A = ("aaaaaaaa-0000-4000-8000-000000000001", "Team X")
GROUP_B = ("aaaaaaaa-0000-4000-8000-000000000002", "Team Y")
GROUP_C = ("aaaaaaaa-0000-4000-8000-000000000003", "Team Z")

NODE = "https://node.test"
_BASE_TIME = datetime(2024, 1, 1, tzinfo=UTC)


def _jpeg(seed: int = 0) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), ((seed * 37) % 256, (seed * 91) % 256, 120)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


class FastLimiter(RateLimiter):
    """Real limiter (Redis) without the request-rate waits."""

    def acquire(self, bucket, cfg, max_wait_s: float = 300.0) -> float:  # noqa: ARG002
        self.check_gates()
        return 0.0


class FakeMangaDex:
    def __init__(self) -> None:
        self.mangas: dict[str, dict[str, Any]] = {}
        self.chapters: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, str] = {}
        self.requests: list[str] = []
        self.fail: dict[str, int] = {}  # path regex -> HTTP status to answer instead
        self._seq = itertools.count(1)
        self._client = None
        self.api = urlparse(get_config().mangadex_api_url).hostname

    # ---------------------------------------------------------------- data
    def add_manga(self, title: str = "Test Manga", **attrs) -> str:
        mid = str(uuid.uuid4())
        self.mangas[mid] = {
            "id": mid,
            "type": "manga",
            "attributes": {
                "title": {"en": title},
                "altTitles": [],
                "description": {"en": f"About {title}"},
                "originalLanguage": "ja",
                "status": "ongoing",
                "contentRating": "safe",
                "publicationDemographic": None,
                "tags": [],
                "year": 2020,
                "lastChapter": None,
                "lastVolume": None,
                "updatedAt": _BASE_TIME.isoformat(),
                **attrs,
            },
            "relationships": [
                {"id": str(uuid.uuid4()), "type": "cover_art", "attributes": {"fileName": "cover.jpg"}},
            ],
        }
        return mid

    def add_chapter(self, mid: str, volume: str | None = None, chapter: str | None = "1", title: str | None = None,
                    lang: str = "en", groups=(GROUP_A,), pages: int = 3, external_url: str | None = None,
                    when: datetime | None = None, unavailable: bool = False) -> dict[str, Any]:
        n = next(self._seq)
        cid = str(uuid.uuid4())
        when = when or (_BASE_TIME + timedelta(hours=n))
        rels = [{"id": mid, "type": "manga"}]
        for gid, name in groups:
            self.groups[gid] = name
            rels.append({"id": gid, "type": "scanlation_group", "attributes": {"name": name}})
        ch = {
            "id": cid,
            "type": "chapter",
            "attributes": {
                "volume": volume, "chapter": chapter, "title": title, "translatedLanguage": lang,
                "pages": 0 if external_url else pages, "externalUrl": external_url, "isUnavailable": unavailable,
                "version": 1, "publishAt": when.isoformat(), "readableAt": when.isoformat(),
                "createdAt": when.isoformat(), "updatedAt": when.isoformat(),
            },
            "relationships": rels,
            "_files": [f"{i}-{cid[:8]}.jpg" for i in range(1, (0 if external_url else pages) + 1)],
        }
        self.chapters[cid] = ch
        return ch

    def revise(self, cid: str, pages: int | None = None) -> dict[str, Any]:
        """New MangaDex version of a chapter with a new page list (same chapter UUID)."""
        ch = self.chapters[cid]
        a = ch["attributes"]
        a["version"] = int(a["version"]) + 1
        if pages is not None:
            a["pages"] = pages
        stamp = next(self._seq)
        ch["_files"] = [f"{i}-v{a['version']}-{stamp}.jpg" for i in range(1, a["pages"] + 1)]
        a["updatedAt"] = datetime.now(UTC).isoformat()
        return ch

    # ---------------------------------------------------------------- client
    @property
    def client(self):
        if self._client is None:
            from app.redis_client import get_redis
            from app.services.control import _settings_provider, persist_access_block
            from app.mangadex.client import MangaDexClient

            self._client = MangaDexClient(
                limiter=FastLimiter(get_redis()),
                settings_provider=_settings_provider,
                http=httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=False),
                on_access_blocked=persist_access_block,
                cache=get_redis(),
            )
        return self._client

    # ---------------------------------------------------------------- HTTP
    @staticmethod
    def _ok(payload: dict, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"result": "ok", **payload})

    @staticmethod
    def _err(status: int, title: str) -> httpx.Response:
        return httpx.Response(status, json={"result": "error", "errors": [{"status": status, "title": title}]})

    def _public(self, ch: dict) -> dict:
        return {k: v for k, v in ch.items() if not k.startswith("_")}

    def _manga_out(self, mid: str) -> dict:
        m = dict(self.mangas[mid])
        attrs = dict(m["attributes"])
        attrs["availableTranslatedLanguages"] = sorted(
            {c["attributes"]["translatedLanguage"] for c in self.chapters.values()
             if any(r["type"] == "manga" and r["id"] == mid for r in c["relationships"])})
        m["attributes"] = attrs
        return m

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        self.requests.append(f"{request.method} {url.host}{path}")
        for pattern, status in self.fail.items():
            if re.search(pattern, path):
                return self._err(status, "forced failure")
        if url.host == urlparse(NODE).hostname:
            m = re.fullmatch(r"/(data|data-saver)/([^/]+)/(.+)", path)
            if not m:
                return httpx.Response(404)
            body = _jpeg(hash(m.group(3)) % 1000)
            return httpx.Response(200, content=body, headers={"Content-Type": "image/jpeg",
                                                              "Content-Length": str(len(body))})
        if url.host.endswith("uploads.mangadex.org"):
            body = _jpeg()
            return httpx.Response(200, content=body, headers={"Content-Type": "image/jpeg"})
        if url.host == "api.mangadex.network":
            return httpx.Response(200, json={})
        if url.host != self.api:
            return httpx.Response(404)
        params = url.params
        if m := re.fullmatch(r"/manga/([0-9a-f-]{36})", path):
            return self._ok({"data": self._manga_out(m.group(1))}) if m.group(1) in self.mangas else self._err(404, "Not found")
        if m := re.fullmatch(r"/manga/([0-9a-f-]{36})/feed", path):
            mid = m.group(1)
            if mid not in self.mangas:
                return self._err(404, "Not found")
            langs = set(params.get_list("translatedLanguage[]"))
            items = [self._public(c) for c in self.chapters.values()
                     if any(r["type"] == "manga" and r["id"] == mid for r in c["relationships"])
                     and (not langs or c["attributes"]["translatedLanguage"] in langs)]
            offset, limit = int(params.get("offset", 0)), int(params.get("limit", 100))
            return self._ok({"data": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit})
        if path == "/manga":
            q = (params.get("title") or "").lower()
            ids = set(params.get_list("ids[]"))
            hits = [self._manga_out(mid) for mid, mg in self.mangas.items()
                    if (ids and mid in ids) or (q and (q in mid or any(q in t.lower() for t in mg["attributes"]["title"].values())
                                                       or any(q in t.lower() for alt in mg["attributes"]["altTitles"] for t in alt.values())))]
            return self._ok({"data": hits, "total": len(hits), "offset": 0, "limit": 50})
        if m := re.fullmatch(r"/chapter/([0-9a-f-]{36})", path):
            ch = self.chapters.get(m.group(1))
            return self._ok({"data": self._public(ch)}) if ch else self._err(404, "Not found")
        if m := re.fullmatch(r"/group/([0-9a-f-]{36})", path):
            gid = m.group(1)
            if gid not in self.groups:
                return self._err(404, "Not found")
            return self._ok({"data": {"id": gid, "type": "scanlation_group", "attributes": {"name": self.groups[gid]}}})
        if m := re.fullmatch(r"/at-home/server/([0-9a-f-]{36})", path):
            ch = self.chapters.get(m.group(1))
            if ch is None or ch["attributes"]["isUnavailable"]:
                return self._err(404, "Not found")
            files = ch["_files"]
            return self._ok({"baseUrl": NODE, "chapter": {"hash": m.group(1).replace("-", ""), "data": files,
                                                          "dataSaver": files}})
        return self._err(404, f"Unhandled fake route {path}")


# -------------------------------------------------------------------- shortcuts


def add(md: FakeMangaDex, mid: str, languages=("en",), **kw):
    from app.db import session_scope
    from app.services.catalog import add_manga

    with session_scope() as s:
        add_manga(s, md.client, uuid.UUID(mid), list(languages), **kw)


def scan(md: FakeMangaDex, mid: str, trigger: str = "test") -> dict:
    from app.services.monitor import scan_manga

    return scan_manga(uuid.UUID(mid), trigger, client=md.client)


def releases(mid: str):
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import ChapterRelease

    with session_scope() as s:
        rows = list(s.scalars(select(ChapterRelease).where(ChapterRelease.manga_id == uuid.UUID(mid))))
        s.expunge_all()
    return sorted(rows, key=lambda r: (r.language_code, r.chapter or "", (r.group_names or [""])[0], str(r.id)))


def run_queue(md: FakeMangaDex, rounds: int = 200) -> int:
    """Dispatch and run download jobs in this process until the queue is empty."""
    from app.db import session_scope
    from app.services.downloader import run_download
    from app.services.jobs import dispatch_jobs

    done = 0
    for _ in range(rounds):
        picked: list[int] = []
        with session_scope() as s:
            dispatch_jobs(s, storage_ok=True, enqueue=lambda job_id, rq_id: picked.append(job_id))
        if not picked:
            return done
        for job_id in picked:
            run_download(job_id, client=md.client)
            done += 1
    raise AssertionError("run_queue did not settle")


def set_settings(**kw):
    from app.db import session_scope
    from app.settings_service import load_settings, save_settings

    with session_scope() as s:
        cur = load_settings(s, use_cache=False)
        save_settings(s, cur.model_copy(update=kw))
