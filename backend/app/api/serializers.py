from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models import ChapterRelease, DownloadJob, Manga, MonitoringRun


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def cover_url(manga_id, filename: str | None) -> str | None:
    if not filename:
        return None
    return f"/api/covers/{manga_id}/{filename}"


def manga_summary(m: Manga, languages: list[str], groups: list[dict], counts: dict[str, int]) -> dict[str, Any]:
    cfg = m.monitoring
    return {
        "id": str(m.id),
        "title": m.display_title,
        "canonical_title": m.title,
        "title_override": m.title_override,
        "folder_name": m.folder_name,
        "cover_url": cover_url(m.id, m.cover_filename),
        "status": m.status,
        "year": m.year,
        "languages": languages,
        "group_mode": cfg.group_mode if cfg else "all",
        "groups": groups,
        "downloaded": counts.get("downloaded", 0),
        "missing": counts.get("missing", 0),
        "external": counts.get("external", 0),
        "active_jobs": counts.get("active", 0),
        "failed_jobs": counts.get("failed", 0),
        "monitoring_enabled": cfg.monitoring_enabled if cfg else False,
        "auto_download": cfg.auto_download if cfg else False,
        "last_scan_at": iso(cfg.last_scan_at) if cfg else None,
        "last_success_at": iso(cfg.last_success_at) if cfg else None,
        "next_scan_at": iso(cfg.next_scan_at) if cfg else None,
        "last_error": cfg.last_error if cfg else None,
        "update_tier": cfg.update_tier if cfg else None,
        "upload_complete": bool(cfg.upload_complete) if cfg else False,
        "last_chapter_at": iso(cfg.last_chapter_at) if cfg else None,
        "prev_chapter_at": iso(cfg.prev_chapter_at) if cfg else None,
        "pending_approval": bool(m.pending_approval),
        "pending_since": iso(m.pending_since),
        "added_via": m.added_via,
        "tags": list(m.tags or []),
    }


def manga_detail(m: Manga, languages: list[str], groups: list[dict], counts: dict[str, int]) -> dict[str, Any]:
    d = manga_summary(m, languages, groups, counts)
    cfg = m.monitoring
    d.update(
        {
            "alt_titles": m.alt_titles,
            "description": m.description,
            "content_rating": m.content_rating,
            "original_language": m.original_language,
            "tags": m.tags,
            "authors": m.authors,
            "available_languages": m.available_languages,
            "last_volume": m.last_volume,
            "last_chapter": m.last_chapter,
            "mangadex_url": f"https://mangadex.org/title/{m.id}",
            "interval_minutes": cfg.interval_minutes if cfg else None,
            "include_unknown_group": cfg.include_unknown_group if cfg else True,
            "start_chapter": float(cfg.start_chapter) if cfg and cfg.start_chapter is not None else None,
            "one_release_per_chapter": bool(cfg.one_release_per_chapter) if cfg else False,
            # start_chapter is volume * 1000 + chapter when the numbering restarts every volume.
            "chapters_per_volume": bool(m.chapters_per_volume),
            "consecutive_failures": cfg.consecutive_failures if cfg else 0,
            "metadata_refreshed_at": iso(m.metadata_refreshed_at),
        }
    )
    return d


def job_out(j: DownloadJob, release: ChapterRelease | None = None, manga: Manga | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": j.id,
        "release_id": str(j.release_id),
        "status": j.status,
        "pause_reason": j.pause_reason,
        "attempts": j.attempts,
        "next_attempt_at": iso(j.next_attempt_at),
        "last_error": j.last_error,
        "error_kind": j.error_kind,
        # Only report page progress when the page count is actually known.
        "pages_total": j.pages_total,
        "pages_done": j.pages_done if j.pages_total else None,
        "is_redownload": j.is_redownload,
        "trigger": j.trigger,
        "target": j.target_rel_path,
        "created_at": iso(j.created_at),
        "started_at": iso(j.started_at),
        "finished_at": iso(j.finished_at),
        "updated_at": iso(j.updated_at),
    }
    if release is not None:
        out["release"] = release_brief(release)
    if manga is not None:
        cfg = manga.monitoring
        out["manga"] = {"id": str(manga.id), "title": manga.display_title,
                        "cover_url": cover_url(manga.id, manga.cover_filename),
                        "update_tier": cfg.update_tier if cfg else None,
                        "upload_complete": bool(cfg.upload_complete) if cfg else False}
    return out


def release_brief(r: ChapterRelease) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "language": r.language_code,
        "volume": r.volume,
        "chapter": r.chapter,
        "title": r.title,
        "groups": r.group_names or [],
    }


def release_out(r: ChapterRelease, job: DownloadJob | None, wanted: bool,
                in_volume: str | None = None) -> dict[str, Any]:
    return {
        **release_brief(r),
        "group_ids": r.group_ids or [],
        "pages": r.pages,
        "publish_at": iso(r.publish_at),
        "readable_at": iso(r.readable_at),
        "external": r.is_external,
        "unavailable": r.is_unavailable,
        "local_status": r.local_status,
        "folder": r.folder_rel_path,
        "in_volume_cbz": in_volume,
        "local_page_count": r.local_page_count,
        "downloaded_at": iso(r.downloaded_at),
        "wanted": wanted,
        "sort": float(r.logical_chapter.sort_number) if r.logical_chapter and r.logical_chapter.sort_number is not None else None,
        "job": job_out(j=job) if job else None,
        "mangadex_url": f"https://mangadex.org/chapter/{r.id}",
    }


def run_out(run: MonitoringRun, title: str | None = None) -> dict[str, Any]:
    return {
        "id": run.id,
        "manga_id": str(run.manga_id),
        "manga_title": title,
        "trigger": run.trigger,
        "full_scan": run.full_scan,
        "status": run.status,
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "releases_seen": run.releases_seen,
        "new_releases": run.new_releases,
        "queued": run.queued,
        "requests_made": run.requests_made,
        "error": run.error,
    }
