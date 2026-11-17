"""Statistics: what the service has done since it started, with filters.

Read-only aggregation over PostgreSQL. A *download* is a completed download job
(redownloads, same-group re-uploads and version refreshes included, version checks
that found unchanged pages excluded); pages and bytes come from the page records.

Filters on the title (series, tags, type, rating, status, demographic) select the
manga; filters on the release (language, scanlation group) select the chapters;
``from``/``to`` (local dates in ``tz``, inclusive) select the period. Totals that
describe the library *now* (followed titles, chapters on disk, queue) ignore the
period but honour the other filters.
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.serializers import cover_url
from app.db import get_db

router = APIRouter(prefix="/api/stats", tags=["stats"])

TYPE_BY_LANG = {"ja": "manga", "ko": "manhwa", "zh": "manhua", "zh-hk": "manhua"}
LIBRARY_STATUSES = ("downloaded", "modified", "imported")
TAG_GROUPS = ("genre", "theme", "format", "content")
TOP_N = 25


def _type_of(original_language: str | None) -> str:
    return TYPE_BY_LANG.get((original_language or "").lower(), "other")


@dataclass
class MangaRow:
    id: uuid.UUID
    title: str
    tags: list[str]
    rating: str
    status: str
    type: str
    demographic: str
    cover: str | None
    added_at: datetime
    pending: bool
    monitoring: bool
    auto_download: bool
    tier: int | None
    upload_complete: bool
    tag_keys: set[str] = field(default_factory=set)


def _load_manga(db: Session) -> dict[uuid.UUID, MangaRow]:
    rows = db.execute(text("""
        SELECT m.id, coalesce(m.title_override, m.title) AS title, m.tags, m.content_rating, m.status,
               m.original_language, m.raw->'attributes'->>'publicationDemographic' AS demographic,
               m.cover_filename, m.created_at, m.pending_approval,
               coalesce(mc.monitoring_enabled, false), coalesce(mc.auto_download, false),
               mc.update_tier, coalesce(mc.upload_complete, false),
               (SELECT min(e.created_at) FROM job_events e WHERE e.manga_id = m.id AND e.event = 'approved') AS approved_at
        FROM manga m LEFT JOIN monitoring_configs mc ON mc.manga_id = m.id
    """)).all()
    out: dict[uuid.UUID, MangaRow] = {}
    for r in rows:
        tags = [t for t in (r[2] or []) if isinstance(t, str)]
        out[r[0]] = MangaRow(
            id=r[0], title=r[1], tags=tags, rating=r[3] or "unknown", status=r[4] or "unknown",
            type=_type_of(r[5]), demographic=r[6] or "none", cover=cover_url(r[0], r[7]),
            added_at=r[14] or r[8], pending=bool(r[9]), monitoring=bool(r[10]), auto_download=bool(r[11]),
            tier=r[12], upload_complete=bool(r[13]), tag_keys={t.lower() for t in tags},
        )
    return out


def _tag_groups(db: Session) -> dict[str, str]:
    rows = db.execute(text("""
        SELECT DISTINCT t->'attributes'->'name'->>'en', t->'attributes'->>'group'
        FROM manga, jsonb_array_elements(coalesce(raw->'attributes'->'tags', '[]'::jsonb)) t
    """)).all()
    return {name.lower(): grp for name, grp in rows if name and grp}


def _service_since(db: Session) -> datetime | None:
    return db.execute(text("""
        SELECT least((SELECT min(created_at) FROM manga), (SELECT min(created_at) FROM job_events),
                     (SELECT min(started_at) FROM monitoring_runs))
    """)).scalar()


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


# --------------------------------------------------------------------------- options


@router.get("/options")
def options(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Values the filters can take (titles, tags by group, languages, groups...)."""
    manga = {k: v for k, v in _load_manga(db).items() if not v.pending}
    groups_of = _tag_groups(db)
    tag_count: Counter[str] = Counter()
    tag_name: dict[str, str] = {}
    for m in manga.values():
        for t in m.tags:
            tag_count[t.lower()] += 1
            tag_name.setdefault(t.lower(), t)
    downloaded = db.execute(text("""
        SELECT r.manga_id, count(*) FROM download_jobs j JOIN chapter_releases r ON r.id = j.release_id
        WHERE j.status = 'completed' GROUP BY 1
    """)).all()
    dl = dict(downloaded)
    langs = db.execute(text("""
        SELECT r.language_code, coalesce(l.name, r.language_code), count(*)
        FROM download_jobs j JOIN chapter_releases r ON r.id = j.release_id
        LEFT JOIN languages l ON l.code = r.language_code
        WHERE j.status = 'completed' GROUP BY 1, 2 ORDER BY 3 DESC
    """)).all()
    grp = db.execute(text("""
        SELECT g, count(*) FROM download_jobs j JOIN chapter_releases r ON r.id = j.release_id,
               LATERAL jsonb_array_elements_text(CASE WHEN jsonb_array_length(r.group_names) = 0
                                                      THEN '["Unknown Group"]'::jsonb ELSE r.group_names END) g
        WHERE j.status = 'completed' GROUP BY 1 ORDER BY 2 DESC, 1
    """)).all()

    def counts(attr: str) -> list[dict[str, Any]]:
        c = Counter(getattr(m, attr) for m in manga.values())
        return [{"value": k, "series": n} for k, n in c.most_common()]

    since = _service_since(db)
    return {
        "since": since.isoformat() if since else None,
        "series": sorted(
            ({"id": str(m.id), "title": m.title, "chapters": dl.get(m.id, 0)} for m in manga.values()),
            key=lambda x: x["title"].lower(),
        ),
        "tags": sorted(
            ({"name": tag_name[k], "group": groups_of.get(k, "other"), "series": n} for k, n in tag_count.items()),
            key=lambda x: (-x["series"], x["name"].lower()),
        ),
        "languages": [{"code": c, "name": n, "chapters": k} for c, n, k in langs],
        "groups": [{"name": g, "chapters": n} for g, n in grp],
        "types": counts("type"),
        "ratings": counts("rating"),
        "statuses": counts("status"),
        "demographics": counts("demographic"),
    }


