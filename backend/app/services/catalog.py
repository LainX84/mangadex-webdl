"""Manga, language, scanlation group and release bookkeeping."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import GroupMode, LocalStatus
from app.languages import ensure_language
from app.library.naming import (
    chapter_kind,
    chapter_sort_number,
    logical_chapter_key,
    logical_sort_number,
    sanitize_component,
    with_collision_suffix,
)
from app.mangadex.client import MangaDexClient
from app.mangadex.parse import ParsedChapter, ParsedManga, parse_manga
from app.models import (
    ChapterRelease,
    ExternalChapter,
    GroupSubscription,
    Language,
    LogicalChapter,
    Manga,
    MangaLanguage,
    MonitoringConfig,
    ScanlationGroup,
)


class CatalogError(ValueError):
    pass


def unique_manga_folder(session: Session, title: str, manga_id: uuid.UUID, exclude_id: uuid.UUID | None = None) -> str:
    base = sanitize_component(title, fallback=f"Manga {str(manga_id)[:8]}")
    stmt = select(Manga.id).where(Manga.folder_name == base)
    if exclude_id:
        stmt = stmt.where(Manga.id != exclude_id)
    if session.scalar(stmt) is None:
        return base
    return with_collision_suffix(base, str(manga_id))


def apply_manga_metadata(manga: Manga, parsed: ParsedManga) -> None:
    manga.title = parsed.title
    manga.alt_titles = parsed.alt_titles
    manga.description = parsed.description
    manga.status = parsed.status
    manga.year = parsed.year
    manga.content_rating = parsed.content_rating
    manga.original_language = parsed.original_language
    manga.last_volume = parsed.last_volume
    manga.last_chapter = parsed.last_chapter
    manga.tags = parsed.tags
    manga.authors = parsed.authors
    manga.available_languages = parsed.available_languages
    manga.cover_filename = parsed.cover_filename
    manga.raw = parsed.raw
    manga.metadata_refreshed_at = datetime.now(UTC)


def validate_languages(session: Session, codes: Iterable[str]) -> list[str]:
    codes = sorted({c.strip() for c in codes if c and c.strip()})
    if not codes:
        raise CatalogError("Select at least one language")
    known = set(session.scalars(select(Language.code).where(Language.code.in_(codes))))
    unknown = [c for c in codes if c not in known]
    if unknown:
        raise CatalogError(f"Unknown language code(s): {', '.join(unknown)}")
    return codes


def add_manga(
    session: Session,
    client: MangaDexClient,
    manga_id: uuid.UUID,
    languages: list[str],
    group_mode: GroupMode = GroupMode.ALL,
    group_ids: list[uuid.UUID] | None = None,
    monitoring_enabled: bool = True,
    auto_download: bool = True,
    title_override: str | None = None,
    include_unknown_group: bool = True,
    start_chapter=None,
) -> Manga:
    if session.get(Manga, manga_id) is not None:
        raise CatalogError("This manga is already in the library")
    languages = validate_languages(session, languages)
    parsed = parse_manga(client.get_manga(str(manga_id), use_cache=False))
    manga = Manga(id=manga_id, title=parsed.title, title_override=(title_override or None), folder_name="",
                  added_via="manual")
    apply_manga_metadata(manga, parsed)
    manga.folder_name = unique_manga_folder(session, manga.display_title, manga_id)
    session.add(manga)
    session.flush()
    # Added again after being dismissed / removed: it is wanted now, so it is no longer "dismissed".
    from app.models import MangaDexFollow

    follow = session.get(MangaDexFollow, manga_id)
    if follow is not None and follow.dismissed_at is not None:
        follow.dismissed_at = None
    session.add(
        MonitoringConfig(
            manga_id=manga_id,
            monitoring_enabled=monitoring_enabled,
            auto_download=auto_download,
            group_mode=str(group_mode),
            include_unknown_group=include_unknown_group,
            start_chapter=start_chapter,
            start_chapter_set_at=datetime.now(UTC) if start_chapter is not None else None,
            next_scan_at=datetime.now(UTC),
            needs_full_scan=True,
        )
    )
    for code in languages:
        session.add(MangaLanguage(manga_id=manga_id, language_code=code))
    if group_mode == GroupMode.SELECTED:
        set_group_subscriptions(session, client, manga_id, group_ids or [])
    session.flush()
    return manga


def add_pending_manga(session: Session, parsed: ParsedManga, languages: list[str]) -> Manga:
    """Add a title from the MangaDex followed list, waiting for the user's approval.

    Monitoring and automatic downloads stay off and no scan is scheduled; the
    title only becomes active through :func:`approve_pending_manga`.
    """
    manga_id = uuid.UUID(parsed.id)
    known = set(session.scalars(select(Language.code).where(Language.code.in_(languages)))) if languages else set()
    langs = [c for c in languages if c in known] or ["en"]
    manga = Manga(id=manga_id, title=parsed.title, folder_name="", pending_approval=True,
                  pending_since=datetime.now(UTC), added_via="mangadex_follow")
    apply_manga_metadata(manga, parsed)
    manga.folder_name = unique_manga_folder(session, manga.display_title, manga_id)
    session.add(manga)
    session.flush()
    session.add(MonitoringConfig(manga_id=manga_id, monitoring_enabled=False, auto_download=False,
                                 group_mode=str(GroupMode.ALL), include_unknown_group=True,
                                 next_scan_at=None, needs_full_scan=True))
    for code in langs:
        session.add(MangaLanguage(manga_id=manga_id, language_code=code))
    session.flush()
    return manga


def approve_pending_manga(
    session: Session,
    client: MangaDexClient | None,
    manga: Manga,
    languages: list[str],
    group_mode: GroupMode = GroupMode.ALL,
    group_ids: list[uuid.UUID] | None = None,
    monitoring_enabled: bool = True,
    auto_download: bool = True,
    title_override: str | None = None,
    include_unknown_group: bool = True,
    start_chapter=None,
) -> Manga:
    """Activate a title that was waiting for approval, with the options the user picked."""
    if not manga.pending_approval:
        raise CatalogError("This manga is already in the library")
    update_preferences(
        session, client, manga, languages=languages, group_mode=group_mode,
        group_ids=list(group_ids or []) if group_mode == GroupMode.SELECTED else [],
        include_unknown_group=include_unknown_group, start_chapter=start_chapter,
        clear_start_chapter=start_chapter is None,
    )
    if client is not None:
        from app.mangadex.errors import MangaDexError

        try:  # fresh metadata (cover, status); the stored copy is fine if MangaDex is busy
            apply_manga_metadata(manga, parse_manga(client.get_manga(str(manga.id), use_cache=False)))
        except MangaDexError:
            pass
    manga.title_override = title_override or None
    # Nothing was ever written for a pending title, so the folder name can simply be re-derived.
    manga.folder_name = unique_manga_folder(session, manga.display_title, manga.id, exclude_id=manga.id)
    cfg = manga.monitoring
    cfg.monitoring_enabled = monitoring_enabled
    cfg.auto_download = auto_download
    cfg.needs_full_scan = True
    cfg.next_scan_at = datetime.now(UTC)
    manga.pending_approval = False
    manga.pending_since = None
    session.flush()
    return manga


OWNED_AT_ADD_SOURCE = "owned_at_add"


def mark_current_releases_owned(session: Session, client: MangaDexClient, manga: Manga) -> dict:
    """"New chapters only": treat every release MangaDex lists right now as already owned.

    Uses the same mechanism as the JAM import (``external_chapters``): those chapter
    numbers are never downloaded (whatever the group), ``start_chapter`` becomes the
    newest numbered chapter, and older or unnumbered releases are only wanted if
    published later (late extras such as x.5). Only releases published from now on
    are downloaded. Reads the feed for the manga's languages (cached by the add form).
    """
    from app.mangadex.parse import parse_chapter

    languages = manga_language_codes(session, manga.id)
    per_volume = manga.chapters_per_volume
    known = {r for r in session.scalars(select(ExternalChapter.release_id).where(ExternalChapter.manga_id == manga.id))}
    newest = None
    added = 0
    for item in client.iter_manga_feed(str(manga.id), languages, cache_ttl=600):
        ch = parse_chapter(item)
        rid = uuid.UUID(ch.id)
        num = logical_sort_number(ch.chapter, ch.volume, per_volume)
        if num is not None and not ch.is_external and (newest is None or num > newest):
            newest = num
        if rid in known:
            continue
        known.add(rid)
        session.add(ExternalChapter(manga_id=manga.id, source=OWNED_AT_ADD_SOURCE, release_id=rid,
                                    language_code=ch.language, chapter=ch.chapter,
                                    chapter_key=logical_chapter_key(ch.chapter, ch.id, ch.volume, per_volume)))
        added += 1
    manga.monitoring.start_chapter = newest
    manga.monitoring.start_chapter_set_at = datetime.now(UTC)
    # Releases already stored (e.g. from an earlier scan) follow the new ownership now.
    if added:
        session.flush()
        for rel in session.scalars(select(ChapterRelease).where(ChapterRelease.manga_id == manga.id,
                                                                ChapterRelease.local_status == LocalStatus.MISSING)):
            if rel.id in known:
                rel.local_status = LocalStatus.EXTERNAL
    session.flush()
    return {"owned": added, "after_chapter": newest}


OWNED_MANUAL_SOURCE = "owned_manual"


def mark_owned_up_to(session: Session, client: MangaDexClient, manga: Manga, up_to: float) -> dict:
    """"I have these already, up to chapter N": every chapter numbered <= ``up_to`` is owned elsewhere.

    The releases MangaDex lists now with such a number are recorded like "New chapters only";
    every whole number 1..N gets an owned key too, so a later upload of a chapter you already
    have is not downloaded. ``start_chapter`` becomes N (higher numbers stay wanted). Open
    downloads are left to the caller. Not for per-volume numbering (chapter numbers repeat).
    """
    from app.mangadex.parse import parse_chapter

    if manga.chapters_per_volume:
        raise CatalogError("This title numbers chapters per volume: use \"everything listed now\" instead")
    if up_to < 0 or up_to > 100_000:
        raise CatalogError("Chapter number out of range")
    known_rel = set(session.scalars(select(ExternalChapter.release_id).where(ExternalChapter.manga_id == manga.id)))
    keys = set(session.scalars(select(ExternalChapter.chapter_key).where(ExternalChapter.manga_id == manga.id)))
    owned_rel: set[uuid.UUID] = set()
    added = 0
    for item in client.iter_manga_feed(str(manga.id), manga_language_codes(session, manga.id), cache_ttl=600):
        ch = parse_chapter(item)
        n = logical_sort_number(ch.chapter, None, False)
        if n is None or n > up_to:
            continue
        rid = uuid.UUID(ch.id)
        owned_rel.add(rid)
        if rid in known_rel:
            continue
        key = logical_chapter_key(ch.chapter, ch.id, ch.volume, False)
        session.add(ExternalChapter(manga_id=manga.id, source=OWNED_AT_ADD_SOURCE, release_id=rid,
                                    language_code=ch.language, chapter=ch.chapter, chapter_key=key))
        known_rel.add(rid)
        keys.add(key)
        added += 1
    for i in range(1, int(up_to) + 1):  # numbers MangaDex does not list (yet) are owned too
        key = f"c:{i}"
        if key not in keys:
            session.add(ExternalChapter(manga_id=manga.id, source=OWNED_MANUAL_SOURCE, release_id=None,
                                        language_code=None, chapter=str(i), chapter_key=key))
            keys.add(key)
            added += 1
    session.flush()
    for rel in session.scalars(select(ChapterRelease).where(
            ChapterRelease.manga_id == manga.id,
            ChapterRelease.local_status.in_([LocalStatus.MISSING, LocalStatus.SKIPPED]))):
        n = logical_sort_number(rel.chapter, None, False)
        if rel.id in owned_rel or (n is not None and n <= up_to):
            rel.local_status = LocalStatus.EXTERNAL
    manga.monitoring.start_chapter = up_to
    manga.monitoring.start_chapter_set_at = datetime.now(UTC)
    session.flush()
    return {"owned": added, "after_chapter": up_to}


def ensure_group(session: Session, group_id: uuid.UUID, name: str | None = None, client: MangaDexClient | None = None,
                 website: str | None = None, official: bool = False) -> ScanlationGroup:
    group = session.get(ScanlationGroup, group_id)
    if group is None:
        if name is None and client is not None:
            data = client.get_group(str(group_id))
            attrs = data.get("attributes") or {}
            name = attrs.get("name")
            website = attrs.get("website")
            official = bool(attrs.get("official"))
        group = ScanlationGroup(id=group_id, name=name or f"Group {str(group_id)[:8]}", website=website,
                                is_official_publisher=official)
        session.add(group)
        session.flush()
    elif name and group.name != name:
        group.name = name
    return group


def set_group_subscriptions(session: Session, client: MangaDexClient | None, manga_id: uuid.UUID,
                            group_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    wanted = set(group_ids)
    current = {s.group_id: s for s in session.scalars(select(GroupSubscription).where(GroupSubscription.manga_id == manga_id))}
    for gid in wanted - current.keys():
        ensure_group(session, gid, client=client)
        session.add(GroupSubscription(manga_id=manga_id, group_id=gid))
    for gid in current.keys() - wanted:
        session.delete(current[gid])
    session.flush()
    return wanted


def manga_language_codes(session: Session, manga_id: uuid.UUID) -> list[str]:
    return list(session.scalars(select(MangaLanguage.language_code).where(MangaLanguage.manga_id == manga_id)))


def manga_group_ids(session: Session, manga_id: uuid.UUID) -> set[str]:
    return {str(g) for g in session.scalars(select(GroupSubscription.group_id).where(GroupSubscription.manga_id == manga_id))}


def update_preferences(
    session: Session,
    client: MangaDexClient | None,
    manga: Manga,
    *,
    languages: list[str] | None = None,
    group_mode: GroupMode | None = None,
    group_ids: list[uuid.UUID] | None = None,
    include_unknown_group: bool | None = None,
    start_chapter=None,
    clear_start_chapter: bool = False,
) -> bool:
    """Update language/group filters. Returns True if a rescan is needed.

    Changing filters never re-downloads anything: releases already on disk keep
    their local status; the follow-up scan only queues releases still missing.
    """
    cfg = manga.monitoring
    changed = False
    if languages is not None:
        languages = validate_languages(session, languages)
        current = set(manga_language_codes(session, manga.id))
        if set(languages) != current:
            for row in list(manga.languages):
                if row.language_code not in languages:
                    session.delete(row)
            for code in set(languages) - current:
                session.add(MangaLanguage(manga_id=manga.id, language_code=code))
            cfg.needs_full_scan = True
            changed = True
    if group_mode is not None and str(group_mode) != cfg.group_mode:
        cfg.group_mode = str(group_mode)
        changed = True
    if group_ids is not None:
        before = manga_group_ids(session, manga.id)
        after = set_group_subscriptions(session, client, manga.id, group_ids)
        if {str(g) for g in after} != before:
            changed = True
    if include_unknown_group is not None and include_unknown_group != cfg.include_unknown_group:
        cfg.include_unknown_group = include_unknown_group
        changed = True
    if clear_start_chapter and cfg.start_chapter is not None:
        cfg.start_chapter = None
        cfg.start_chapter_set_at = None
        changed = True
    elif start_chapter is not None and (cfg.start_chapter is None or float(cfg.start_chapter) != float(start_chapter)):
        cfg.start_chapter = start_chapter
        # Chapter numbers appearing on MangaDex from now on are wanted even below the start.
        cfg.start_chapter_set_at = datetime.now(UTC)
        changed = True
    session.flush()
    return changed


def release_wanted(
    release: ChapterRelease,
    languages: set[str],
    group_mode: str,
    group_ids: set[str],
    include_unknown_group: bool,
    start_chapter=None,
    owned_keys: frozenset[str] | set[str] | None = None,
    extras_since: datetime | None = None,
    excluded_groups: frozenset[str] | set[str] | None = None,
    *,
    ctx: "WantContext | None" = None,
) -> bool:
    """Does this release match the manga's download rules?

    * ``start_chapter``: chapters numbered >= this are wanted;
    * ``owned_keys``: chapter numbers already owned elsewhere are never wanted;
    * ``extras_since``: when set, releases below ``start_chapter`` (or without a
      number) are wanted only if published after this moment (late extras such
      as x.5, re-uploads). When not set, unnumbered releases are always wanted;
    * ``ctx.late_after`` (plain start chapter, nothing owned elsewhere): a release
      numbered below the start is still wanted when its chapter number is *new*,
      i.e. no release of that number in that language was published before the
      start was set (late extras, chapters skipped and uploaded later). A new
      upload of a number that already existed (another group, a re-upload of a
      removed chapter) is not: the user owns those chapters outside this app.

    ``ctx`` (from :func:`want_contexts`) replaces ``owned_keys`` / ``extras_since``.
    """
    late_after = first_published = None
    if ctx is not None:
        owned_keys, extras_since = ctx.owned_keys, ctx.extras_since
        late_after, first_published = ctx.late_after, ctx.first_published
    if release.language_code not in languages:
        return False
    if release.is_unavailable or release.is_external or release.pages <= 0:
        return False
    if in_excluded_group(release, excluded_groups):
        return False
    if owned_keys:
        lc = release.logical_chapter
        if lc is not None and (lc.chapter_key in owned_keys or plain_chapter_key(lc.chapter_key) in owned_keys):
            return False
    if start_chapter is not None or extras_since is not None:
        number = release_number(release)
        is_new = number is not None and start_chapter is not None and number >= float(start_chapter)
        if not is_new:
            if extras_since is not None:
                published = release.publish_at or release.readable_at
                if published is None or published <= extras_since:
                    return False
            elif number is not None and not is_new_chapter_number(release, late_after, first_published):
                return False
    if group_mode == GroupMode.ALL:
        return True
    if not release.group_ids:
        return include_unknown_group
    return any(g in group_ids for g in release.group_ids)


def _published(release) -> datetime | None:
    return release.publish_at or release.readable_at


def is_new_chapter_number(release, late_after: datetime | None, first_published) -> bool:
    """Was this release's chapter number first published (in its language) after ``late_after``?"""
    if late_after is None:
        return False
    own = _published(release)
    if own is None or own <= late_after:
        return False
    lc = release.logical_chapter
    if lc is None or not first_published:
        return True
    first = first_published.get((release.language_code, lc.chapter_key))
    return first is None or first > late_after


