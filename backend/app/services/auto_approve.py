"""Approve titles waiting for approval that are already in the comics archive.

A followed title whose romaji or English MangaDex title names a folder of the archive
is something you are already collecting: it is approved the way the add form would do
it with the archive suggestion ("Continue from Ch. N", or "New chapters only" when the
archive is up to date), in the language of the archive folder, all groups, monitoring
and auto-download on.

Only clear cases are approved. A title stays in Approvals (with the reason in the
report) when:

- the matching folder also matches another title (pending or in the library),
- it matches folders in more than one language, or several folders in one language,
- no chapter number can be read from the folder (e.g. volumes MangaDex can't map).

When MangaDex lists no chapter in that language (removed after a DMCA notice), the title
is approved anyway, named after the archive folder (the name already in use), starting
after the newest chapter of the archive: anything uploaded again later is downloaded.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import ArchiveFolder, Manga

log = get_logger(__name__)

ROMAJI_OR_ENGLISH = ("en",)


def _is_wanted_lang(code: str) -> bool:
    return code in ROMAJI_OR_ENGLISH or code.endswith("-ro")


def title_keys(m: Manga) -> set[str]:
    """Normalised romaji / English titles of the manga (main title + matching alternative titles)."""
    from app.services.archive import manga_title_keys

    main = ((m.raw or {}).get("attributes") or {}).get("title") or {}
    titles: list[str | None] = [m.title]
    if isinstance(main, dict):
        titles += [v for k, v in main.items() if isinstance(v, str) and _is_wanted_lang(k)]
    alts = [{k: v for k, v in a.items() if _is_wanted_lang(k)} for a in (m.alt_titles or []) if isinstance(a, dict)]
    return manga_title_keys(titles, [a for a in alts if a])


def _md_pairs(client, manga_id: uuid.UUID, language: str) -> list[tuple[float, str | None]]:
    from app.library.naming import chapter_sort_number
    from app.mangadex.parse import parse_chapter

    pairs = []
    for item in client.iter_manga_feed(str(manga_id), [language], cache_ttl=600):
        ch = parse_chapter(item)
        n = chapter_sort_number(ch.chapter)
        if n is not None and not ch.is_external:
            pairs.append((n, ch.volume))
    return pairs


def plan(session: Session, client, manga_ids: list[uuid.UUID] | None = None) -> list[dict[str, Any]]:
    """What would be done for every pending title (or the given ones) found in the archive.

    Each entry: manga_id, title, folder, language, and either ``action`` = "approve"
    (with start_chapter / only_new / last_chapter) or ``action`` = "skip" with ``reason``.
    Titles with no matching folder are left out. Reads one MangaDex feed per candidate.
    """
    from app.services import archive

    root = archive.archive_root()
    if root is None:
        return []
    every = list(session.scalars(select(Manga)))
    # Which titles (pending or not) claim each archive key, to spot folders shared by two titles.
    keys_of = {m.id: title_keys(m) for m in every}
    claimed: dict[str, set[uuid.UUID]] = defaultdict(set)
    for mid, keys in keys_of.items():
        for k in keys:
            claimed[k].add(mid)
    wanted = set(manga_ids) if manga_ids is not None else None
    pending = sorted((m for m in every if m.pending_approval and (wanted is None or m.id in wanted)),
                     key=lambda m: m.title.lower())
    out: list[dict[str, Any]] = []
    for m in pending:
        keys = keys_of[m.id]
        if not keys:
            continue
        rows = list(session.scalars(select(ArchiveFolder).where(ArchiveFolder.title_norm.in_(keys))))
        if not rows:
            continue
        entry: dict[str, Any] = {"manga_id": str(m.id), "title": m.title,
                                 "folders": [r.rel_path for r in rows]}
        others = {o for r in rows for o in claimed.get(r.title_norm, set())} - {m.id}
        if others:
            names = sorted(o.title for o in every if o.id in others)
            out.append({**entry, "action": "skip", "reason": f"folder also matches: {', '.join(names)[:200]}"})
            continue
        langs = {r.language for r in rows if r.language}
        if len(langs) != 1:
            out.append({**entry, "action": "skip", "reason": "folders in several languages" if langs else "unknown language"})
            continue
        if len(rows) > 1:
            out.append({**entry, "action": "skip", "reason": "several folders in the same language"})
            continue
        row, lang = rows[0], langs.pop()
        entry.update(folder=row.rel_path, language=lang)
        pairs = _md_pairs(client, m.id, lang)
        analysis = archive.folder_analysis(session, row, root)
        if not pairs:
            # Removed from MangaDex (DMCA): keep following it under the name already in use.
            last = analysis.chapter
            entry.update(action="approve", removed=True, title_override=row.title, last_chapter=last,
                         evidence=analysis.chapter_file)
            if last is not None and _reliable(analysis, archive, row):
                entry.update(only_new=False, start_chapter=float(math.floor(last) + 1))
            else:
                entry.update(only_new=True, start_chapter=None)
            out.append(entry)
            continue
        r = archive.resolve(analysis, archive.MangaDexChapters.build(pairs))
        if r and r.get("last_chapter") is not None and r.get("source") != "volume" \
                and not _reliable(analysis, archive, row, r["last_chapter"]):
            vol = _inside_volume(analysis, archive, row)
            # e.g. "c007" read inside "Volume 15": numbering restarts per volume, the number is not reliable
            out.append({**entry, "action": "skip",
                        "reason": f"ch. {_fmt(r['last_chapter'])} read inside volume {_fmt(vol)}: not reliable"})
            continue
        if not r or r.get("last_chapter") is None:
            why = "only volumes MangaDex can't map to chapters" if r and r.get("source") == "volume_unmapped" \
                else "no chapter number found in the folder"
            out.append({**entry, "action": "skip", "reason": why})
            continue
        entry.update(action="approve", last_chapter=r["last_chapter"], evidence=r.get("evidence"))
        if r.get("up_to_date"):
            entry.update(only_new=True, start_chapter=None)
        else:
            entry.update(only_new=False, start_chapter=r["next_chapter"], after_count=r.get("after_count"))
        out.append(entry)
    session.flush()  # cached folder analyses
    return out


def _inside_volume(analysis, archive, row) -> float | None:
    """Volume number of the archive the newest chapter was read from (or the newest volume)."""
    vol = _num(analysis.volume)
    if vol is None and analysis.chapter_from == "inside" and analysis.chapter_file:
        kind, n = archive.parse_archive_name(analysis.chapter_file, row.title)
        vol = n if kind == "volume" else None
    return vol


def _reliable(analysis, archive, row, last: float | None = None) -> bool:
    """A chapter number lower than the volume it was read from means numbering restarts per volume."""
    last = analysis.chapter if last is None else last
    vol = _inside_volume(analysis, archive, row)
    return last is not None and (vol is None or last >= vol)


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(n: float | None) -> str:
    return "?" if n is None else f"{n:g}"


def apply(session: Session, client, entries: list[dict[str, Any]]) -> list[str]:
    """Approve the "approve" entries of a plan. Commits each title on its own; returns approved ids."""
    from app.enums import GroupMode, ScanTrigger
    from app.services import catalog
    from app.services.control import record_event
    from app.services.jobs import enqueue_scan

    done: list[str] = []
    for e in entries:
        if e.get("action") != "approve":
            continue
        m = session.get(Manga, uuid.UUID(e["manga_id"]))
        if m is None or not m.pending_approval:
            continue
        try:
            catalog.approve_pending_manga(session, client, m, [e["language"]], GroupMode.ALL, [], True, True,
                                          e.get("title_override"), True, e.get("start_chapter"))
            if e.get("only_new"):
                catalog.mark_current_releases_owned(session, client, m)
            how = "new chapters only" if e.get("only_new") else f"continue from ch. {_fmt(e.get('start_chapter'))}"
            removed = "; no chapters on MangaDex (removed)" if e.get("removed") else ""
            record_event(session, "approved", e.get("note") or (
                f"Approved automatically: in the comics archive up to ch. {_fmt(e.get('last_chapter'))} "
                f"({e['folder']}); {e['language'].upper()}, {how}{removed}"), manga_id=m.id)
            session.commit()
        except Exception as exc:  # one bad title never blocks the others
            session.rollback()
            log.warning("auto_approve_failed", manga_id=e["manga_id"], error=str(exc))
            e["action"], e["reason"] = "skip", f"approval failed: {exc}"[:200]
            continue
        enqueue_scan(m.id, ScanTrigger.ADDED)
        done.append(e["manga_id"])
    log.info("auto_approve_done", approved=len(done))
    return done


def run(session: Session, client, manga_ids: list[uuid.UUID] | None = None, do_apply: bool = True) -> dict[str, Any]:
    """Plan (and with ``do_apply`` approve) — used by the CLI, the archive index and the followed-list sync."""
    entries = plan(session, client, manga_ids)
    session.commit()
    approved = apply(session, client, entries) if do_apply else []
    return {"approved": len(approved), "would_approve": sum(e["action"] == "approve" for e in entries),
            "skipped": sum(e["action"] == "skip" for e in entries), "entries": entries}
