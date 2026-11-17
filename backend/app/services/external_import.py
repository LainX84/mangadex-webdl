"""Follow titles the user already owns elsewhere (e.g. downloaded with JAM).

For every title in the plan:
* it is added to the library with the given languages, all groups, automatic
  downloads on, ``start_chapter`` = newest owned chapter and one release per
  chapter number;
* every owned chapter is recorded in ``external_chapters``: that chapter number is
  never downloaded again (whatever the group or language) and the matching
  MangaDex release is shown as "external" instead of "missing".

Releases below ``start_chapter`` are then only fetched if published after the
newest owned chapter (late extras such as x.5, re-uploads); see
``catalog.release_wanted``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import select

from app.db import session_scope
from app.library.naming import logical_chapter_key
from app.logging_setup import get_logger
from app.mangadex.errors import MangaDexError, NotFoundError
from app.models import ExternalChapter, Manga
from app.services.catalog import CatalogError, add_manga
from app.settings_service import load_settings, save_settings

log = get_logger(__name__)

# Enough headroom to scan ~1000 titles every 6 hours (one light request each).
MAX_SCAN_SPACING_FOR_LARGE_LIBRARY = 10


def _validate(item: dict) -> tuple[uuid.UUID, list[str], float | None, list[tuple[uuid.UUID | None, str | None, str | None]]]:
    mid = uuid.UUID(str(item["id"]))
    langs = [str(x) for x in item.get("languages") or ["en"]]
    start = item.get("start_chapter")
    start = float(start) if start is not None else None
    ext = []
    for rid, lang, chapter in item.get("external") or []:
        ext.append((uuid.UUID(str(rid)) if rid else None, lang, chapter))
    return mid, langs, start, ext


def import_plan(plan: list[dict], *, source: str = "jam", dry_run: bool = False, limit: int | None = None,
                client=None, progress: Callable[[str], None] | None = None) -> dict:
    if client is None and not dry_run:
        from app.services.control import build_client

        client = build_client()
    report = {"imported": 0, "already_in_library": [], "not_found": [], "errors": [], "external_chapters": 0,
              "dry_run": dry_run}
    for n, item in enumerate(plan, 1):
        if limit is not None and report["imported"] >= limit:
            break
        try:
            mid, langs, start, ext = _validate(item)
        except (KeyError, ValueError, TypeError) as exc:
            report["errors"].append({"item": str(item.get("id")), "error": f"invalid plan entry: {exc}"})
            continue
        with session_scope() as s:
            if s.get(Manga, mid) is not None:
                report["already_in_library"].append(str(mid))
                continue
            if dry_run:
                report["imported"] += 1
                report["external_chapters"] += len(ext)
                continue
            try:
                manga = add_manga(s, client, mid, langs, start_chapter=start)
            except NotFoundError:
                report["not_found"].append(str(mid))
                continue
            except (CatalogError, MangaDexError) as exc:
                report["errors"].append({"item": str(mid), "error": str(exc)[:300]})
                continue
            manga.monitoring.one_release_per_chapter = True
            seen = set()
            for rid, lang, chapter in ext:
                if rid in seen:
                    continue
                seen.add(rid)
                s.add(ExternalChapter(manga_id=mid, source=source, release_id=rid, language_code=lang,
                                      chapter=chapter, chapter_key=logical_chapter_key(chapter, str(rid))))
            report["imported"] += 1
            report["external_chapters"] += len(seen)
        if progress and n % 25 == 0:
            progress(f"{n}/{len(plan)} processed, {report['imported']} imported")
    if report["imported"] and not dry_run:
        with session_scope() as s:
            settings = load_settings(s, use_cache=False)
            if settings.scan_spacing_seconds > MAX_SCAN_SPACING_FOR_LARGE_LIBRARY:
                report["scan_spacing_seconds"] = {"from": settings.scan_spacing_seconds,
                                                  "to": MAX_SCAN_SPACING_FOR_LARGE_LIBRARY}
                save_settings(s, settings.model_copy(update={"scan_spacing_seconds": MAX_SCAN_SPACING_FOR_LARGE_LIBRARY}))
    log.info("external_import_done", **{k: (v if isinstance(v, int) else len(v)) for k, v in report.items()
                                        if k != "dry_run" and not isinstance(v, dict)})
    return report