def plain_chapter_key(key: str) -> str:
    """"v15:c:2" -> "c:2". An owned chapter whose volume is unknown (its MangaDex
    release is gone) keeps the plain key and still covers that chapter number."""
    return key.split(":", 1)[1] if key.startswith("v") and ":" in key else key


def release_number(release) -> float | None:
    """Number compared with ``start_chapter``: the logical chapter's sort number
    (volume * 1000 + chapter for per-volume numbering), else the chapter number."""
    lc = release.logical_chapter
    sort = getattr(lc, "sort_number", None) if lc is not None else None
    if sort is not None:
        return float(sort)
    return chapter_sort_number(release.chapter)


@dataclass(frozen=True)
class ExternalContext:
    owned_keys: frozenset[str]
    extras_since: datetime | None


def external_contexts(session: Session, manga_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, ExternalContext]:
    """Owned-elsewhere chapter numbers and the "late extras" cut-off, per manga."""
    ids = list(manga_ids)
    if not ids:
        return {}
    keys: dict[uuid.UUID, set[str]] = defaultdict(set)
    created: dict[uuid.UUID, datetime] = {}
    for mid, key, ts in session.execute(
        select(ExternalChapter.manga_id, ExternalChapter.chapter_key, ExternalChapter.created_at)
        .where(ExternalChapter.manga_id.in_(ids))
    ):
        keys[mid].add(key)
        created[mid] = max(created.get(mid, ts), ts)
    if not keys:
        return {}
    newest = dict(
        session.execute(
            select(ChapterRelease.manga_id,
                   func.max(func.coalesce(ChapterRelease.publish_at, ChapterRelease.readable_at)))
            .where(ChapterRelease.manga_id.in_(list(keys)), ChapterRelease.local_status == LocalStatus.EXTERNAL)
            .group_by(ChapterRelease.manga_id)
        ).all()
    )
    return {mid: ExternalContext(frozenset(k), newest.get(mid) or created[mid]) for mid, k in keys.items()}


