"""Convert MangaDex JSON entities into plain Python structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _localized(d: Any, prefer: tuple[str, ...] = ("en",)) -> str | None:
    if not isinstance(d, dict) or not d:
        return None
    for lang in prefer:
        if d.get(lang):
            return str(d[lang])
    for v in d.values():
        if v:
            return str(v)
    return None


def canonical_title(attributes: dict[str, Any]) -> str:
    """The manga's main MangaDex title (the ``title`` field, not alt titles)."""
    title = attributes.get("title") or {}
    if isinstance(title, dict) and title:
        # MangaDex stores exactly one entry in ``title``; that entry is canonical.
        first = next((v for v in title.values() if v), None)
        if first:
            return str(first)
    return _localized(title) or "Untitled"


@dataclass
class ParsedManga:
    id: str
    title: str
    alt_titles: list[dict[str, str]]
    description: str | None
    status: str | None
    year: int | None
    content_rating: str | None
    original_language: str | None
    last_volume: str | None
    last_chapter: str | None
    tags: list[str]
    authors: list[str]
    available_languages: list[str]
    cover_filename: str | None
    raw: dict[str, Any] = field(repr=False)


def parse_manga(data: dict[str, Any]) -> ParsedManga:
    attrs = data.get("attributes") or {}
    rels = data.get("relationships") or []
    cover = next((r for r in rels if r.get("type") == "cover_art"), None)
    cover_filename = ((cover or {}).get("attributes") or {}).get("fileName")
    authors: list[str] = []
    for r in rels:
        if r.get("type") in {"author", "artist"}:
            name = (r.get("attributes") or {}).get("name")
            if name and name not in authors:
                authors.append(name)
    tags = []
    for t in attrs.get("tags") or []:
        name = _localized((t.get("attributes") or {}).get("name"))
        if name:
            tags.append(name)
    alt_titles = [a for a in (attrs.get("altTitles") or []) if isinstance(a, dict)]
    return ParsedManga(
        id=str(data["id"]),
        title=canonical_title(attrs),
        alt_titles=alt_titles,
        description=_localized(attrs.get("description")),
        status=attrs.get("status"),
        year=attrs.get("year") if isinstance(attrs.get("year"), int) else None,
        content_rating=attrs.get("contentRating"),
        original_language=attrs.get("originalLanguage"),
        last_volume=attrs.get("lastVolume") or None,
        last_chapter=attrs.get("lastChapter") or None,
        tags=tags,
        authors=authors,
        available_languages=[x for x in (attrs.get("availableTranslatedLanguages") or []) if x],
        cover_filename=cover_filename,
        raw={"attributes": attrs},
    )


@dataclass
class ParsedGroup:
    id: str
    name: str
    website: str | None = None
    official: bool = False


@dataclass
class ParsedChapter:
    id: str
    manga_id: str | None
    language: str
    volume: str | None
    chapter: str | None
    title: str | None
    pages: int
    version: int | None
    external_url: str | None
    publish_at: datetime | None
    readable_at: datetime | None
    updated_at: datetime | None
    groups: list[ParsedGroup]
    raw: dict[str, Any] = field(repr=False)
    # Removed from MangaDex (e.g. DMCA); still listed with includeUnavailable=1.
    unavailable: bool = False

    @property
    def is_external(self) -> bool:
        return bool(self.external_url) and self.pages <= 1


def _clean_num(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_chapter(data: dict[str, Any]) -> ParsedChapter:
    attrs = data.get("attributes") or {}
    rels = data.get("relationships") or []
    groups: list[ParsedGroup] = []
    manga_id = None
    for r in rels:
        if r.get("type") == "scanlation_group":
            ga = r.get("attributes") or {}
            groups.append(
                ParsedGroup(
                    id=str(r["id"]),
                    name=(ga.get("name") or "").strip() or f"Group {str(r['id'])[:8]}",
                    website=ga.get("website"),
                    official=bool(ga.get("official")),
                )
            )
        elif r.get("type") == "manga":
            manga_id = str(r.get("id"))
    title = attrs.get("title")
    return ParsedChapter(
        id=str(data["id"]),
        manga_id=manga_id,
        language=str(attrs.get("translatedLanguage") or "unknown"),
        volume=_clean_num(attrs.get("volume")),
        chapter=_clean_num(attrs.get("chapter")),
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        pages=int(attrs.get("pages") or 0),
        version=attrs.get("version"),
        external_url=attrs.get("externalUrl") or None,
        publish_at=_dt(attrs.get("publishAt")),
        readable_at=_dt(attrs.get("readableAt")),
        updated_at=_dt(attrs.get("updatedAt")),
        groups=groups,
        unavailable=bool(attrs.get("isUnavailable")),
        raw={"attributes": attrs, "relationships": [{"id": r.get("id"), "type": r.get("type")} for r in rels]},
    )