# --------------------------------------------------------------------------- stats


def _bucket_start(d: date, bucket: str) -> date:
    if bucket == "week":
        return d - timedelta(days=d.weekday())
    if bucket == "month":
        return d.replace(day=1)
    return d


def _next_bucket(d: date, bucket: str) -> date:
    if bucket == "week":
        return d + timedelta(days=7)
    if bucket == "month":
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return d + timedelta(days=1)


def _parse_date(v: str | None, name: str) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid {name} date") from exc


def _streaks(days: set[date], today: date) -> tuple[int, int]:
    longest = cur = 0
    prev: date | None = None
    for d in sorted(days):
        cur = cur + 1 if prev and d - prev == timedelta(days=1) else 1
        longest = max(longest, cur)
        prev = d
    current = 0
    d = today if today in days else today - timedelta(days=1)
    while d in days:
        current += 1
        d -= timedelta(days=1)
    return longest, current


@router.get("")
def stats(
    db: Session = Depends(get_db),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    tz: str = Query("UTC", max_length=64),
    bucket: str = Query("auto", pattern="^(auto|day|week|month)$"),
    manga: list[uuid.UUID] = Query(default_factory=list),
    tag: list[str] = Query(default_factory=list),
    tag_match: str = Query("any", pattern="^(any|all)$"),
    type_: list[str] = Query(default_factory=list, alias="type"),
    rating: list[str] = Query(default_factory=list),
    status_: list[str] = Query(default_factory=list, alias="status"),
    demographic: list[str] = Query(default_factory=list),
    lang: list[str] = Query(default_factory=list),
    group: list[str] = Query(default_factory=list),
) -> dict[str, Any]:
    zone = _zone(tz)
    now = datetime.now(timezone.utc)
    today = now.astimezone(zone).date()
    since = _service_since(db)
    since_day = since.astimezone(zone).date() if since else today

    d_from = _parse_date(from_, "from") or since_day
    d_to = _parse_date(to, "to") or today
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    days = (d_to - d_from).days + 1
    if bucket == "auto":
        bucket = "day" if days <= 62 else "week" if days <= 366 else "month"
    t_from = datetime.combine(d_from, datetime.min.time(), zone)
    t_to = datetime.combine(d_to + timedelta(days=1), datetime.min.time(), zone)
    p_from = t_from - (t_to - t_from)  # previous period of the same length

    # ---- titles
    all_manga = _load_manga(db)
    wanted_ids = set(manga)
    tag_keys = [t.strip().lower() for t in tag if t.strip()]
    types, ratings, statuses, demos = set(type_), set(rating), set(status_), set(demographic)

    def title_ok(m: MangaRow) -> bool:
        if wanted_ids and m.id not in wanted_ids:
            return False
        if tag_keys:
            hit = [k in m.tag_keys for k in tag_keys]
            if not (all(hit) if tag_match == "all" else any(hit)):
                return False
        return (not types or m.type in types) and (not ratings or m.rating in ratings) \
            and (not statuses or m.status in statuses) and (not demos or m.demographic in demos)

    selected = {k: m for k, m in all_manga.items() if title_ok(m)}
    approved = {k: m for k, m in selected.items() if not m.pending}
    langs, groups = set(lang), set(group)

    def release_ok(language: str, group_names: list[str]) -> bool:
        if langs and language not in langs:
            return False
        if groups and not groups.intersection(group_names or ["Unknown Group"]):
            return False
        return True

    filtered_release = bool(langs or groups)
    groups_of = _tag_groups(db)

    # ---- downloads (every completed job; period applied below)
    rows = db.execute(text("""
        SELECT j.id, j.finished_at, j.started_at, j.trigger, j.is_redownload, j.is_refresh,
               j.replaces_release_id IS NOT NULL, coalesce(j.pages_total, pr.n, r.pages, 0),
               r.manga_id, r.language_code, r.group_names, r.volume, r.chapter, r.title,
               coalesce(pr.bytes, 0), coalesce(r.readable_at, r.publish_at)
        FROM download_jobs j
        JOIN chapter_releases r ON r.id = j.release_id
        LEFT JOIN (SELECT release_id, sum(size_bytes) AS bytes, count(*) AS n
                   FROM page_records WHERE status = 'completed' GROUP BY release_id) pr ON pr.release_id = r.id
        WHERE j.status = 'completed' AND j.finished_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM job_events e WHERE e.job_id = j.id AND e.event = 'unchanged')
        ORDER BY j.finished_at
    """)).all()
    lang_names = dict(db.execute(text("SELECT code, name FROM languages")).all())

    timeline: dict[date, dict[str, Any]] = {}
    b = _bucket_start(d_from, bucket)
    while b <= d_to:
        timeline[b] = {"chapters": 0, "pages": 0, "bytes": 0, "series_added": 0, "scans": 0, "requests": 0}
        b = _next_bucket(b, bucket)

    s = Counter()
    prev = Counter()
    before = 0
    heat = [[0] * 24 for _ in range(7)]
    per_day: Counter[date] = Counter()
    per_series: dict[uuid.UUID, Counter] = defaultdict(Counter)
    per_series_last: dict[uuid.UUID, datetime] = {}
    per_lang: dict[str, Counter] = defaultdict(Counter)
    per_group: dict[str, Counter] = defaultdict(Counter)
    group_series: dict[str, set] = defaultdict(set)
    per_trigger: Counter[str] = Counter()
    volumes: set[tuple] = set()
    series_set: set = set()
    prev_series: set = set()
    tbd = 0
    durations: list[float] = []
    dur_pages = 0
    latencies: list[float] = []
    biggest: dict[str, Any] | None = None

    for (jid, fin, started, trig, redl, refresh, reupl, pages, mid, lcode, gnames, vol, chap, ctitle,
         nbytes, published) in rows:
        m = selected.get(mid)
        nbytes, pages = int(nbytes or 0), int(pages or 0)
        if m is None or not release_ok(lcode, gnames or []):
            continue
        if fin < t_from:
            if fin >= p_from:
                prev["chapters"] += 1
                prev["pages"] += pages
                prev["bytes"] += nbytes
                prev_series.add(mid)
            before += 1
            continue
        if fin >= t_to:
            continue
        local = fin.astimezone(zone)
        day = local.date()
        s["chapters"] += 1
        s["pages"] += pages
        s["bytes"] += nbytes
        s["redownloads"] += int(bool(redl))
        s["reuploads"] += int(bool(reupl))
        s["refreshes"] += int(bool(refresh))
        tl = timeline.get(_bucket_start(day, bucket))
        if tl is not None:
            tl["chapters"] += 1
            tl["pages"] += pages
            tl["bytes"] += nbytes
        heat[local.weekday()][local.hour] += 1
        per_day[day] += 1
        ps = per_series[mid]
        ps["chapters"] += 1
        ps["pages"] += pages
        ps["bytes"] += nbytes
        per_series_last[mid] = fin
        pl = per_lang[lcode]
        pl["chapters"] += 1
        pl["pages"] += pages
        for g in (gnames or ["Unknown Group"]):
            per_group[g]["chapters"] += 1
            per_group[g]["pages"] += pages
            group_series[g].add(mid)
        per_trigger[trig or "manual"] += 1
        series_set.add(mid)
        if vol:
            volumes.add((mid, lcode, vol))
        else:
            tbd += 1
        if started and fin > started and pages:
            durations.append((fin - started).total_seconds())
            dur_pages += pages
        # Arrival time: only chapters published after the title was added (not the backlog)
        if published and trig in ("monitor", "follow_feed") and published >= m.added_at and fin >= published:
            latencies.append((fin - published).total_seconds())
        if biggest is None or pages > biggest["pages"]:
            biggest = {"manga_id": str(mid), "title": m.title, "volume": vol, "chapter": chap,
                       "chapter_title": ctitle, "pages": pages, "bytes": nbytes, "at": fin.isoformat()}

    # cumulative chapters across the timeline
    running = before
    for k in sorted(timeline):
        running += timeline[k]["chapters"]
        timeline[k]["cumulative"] = running

    # ---- series added (approval date for titles that came from the MangaDex follows)
    added_in = [m for m in approved.values() if t_from <= m.added_at < t_to]
    for m in added_in:
        tl = timeline.get(_bucket_start(m.added_at.astimezone(zone).date(), bucket))
        if tl is not None:
            tl["series_added"] += 1

    # ---- monitoring scans
    scan_rows = db.execute(text("""
        SELECT manga_id, started_at, status, requests_made, new_releases
        FROM monitoring_runs WHERE started_at >= :a AND started_at < :b
    """), {"a": t_from, "b": t_to}).all()
    scans = Counter()
    for mid, started, st, req, new in scan_rows:
        if mid not in selected:
            continue
        scans["runs"] += 1
        scans["failed"] += int(st == "failed")
        scans["requests"] += req or 0
        scans["new_releases"] += new or 0
        tl = timeline.get(_bucket_start(started.astimezone(zone).date(), bucket))
        if tl is not None:
            tl["scans"] += 1
            tl["requests"] += req or 0

    # ---- problems in the period
    problems = Counter(dict(db.execute(text("""
        SELECT e.event, count(*) FROM job_events e JOIN chapter_releases r ON r.id = e.release_id
        WHERE e.created_at >= :a AND e.created_at < :b
          AND e.event IN ('retry_scheduled', 'cancelled', 'failed', 'needs_attention', 'recovered')
          AND r.manga_id = ANY(:ids)
        GROUP BY 1
    """), {"a": t_from, "b": t_to, "ids": list(selected)}).all()))

    # ---- library right now
    lib_rows = db.execute(text("""
        SELECT r.manga_id, r.local_status, r.language_code, r.group_names, r.volume,
               coalesce(r.local_page_count, r.pages), coalesce(pr.bytes, 0)
        FROM chapter_releases r
        LEFT JOIN (SELECT release_id, sum(size_bytes) AS bytes FROM page_records
                   WHERE status = 'completed' GROUP BY release_id) pr ON pr.release_id = r.id
        WHERE r.local_status IN ('downloaded', 'modified', 'imported', 'missing_on_disk', 'external', 'skipped')
    """)).all()
    lib = Counter()
    lib_vol: set = set()
    for mid, st, lcode, gnames, vol, pages, nbytes in lib_rows:
        if mid not in approved or not release_ok(lcode, gnames or []):
            continue
        if st in LIBRARY_STATUSES:
            lib["chapters"] += 1
            lib["pages"] += pages or 0
            lib["bytes"] += int(nbytes or 0)
            if vol:
                lib_vol.add((mid, lcode, vol))
            else:
                lib["tbd"] += 1
        elif st == "missing_on_disk":
            lib["moved_out"] += 1
        elif st == "external":
            lib["owned_elsewhere"] += 1
        else:
            lib["skipped"] += 1
    lib["volumes"] = len(lib_vol)

    queue = Counter()
    for mid, st, lcode, gnames in db.execute(text("""
        SELECT r.manga_id, j.status, r.language_code, r.group_names FROM download_jobs j
        JOIN chapter_releases r ON r.id = j.release_id
        WHERE j.status NOT IN ('completed', 'cancelled')
    """)).all():
        if mid in selected and release_ok(lcode, gnames or []):
            key = {"queued": "active", "downloading": "active", "retry_scheduled": "pending",
                   "needs_attention": "failed"}.get(st, st)
            queue[key] += 1

    # ---- breakdowns by title attributes (series = approved titles; chapters = downloads in the period)
    def by_attr(attr: str) -> list[dict[str, Any]]:
        series_n: Counter[str] = Counter(getattr(m, attr) for m in approved.values())
        ch: Counter[str] = Counter()
        pg: Counter[str] = Counter()
        for mid, c in per_series.items():
            ch[getattr(selected[mid], attr)] += c["chapters"]
            pg[getattr(selected[mid], attr)] += c["pages"]
        keys = set(series_n) | set(ch)
        return sorted(({"name": k, "series": series_n[k], "chapters": ch[k], "pages": pg[k]} for k in keys),
                      key=lambda x: (-x["chapters"], -x["series"], x["name"]))

    tag_series: Counter[str] = Counter()
    tag_ch: Counter[str] = Counter()
    tag_pg: Counter[str] = Counter()
    tag_disp: dict[str, str] = {}
    for m in approved.values():
        for t in m.tags:
            tag_series[t.lower()] += 1
            tag_disp.setdefault(t.lower(), t)
    for mid, c in per_series.items():
        for t in selected[mid].tags:
            tag_ch[t.lower()] += c["chapters"]
            tag_pg[t.lower()] += c["pages"]
            tag_disp.setdefault(t.lower(), t)
    by_tag: dict[str, list] = {g: [] for g in (*TAG_GROUPS, "other")}
    for k in set(tag_series) | set(tag_ch):
        by_tag.setdefault(groups_of.get(k, "other"), []).append(
            {"name": tag_disp[k], "series": tag_series[k], "chapters": tag_ch[k], "pages": tag_pg[k]})
    for g in by_tag:
        by_tag[g].sort(key=lambda x: (-x["chapters"], -x["series"], x["name"]))

    top_series = sorted(
        ({"id": str(mid), "title": selected[mid].title, "cover_url": selected[mid].cover,
          "tier": selected[mid].tier, "chapters": c["chapters"], "pages": c["pages"], "bytes": c["bytes"],
          "last_at": per_series_last[mid].isoformat()} for mid, c in per_series.items()),
        key=lambda x: (-x["chapters"], -x["pages"]),
    )

    busiest = max(per_day.items(), key=lambda kv: kv[1]) if per_day else None
    longest, current = _streaks(set(per_day), today)
    total_dl_s = sum(durations)

    def pct(cur: int, old: int) -> float | None:
        return None if old == 0 else round((cur - old) / old * 100, 1)

    return {
        "generated_at": now.isoformat(),
        "tz": str(zone),
        "since": since.isoformat() if since else None,
        "range": {"from": d_from.isoformat(), "to": d_to.isoformat(), "days": days, "bucket": bucket},
        "filtered": {
            "titles": bool(wanted_ids or tag_keys or types or ratings or statuses or demos),
            "releases": filtered_release,
            "series_selected": len(approved),
        },
        "summary": {
            "chapters": s["chapters"], "pages": s["pages"], "bytes": s["bytes"],
            "series": len(series_set), "volumes": len(volumes), "chapters_without_volume": tbd,
            "groups": len(per_group), "languages": len(per_lang),
            "active_days": len(per_day),
            "per_active_day": round(s["chapters"] / len(per_day), 1) if per_day else 0,
            "avg_pages": round(s["pages"] / s["chapters"], 1) if s["chapters"] else 0,
            "avg_bytes": round(s["bytes"] / s["chapters"]) if s["chapters"] else 0,
            "avg_page_bytes": round(s["bytes"] / s["pages"]) if s["pages"] else 0,
            "redownloads": s["redownloads"], "reuploads": s["reuploads"], "refreshes": s["refreshes"],
            "download_seconds": round(total_dl_s),
            "pages_per_minute": round(dur_pages / (total_dl_s / 60), 1) if total_dl_s else None,
            "median_arrival_seconds": round(statistics.median(latencies)) if latencies else None,
            "arrival_samples": len(latencies),
            "series_added": len(added_in),
            "scans": scans["runs"], "scan_failures": scans["failed"], "requests": scans["requests"],
            "new_releases_found": scans["new_releases"],
            "retries": problems["retry_scheduled"], "cancelled": problems["cancelled"],
            "recovered": problems["recovered"],
        },
        "previous": {
            "from": p_from.astimezone(zone).date().isoformat(),
            "to": (t_from - timedelta(days=1)).astimezone(zone).date().isoformat(),
            "available": bool(since) and t_from > since,
            "chapters": prev["chapters"], "pages": prev["pages"], "bytes": prev["bytes"], "series": len(prev_series),
            "delta_pct": {"chapters": pct(s["chapters"], prev["chapters"]), "pages": pct(s["pages"], prev["pages"]),
                          "bytes": pct(s["bytes"], prev["bytes"])},
        },
        "now": {
            "series_followed": len(approved),
            "monitored": sum(m.monitoring for m in approved.values()),
            "auto_download": sum(m.auto_download for m in approved.values()),
            "pending_approval": sum(m.pending for m in selected.values()),
            "upload_complete": sum(m.upload_complete for m in approved.values()),
            "tiers": {str(t): sum(1 for m in approved.values() if m.tier == t) for t in (1, 2, 3)},
            "library": {k: lib[k] for k in ("chapters", "pages", "bytes", "volumes", "tbd", "moved_out",
                                             "owned_elsewhere", "skipped")},
            "queue": {k: queue[k] for k in ("active", "pending", "paused", "failed")},
        },
        "timeline": [{"t": k.isoformat(), **v} for k, v in sorted(timeline.items())],
        "heatmap": heat,
        "top_series": top_series[:TOP_N],
        "series_with_downloads": len(top_series),
        "by_tag": by_tag,
        "by_type": by_attr("type"),
        "by_rating": by_attr("rating"),
        "by_status": by_attr("status"),
        "by_demographic": by_attr("demographic"),
        "by_language": sorted(({"code": k, "name": lang_names.get(k, k), **v} for k, v in per_lang.items()),
                              key=lambda x: -x["chapters"]),
        "by_group": sorted(({"name": k, "series": len(group_series[k]), **v} for k, v in per_group.items()),
                           key=lambda x: (-x["chapters"], x["name"]))[:TOP_N],
        "groups_total": len(per_group),
        "by_trigger": [{"name": k, "chapters": n} for k, n in per_trigger.most_common()],
        "records": {
            "busiest_day": {"date": busiest[0].isoformat(), "chapters": busiest[1]} if busiest else None,
            "biggest_chapter": biggest,
            "longest_streak": longest,
            "current_streak": current,
        },
    }