REPLACEABLE_STATUSES = {LocalStatus.DOWNLOADED, LocalStatus.IMPORTED, LocalStatus.MODIFIED}
# A re-upload must show up on MangaDex clearly after we had the older copy.
REUPLOAD_MARGIN = timedelta(minutes=10)


def same_group_reuploads(releases: Iterable[ChapterRelease], candidate=lambda r: True,
                         moved_out_since: datetime | None = None) -> list[tuple]:
    """(new, old) pairs: a newer upload of a chapter by the same group, replacing the copy we have.

    Same language, chapter number and (non-empty) group set; the old release is in the
    library, the new one is missing, was published later and only appeared on MangaDex
    after we had the old one (so long-standing duplicates are not treated as re-uploads).
    ``candidate`` filters the new releases (language/group rules). One new per old.
    ``moved_out_since``: an old release moved out of the library (*Folder deleted*) still counts
    when it was downloaded at or after this moment (its re-upload goes to the replaced folder).
    """
    buckets: dict[tuple, dict[str, list]] = defaultdict(lambda: {"old": [], "new": []})
    for r in releases:
        lc = r.logical_chapter
        if lc is None or lc.chapter_key.startswith("x:") or not r.group_ids:
            continue
        b = buckets[(r.language_code, lc.chapter_key, frozenset(r.group_ids))]
        if (r.local_status in REPLACEABLE_STATUSES and r.folder_rel_path) or moved_out_recently(r, moved_out_since):
            b["old"].append(r)
        elif (r.local_status == LocalStatus.MISSING and not r.is_unavailable and not r.is_external
              and (r.pages or 0) > 0 and _published(r) is not None and candidate(r)):
            b["new"].append(r)
    pairs = []
    far_past = datetime.min.replace(tzinfo=UTC)
    for b in buckets.values():
        if not b["old"] or not b["new"]:
            continue
        for old in b["old"]:
            ref = old.downloaded_at or old.first_seen_at
            newer = [n for n in b["new"]
                     if _published(n) > (_published(old) or far_past)
                     and ref is not None and n.first_seen_at > ref + REUPLOAD_MARGIN]
            if newer:
                new = max(newer, key=lambda n: (_published(n), str(n.id)))
                b["new"].remove(new)
                pairs.append((new, old))
    return pairs


