"""Parsing of MangaDex URLs and UUIDs pasted by the user."""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ALLOWED_HOSTS = {"mangadex.org", "www.mangadex.org", "canary.mangadex.dev"}


class InvalidMangaReference(ValueError):
    pass


def parse_manga_reference(value: str) -> uuid.UUID:
    """Return the manga UUID from a MangaDex title URL or a bare UUID.

    Accepted forms::

        8f3e1818-a015-491d-bd81-3addc4d7d56a
        https://mangadex.org/title/8f3e1818-a015-491d-bd81-3addc4d7d56a
        https://mangadex.org/title/8f3e1818-a015-491d-bd81-3addc4d7d56a/some-slug?tab=art
        mangadex.org/manga/8f3e1818-...   (legacy path)

    Chapter URLs (``/chapter/<uuid>``) are rejected because the UUID would refer
    to a chapter, not a manga.
    """
    if not value or not value.strip():
        raise InvalidMangaReference("Empty manga reference")
    value = value.strip()

    if _UUID_RE.fullmatch(value):
        return uuid.UUID(value)

    candidate = value if "://" in value else f"https://{value}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise InvalidMangaReference("Not a MangaDex URL or UUID")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in {"title", "manga"} and _UUID_RE.fullmatch(parts[1]):
        return uuid.UUID(parts[1])
    if parts and parts[0] == "chapter":
        raise InvalidMangaReference("This is a chapter URL; paste the manga (title) URL instead")
    raise InvalidMangaReference("Could not find a manga UUID in the URL")


def looks_like_reference(value: str) -> bool:
    try:
        parse_manga_reference(value)
        return True
    except InvalidMangaReference:
        return False
