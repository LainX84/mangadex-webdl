"""User-tunable application settings stored in the database.

Defaults are deliberately conservative. Faster settings mean more requests per
minute towards MangaDex and a higher chance of rate limiting; no combination of
settings can *guarantee* that an IP will never be restricted.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import AppSetting

APP_SETTINGS_KEY = "app"
QUEUE_PAUSED_KEY = "queue_paused"
ACCESS_BLOCK_KEY = "access_block"
MD_ACCOUNT_STATE_KEY = "mangadex_account_state"
SETUP_TOKEN_KEY = "setup_token_hash"


DEFAULT_EXCLUDED_GROUPS = ("Alpha Manga", "K MANGA", "Manga UP!", "MangaPlus", "Omoi", "Azuki Manga")


_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class HighlightedTag(BaseModel):
    """A MangaDex tag shown in its own colour wherever titles are listed."""

    name: str = Field(min_length=1, max_length=100)
    color: str = "#7048e8"


def _default_archive_dirs(custom: bool = False) -> tuple[str, ...]:
    from app.services.archive import DEFAULT_ARCHIVE_CUSTOM_DIRS, DEFAULT_ARCHIVE_DIRS

    return DEFAULT_ARCHIVE_CUSTOM_DIRS if custom else DEFAULT_ARCHIVE_DIRS


class AppSettings(BaseModel):
    # Monitoring
    monitoring_interval_minutes: int = Field(360, ge=30, le=7 * 24 * 60)
    full_scan_interval_hours: int = Field(24, ge=1, le=24 * 30)
    scan_spacing_seconds: int = Field(60, ge=5, le=3600)
    # Adaptive monitoring levels (see app.services.tiers). Level 1 uses the normal interval.
    adaptive_monitoring: bool = True
    tier1_max_days: int = Field(90, ge=1, le=3650)     # Lv.1: updated within ~3 months
    tier2_max_days: int = Field(365, ge=2, le=3650)    # Lv.2: within a year; Lv.3 beyond or complete
    tier2_interval_minutes: int = Field(24 * 60, ge=30, le=30 * 24 * 60)
    tier3_interval_minutes: int = Field(7 * 24 * 60, ge=30, le=90 * 24 * 60)

    # Concurrency
    max_concurrent_chapters: int = Field(1, ge=1, le=4)
    max_concurrent_pages: int = Field(2, ge=1, le=8)

    # Request rates (all processes share these limits)
    api_requests_per_second: float = Field(1.0, gt=0, le=5.0)
    api_min_interval_ms: int = Field(500, ge=0, le=60_000)
    image_requests_per_second: float = Field(2.0, gt=0, le=10.0)
    image_min_interval_ms: int = Field(250, ge=0, le=60_000)
    at_home_requests_per_minute: int = Field(20, ge=1, le=40)

    # Revisions of chapters already moved out of the library (same-group re-upload or new
    # MangaDex version): downloaded into the replaced folder when the chapter was downloaded
    # at most this many days ago; older ones are ignored. 0 = never.
    revision_after_move_days: int = Field(15, ge=0, le=365)

    # Weekly library check (chapters moved out by hand -> "Folder deleted"); local time.
    library_check_weekday: int = Field(6, ge=-1, le=6)  # 0 = Monday ... 6 = Sunday, -1 = off
    library_check_hour: int = Field(5, ge=0, le=23)

    # Retries
    max_retries: int = Field(8, ge=0, le=50)
    initial_retry_delay_seconds: int = Field(30, ge=1, le=3600)
    max_retry_delay_seconds: int = Field(6 * 3600, ge=10, le=7 * 24 * 3600)
    page_retry_limit: int = Field(3, ge=1, le=10)
    rate_limit_base_cooldown_seconds: int = Field(60, ge=5, le=3600)
    rate_limit_max_cooldown_seconds: int = Field(3600, ge=60, le=24 * 3600)

    # Naming
    volume_padding: int = Field(2, ge=1, le=4)
    chapter_padding_with_volume: int = Field(2, ge=1, le=5)
    chapter_padding_without_volume: int = Field(3, ge=1, le=5)
    # "Volume TBD Chapter 12": every chapter folder starts with its volume ("" = old "Chapter 12").
    volume_placeholder: str = Field("TBD", max_length=20)
    # "… - Team X" at the end of the chapter folder instead of a {Group} folder level.
    group_in_chapter_name: bool = True
    # Rename chapter folders in the library when MangaDex assigns (or changes) their volume.
    rename_on_volume_change: bool = True

    # Content
    content_ratings: list[str] = Field(default_factory=lambda: ["safe", "suggestive", "erotica", "pornographic"])
    use_data_saver: bool = False
    report_to_mangadex_at_home: bool = True
    # Scanlation groups never downloaded automatically (by MangaDex group name, case-insensitive).
    # Typically official publishers whose releases are a one-page link to a paid site.
    # Display: chapter list order on the manga pages ("asc" = oldest first).
    chapter_sort_order: Literal["asc", "desc"] = "asc"
    excluded_group_names: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_GROUPS), max_length=500)

    # MangaDex account (only used when the md_* Docker secrets are filled in)
    mangadex_account_enabled: bool = True
    # One request covers every followed manga: new chapters show up within this interval.
    follow_feed_interval_minutes: int = Field(15, ge=5, le=24 * 60)
    # Followed list refresh (new follows become titles waiting for approval).
    follow_list_interval_minutes: int = Field(360, ge=30, le=7 * 24 * 60)
    follow_add_pending: bool = True
    # Languages preselected for titles added from the followed list (changed on approval).
    follow_default_languages: list[str] = Field(default_factory=lambda: ["en"], max_length=20)

    # Read-only comics archive (app.services.archive): suggests where to start downloading.
    archive_enabled: bool = True
    archive_dirs: list[str] = Field(default_factory=lambda: list(_default_archive_dirs()), max_length=100)
    # Only "… - Custom" archives are looked at in these (official releases + volumes built from MangaDex).
    archive_custom_dirs: list[str] = Field(default_factory=lambda: list(_default_archive_dirs(custom=True)),
                                           max_length=100)
    archive_rescan_hours: int = Field(24, ge=1, le=24 * 30)
    # Titles waiting for approval that are already in the archive are approved automatically
    # (app.services.auto_approve), continuing from the archive, after each archive index and
    # each followed-list sync.
    archive_auto_approve: bool = False

    # Display only: MangaDex tags (by English name) highlighted in their own colour.
    highlighted_tags: list[HighlightedTag] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _check(self) -> AppSettings:
        if self.max_retry_delay_seconds < self.initial_retry_delay_seconds:
            raise ValueError("max_retry_delay_seconds must be >= initial_retry_delay_seconds")
        if self.tier2_max_days <= self.tier1_max_days:
            raise ValueError("tier2_max_days must be greater than tier1_max_days")
        if self.rate_limit_max_cooldown_seconds < self.rate_limit_base_cooldown_seconds:
            raise ValueError("rate_limit_max_cooldown_seconds must be >= rate_limit_base_cooldown_seconds")
        allowed = {"safe", "suggestive", "erotica", "pornographic"}
        bad = set(self.content_ratings) - allowed
        if bad or not self.content_ratings:
            raise ValueError(f"content_ratings must be a non-empty subset of {sorted(allowed)}")
        names: list[str] = []
        seen: set[str] = set()
        for n in self.excluded_group_names:
            n = " ".join(str(n).split())
            if n and n.lower() not in seen and len(n) <= 200:
                seen.add(n.lower())
                names.append(n)
        self.excluded_group_names = names
        langs = list(dict.fromkeys(str(x).strip() for x in self.follow_default_languages if str(x).strip()))
        if not langs:
            raise ValueError("follow_default_languages needs at least one language")
        self.follow_default_languages = langs
        self.archive_dirs = _clean_dirs(self.archive_dirs)
        self.archive_custom_dirs = _clean_dirs(self.archive_custom_dirs)
        tags: list[HighlightedTag] = []
        seen_tags: set[str] = set()
        for t in self.highlighted_tags:
            name = " ".join(t.name.split())
            if not name or name.casefold() in seen_tags:
                continue
            if not _HEX_COLOR.match(t.color.strip()):
                raise ValueError(f"highlighted_tags: invalid colour {t.color!r} for {name!r} (use #rrggbb)")
            seen_tags.add(name.casefold())
            tags.append(HighlightedTag(name=name, color=t.color.strip().lower()))
        self.highlighted_tags = tags
        ph = " ".join(str(self.volume_placeholder or "").split())
        if any(c in ph for c in '/\\:*?"<>|.') or (ph and ph[0].isdigit()):
            raise ValueError("volume_placeholder: letters only, e.g. TBD (it must not look like a volume number)")
        self.volume_placeholder = ph
        return self


def _clean_dirs(values: list[str]) -> list[str]:
    dirs: list[str] = []
    for d in values:
        d = "/".join(p.strip() for p in str(d).replace("\\", "/").split("/") if p.strip())
        if d and ".." not in d.split("/") and d not in dirs:
            dirs.append(d)
    return dirs


_cache_lock = threading.Lock()
_cache: tuple[float, AppSettings] | None = None
_CACHE_TTL = 5.0


def invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def get_raw(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(AppSetting, key)
    return default if row is None else row.value


def set_raw(session: Session, key: str, value: Any) -> None:
    stmt = pg_insert(AppSetting).values(key=key, value=value, updated_at=datetime.now(UTC))
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key], set_={"value": value, "updated_at": datetime.now(UTC)}
    )
    session.execute(stmt)


def delete_raw(session: Session, key: str) -> None:
    row = session.get(AppSetting, key)
    if row is not None:
        session.delete(row)


def load_settings(session: Session, use_cache: bool = True) -> AppSettings:
    global _cache
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            if _cache and now - _cache[0] < _CACHE_TTL:
                return _cache[1]
    raw = get_raw(session, APP_SETTINGS_KEY, {}) or {}
    defaults = AppSettings().model_dump()
    merged = {**defaults, **{k: v for k, v in raw.items() if k in defaults}}
    try:
        settings = AppSettings.model_validate(merged)
    except Exception:
        settings = AppSettings()
    with _cache_lock:
        _cache = (now, settings)
    return settings


def save_settings(session: Session, settings: AppSettings) -> AppSettings:
    set_raw(session, APP_SETTINGS_KEY, settings.model_dump())
    invalidate_cache()
    return settings


def is_queue_paused(session: Session) -> bool:
    return bool(get_raw(session, QUEUE_PAUSED_KEY, False))