def moved_out_recently(release: ChapterRelease, since: datetime | None) -> bool:
    """Downloaded by the app, moved out of the library, and downloaded at or after ``since``."""
    return (since is not None and release.local_status == LocalStatus.MISSING_ON_DISK
            and release.downloaded_at is not None and release.downloaded_at >= since)


def version_changed(release: ChapterRelease, moved_out_since: datetime | None = None) -> bool:
    """Same chapter UUID, newer MangaDex version than the downloaded copy (pages may have changed)."""
    return (
        (release.local_status in (LocalStatus.DOWNLOADED, LocalStatus.MODIFIED)
         or moved_out_recently(release, moved_out_since))
        and not release.is_unavailable
        and release.version is not None
        and release.downloaded_version is not None
        and release.version > release.downloaded_version
        and bool(release.page_manifest)
        and all(isinstance(m, dict) and m.get("source") for m in release.page_manifest)
    )


@dataclass(frozen=True)
class WantContext:
    """Per-manga inputs of :func:`release_wanted` that need the database."""

    owned_keys: frozenset[str] | None = None
    extras_since: datetime | None = None
    late_after: datetime | None = None
    # (language, chapter key) -> earliest publication of any release of that number.
    first_published: dict | None = None


def want_contexts(session: Session, manga_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, WantContext]:
    """Download-rule context for every manga in ``manga_ids`` (missing = defaults)."""
    ids = list(manga_ids)
    if not ids:
        return {}
    ext = external_contexts(session, ids)
    out: dict[uuid.UUID, WantContext] = {
        mid: WantContext(owned_keys=e.owned_keys, extras_since=e.extras_since) for mid, e in ext.items()
    }
    # Plain start chapter (nothing owned elsewhere): late new chapter numbers are wanted.
    late = dict(session.execute(
        select(MonitoringConfig.manga_id, MonitoringConfig.start_chapter_set_at)
        .where(MonitoringConfig.manga_id.in_([i for i in ids if i not in ext]),
               MonitoringConfig.start_chapter.is_not(None),
               MonitoringConfig.start_chapter_set_at.is_not(None))
    ).all()) if len(ext) < len(ids) else {}
    if late:
        firsts: dict[uuid.UUID, dict] = defaultdict(dict)
        for mid, lang, key, first in session.execute(
            select(ChapterRelease.manga_id, ChapterRelease.language_code, LogicalChapter.chapter_key,
                   func.min(func.coalesce(ChapterRelease.publish_at, ChapterRelease.readable_at)))
            .join(LogicalChapter, LogicalChapter.id == ChapterRelease.logical_chapter_id)
            .where(ChapterRelease.manga_id.in_(list(late)))
            .group_by(ChapterRelease.manga_id, ChapterRelease.language_code, LogicalChapter.chapter_key)
        ):
            firsts[mid][(lang, key)] = first
        for mid, since in late.items():
            out[mid] = WantContext(late_after=since, first_published=firsts.get(mid, {}))
    return out


# Statuses that mean "this chapter number is already taken care of".
COVERED_STATUSES = {
    LocalStatus.DOWNLOADED,
    LocalStatus.IMPORTED,
    LocalStatus.MODIFIED,
    LocalStatus.EXTERNAL,
    # Folder moved out of the library by the user (e.g. packed into a CBZ elsewhere):
    # the chapter is still owned, so no other release of it is fetched.
    LocalStatus.MISSING_ON_DISK,
}


def excluded_group_ids(session: Session) -> frozenset[str]:
    """IDs (as strings) of the groups listed in settings.excluded_group_names."""
    from app.settings_service import load_settings

    names = {n.lower() for n in load_settings(session).excluded_group_names}
    if not names:
        return frozenset()
    return frozenset(
        str(gid) for gid in session.scalars(
            select(ScanlationGroup.id).where(func.lower(ScanlationGroup.name).in_(names))
        )
    )


def in_excluded_group(release: ChapterRelease, excluded_groups) -> bool:
    return bool(excluded_groups) and any(str(g) in excluded_groups for g in (release.group_ids or []))


def covers_chapter(release: ChapterRelease, excluded_groups=None) -> bool:
    """Does this release mean its chapter number is taken care of?

    Placeholders from official publishers (external link) or from excluded groups
    never count, even if one was downloaded, so a real scanlation can still be picked.
    """
    if release.local_status not in COVERED_STATUSES:
        return False
    if release.local_status == LocalStatus.EXTERNAL:
        return True
    return not (release.is_external or in_excluded_group(release, excluded_groups))


def one_per_chapter(candidates: Iterable[ChapterRelease], covered_keys: set[str]) -> list[ChapterRelease]:
    """Keep the first-published release of each chapter number not already covered."""
    far_future = datetime.max.replace(tzinfo=UTC)
    seen = set(covered_keys)
    out = []
    for r in sorted(candidates, key=lambda r: (r.publish_at or r.readable_at or far_future, r.language_code, str(r.id))):
        key = r.logical_chapter.chapter_key if r.logical_chapter is not None else f"x:{r.id}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def get_or_create_logical_chapter(session: Session, manga_id: uuid.UUID, ch: ParsedChapter) -> LogicalChapter:
    manga = session.get(Manga, manga_id)
    per_volume = bool(manga is not None and manga.chapters_per_volume)
    key = logical_chapter_key(ch.chapter, ch.id, ch.volume, per_volume)
    lc = session.scalar(select(LogicalChapter).where(LogicalChapter.manga_id == manga_id, LogicalChapter.chapter_key == key))
    if lc is None:
        lc = LogicalChapter(
            manga_id=manga_id,
            chapter_key=key,
            chapter=ch.chapter,
            volume=ch.volume,
            sort_number=logical_sort_number(ch.chapter, ch.volume, per_volume),
            kind=chapter_kind(ch.chapter),
        )
        session.add(lc)
        session.flush()
    elif lc.volume is None and ch.volume:
        lc.volume = ch.volume
    return lc


def upsert_release(session: Session, manga_id: uuid.UUID, ch: ParsedChapter) -> tuple[ChapterRelease, bool]:
    """Insert or update a release from MangaDex data. Never touches local state."""
    ensure_language(session, ch.language)
    for g in ch.groups:
        ensure_group(session, uuid.UUID(g.id), name=g.name, website=g.website, official=g.official)
    rid = uuid.UUID(ch.id)
    release = session.get(ChapterRelease, rid)
    is_new = release is None
    lc = get_or_create_logical_chapter(session, manga_id, ch)
    now = datetime.now(UTC)
    if release is None:
        owned = session.scalar(
            select(ExternalChapter.id).where(ExternalChapter.manga_id == manga_id, ExternalChapter.release_id == rid)
            .limit(1)
        )
        status = LocalStatus.EXTERNAL if owned is not None else LocalStatus.MISSING
        release = ChapterRelease(id=rid, manga_id=manga_id, local_status=status, first_seen_at=now)
        session.add(release)
    release.logical_chapter_id = lc.id
    release.language_code = ch.language
    release.volume = ch.volume
    release.chapter = ch.chapter
    release.title = ch.title
    release.pages = ch.pages
    release.version = ch.version
    release.external_url = ch.external_url
    release.publish_at = ch.publish_at
    release.readable_at = ch.readable_at
    release.md_updated_at = ch.updated_at
    release.group_ids = [g.id for g in ch.groups]
    release.group_names = [g.name for g in ch.groups]
    release.is_unavailable = ch.unavailable
    release.raw = ch.raw
    release.last_seen_at = now
    session.flush()
    return release, is_new
