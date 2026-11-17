"""SQLAlchemy models.

Terminology:
* ``Manga`` – a MangaDex title the user follows.
* ``LogicalChapter`` – "chapter 12 of manga X", independent of language/group.
* ``ChapterRelease`` – one published MangaDex chapter entity (UUID), i.e. a specific
  translation by specific group(s). Releases with the same number are never merged.
* ``DownloadJob`` – the persistent state machine that downloads one release.
* ``PageRecord`` – per-page progress for a release, so interrupted downloads resume.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import (
    OPEN_JOB_STATUSES,
    GroupMode,
    JobStatus,
    LocalStatus,
    PageStatus,
    ReviewStatus,
    ScanStatus,
)

JSONType = JSON().with_variant(JSONB(), "postgresql")


def _in_list(values) -> str:
    return ", ".join(f"'{v.value if hasattr(v, 'value') else v}'" for v in values)


def _now_col(**kw):
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


# --------------------------------------------------------------------------- auth


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _now_col(nullable=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    __tablename__ = "user_sessions"

    # SHA-256 of the session token; the raw token only ever lives in the cookie.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _now_col(nullable=False)
    last_seen_at: Mapped[datetime] = _now_col(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship()


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_username_time", "username", "attempted_at"),
        Index("ix_login_attempts_ip_time", "ip", "attempted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempted_at: Mapped[datetime] = _now_col(nullable=False)


# ----------------------------------------------------------------- reference data


class Language(Base):
    __tablename__ = "languages"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # MangaDex code, e.g. "en", "pt-br"
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    folder_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)


class ScanlationGroup(Base):
    __tablename__ = "scanlation_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    website: Mapped[str | None] = mapped_column(String(512))
    is_official_publisher: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = _now_col(nullable=False, onupdate=func.now())


# ------------------------------------------------------------------------- manga


class Manga(Base):
    __tablename__ = "manga"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)  # canonical MangaDex title
    title_override: Mapped[str | None] = mapped_column(Text)
    alt_titles: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    year: Mapped[int | None] = mapped_column(Integer)
    content_rating: Mapped[str | None] = mapped_column(String(32))
    original_language: Mapped[str | None] = mapped_column(String(16))
    last_volume: Mapped[str | None] = mapped_column(String(32))
    last_chapter: Mapped[str | None] = mapped_column(String(32))
    tags: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    authors: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    available_languages: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    cover_filename: Mapped[str | None] = mapped_column(String(255))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    # Resolved, collision-free top-level folder name. Kept stable once assigned.
    folder_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Which {Language}/{Group} levels exist below folder_name (see app.services.layout).
    folder_layout: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=lambda: {"language": False, "groups": []},
        server_default=text("""'{"language": false, "groups": []}'"""),
    )
    created_at: Mapped[datetime] = _now_col(nullable=False)
    updated_at: Mapped[datetime] = _now_col(nullable=False, onupdate=func.now())
    metadata_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Added automatically from the MangaDex followed list: nothing is scanned or
    # downloaded until the user approves it (and picks languages, groups, start chapter).
    pending_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                   server_default=text("false"))
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_via: Mapped[str | None] = mapped_column(String(32))  # "manual" | "mangadex_follow"
    # Chapter numbering restarts in every volume (anthologies): chapters are identified
    # and ordered by volume + chapter (see app.services.numbering).
    chapters_per_volume: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                      server_default=text("false"))

    monitoring: Mapped[MonitoringConfig] = relationship(
        back_populates="manga", uselist=False, cascade="all, delete-orphan"
    )
    languages: Mapped[list[MangaLanguage]] = relationship(cascade="all, delete-orphan")
    group_subscriptions: Mapped[list[GroupSubscription]] = relationship(cascade="all, delete-orphan")

    @property
    def display_title(self) -> str:
        return self.title_override or self.title


class MonitoringConfig(Base):
    __tablename__ = "monitoring_configs"
    __table_args__ = (
        CheckConstraint(f"group_mode IN ({_in_list(GroupMode)})", name="ck_monitoring_group_mode"),
        CheckConstraint("interval_minutes IS NULL OR interval_minutes >= 30", name="ck_monitoring_interval"),
    )

    manga_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), primary_key=True
    )
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_download: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    interval_minutes: Mapped[int | None] = mapped_column(Integer)  # NULL -> global default
    group_mode: Mapped[str] = mapped_column(String(16), nullable=False, default=GroupMode.ALL)
    include_unknown_group: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Set when language selection changes -> next scan must be a full scan.
    needs_full_scan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Only releases numbered >= this chapter are wanted (NULL = all). Unnumbered
    # releases (one-shots, specials) are not affected.
    start_chapter: Mapped[float | None] = mapped_column(Numeric(12, 4))
    # When the user last set ``start_chapter``. Chapter numbers that appear on MangaDex
    # after this moment (late extras such as x.5, chapters skipped and uploaded later)
    # are wanted even when they are numbered below the start.
    start_chapter_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Download at most one release per chapter number (first come wins). Used for
    # series imported from another tool where the chapter, not the group, matters.
    one_release_per_chapter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Adaptive monitoring (app.services.tiers): 1 = active, 2 = slow, 3 = dormant/complete.
    update_tier: Mapped[int | None] = mapped_column(SmallInteger)
    # Every chapter up to the manga's final chapter has been uploaded (incl. DMCA-removed ones).
    upload_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                  server_default=text("false"))
    last_chapter_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prev_chapter_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tier_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    manga: Mapped[Manga] = relationship(back_populates="monitoring")


class MangaLanguage(Base):
    __tablename__ = "manga_languages"

    manga_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), primary_key=True
    )
    language_code: Mapped[str] = mapped_column(ForeignKey("languages.code"), primary_key=True)


class GroupSubscription(Base):
    __tablename__ = "group_subscriptions"

    manga_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scanlation_groups.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = _now_col(nullable=False)

    group: Mapped[ScanlationGroup] = relationship()


# ---------------------------------------------------------------------- chapters


class LogicalChapter(Base):
    __tablename__ = "logical_chapters"
    __table_args__ = (UniqueConstraint("manga_id", "chapter_key", name="uq_logical_chapter_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manga_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Normalised identity: "c:12.5" for numbered chapters, "x:<release uuid>" for
    # releases without a chapter number (one-shots/specials are never merged).
    chapter_key: Mapped[str] = mapped_column(String(128), nullable=False)
    chapter: Mapped[str | None] = mapped_column(String(32))
    volume: Mapped[str | None] = mapped_column(String(32))
    sort_number: Mapped[float | None] = mapped_column(Numeric(12, 4))
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="regular")


class ExternalChapter(Base):
    """A chapter the user already owns outside this library (e.g. downloaded by JAM).

    Its chapter number (``chapter_key``) is never downloaded again, whatever the
    language or group. When external chapters exist, releases numbered below
    ``start_chapter`` (or without a number) are only wanted if they were published
    after the newest external chapter: that catches late extras (x.5) and re-uploads
    without re-downloading the old backlog.
    """

    __tablename__ = "external_chapters"
    __table_args__ = (UniqueConstraint("manga_id", "source", "release_id", name="uq_external_chapter_release"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manga_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="jam")
    release_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    language_code: Mapped[str | None] = mapped_column(String(16))
    chapter: Mapped[str | None] = mapped_column(String(32))
    chapter_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChapterRelease(Base):
    __tablename__ = "chapter_releases"
    __table_args__ = (
        Index("ix_release_manga_lang", "manga_id", "language_code"),
        CheckConstraint(f"local_status IN ({_in_list(LocalStatus)})", name="ck_release_local_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # MangaDex chapter UUID
    manga_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), nullable=False
    )
    logical_chapter_id: Mapped[int] = mapped_column(
        ForeignKey("logical_chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language_code: Mapped[str] = mapped_column(String(16), nullable=False)
    volume: Mapped[str | None] = mapped_column(String(32))
    chapter: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(Text)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int | None] = mapped_column(Integer)
    external_url: Mapped[str | None] = mapped_column(Text)
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    readable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    md_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    group_ids: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    group_names: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    is_unavailable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    first_seen_at: Mapped[datetime] = _now_col(nullable=False)
    last_seen_at: Mapped[datetime] = _now_col(nullable=False)

    # Local library state
    local_status: Mapped[str] = mapped_column(String(32), nullable=False, default=LocalStatus.MISSING)
    folder_rel_path: Mapped[str | None] = mapped_column(Text, unique=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    page_manifest: Mapped[list[Any] | None] = mapped_column(JSONType)
    local_page_count: Mapped[int | None] = mapped_column(Integer)
    local_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # MangaDex ``version`` of the chapter when its pages were downloaded; a higher
    # remote version triggers a page check (same chapter UUID, pages replaced).
    downloaded_version: Mapped[int | None] = mapped_column(Integer)

    logical_chapter: Mapped[LogicalChapter] = relationship()
    manga: Mapped[Manga] = relationship()

    @property
    def is_external(self) -> bool:
        # Official publishers' releases link to their site and carry at most a single
        # placeholder page ("read it on ..."), so they are not real chapters.
        return bool(self.external_url) and (self.pages or 0) <= 1


class PageRecord(Base):
    __tablename__ = "page_records"
    __table_args__ = (
        UniqueConstraint("release_id", "page_index", name="uq_page_release_index"),
        CheckConstraint(f"status IN ({_in_list(PageStatus)})", name="ck_page_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapter_releases.id", ondelete="CASCADE"), nullable=False
    )
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based, original order
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    local_filename: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PageStatus.PENDING)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------- jobs


class DownloadJob(Base):
    __tablename__ = "download_jobs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in_list(JobStatus)})", name="ck_job_status"),
        # At most one open job per release: prevents duplicate downloads.
        Index(
            "uq_job_open_per_release",
            "release_id",
            unique=True,
            postgresql_where=text(f"status IN ({_in_list(OPEN_JOB_STATUSES)})"),
            sqlite_where=text(f"status IN ({_in_list(OPEN_JOB_STATUSES)})"),
        ),
        Index("ix_job_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapter_releases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=JobStatus.PENDING)
    pause_reason: Mapped[str | None] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    error_kind: Mapped[str | None] = mapped_column(String(32))
    temp_dir_rel: Mapped[str | None] = mapped_column(Text)
    target_rel_path: Mapped[str | None] = mapped_column(Text)
    pages_total: Mapped[int | None] = mapped_column(Integer)
    pages_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_redownload: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Version check: compare MangaDex's page list with the downloaded one first and
    # only download (replacing the folder) when the pages actually changed.
    is_refresh: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Same-group re-upload: the older release whose folder this download replaces.
    replaces_release_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapter_releases.id", ondelete="SET NULL", name="fk_download_jobs_replaces_release")
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    rq_job_id: Mapped[str | None] = mapped_column(String(128))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)
    updated_at: Mapped[datetime] = _now_col(nullable=False, onupdate=func.now())

    release: Mapped[ChapterRelease] = relationship(foreign_keys=[release_id])


class JobEvent(Base):
    """Download history / audit trail."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("download_jobs.id", ondelete="SET NULL"), index=True)
    release_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapter_releases.id", ondelete="CASCADE"), index=True
    )
    manga_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col(nullable=False, index=True)


class MonitoringRun(Base):
    __tablename__ = "monitoring_runs"
    __table_args__ = (CheckConstraint(f"status IN ({_in_list(ScanStatus)})", name="ck_scan_status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    manga_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    full_scan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ScanStatus.RUNNING)
    started_at: Mapped[datetime] = _now_col(nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    releases_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_releases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requests_made: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    manga: Mapped[Manga] = relationship()


class MangaDexFollow(Base):
    """Mirror of the MangaDex account's followed list (refreshed by the follow sync).

    ``data`` keeps the MangaDex manga entity, so a dismissed title can be restored
    without another request. ``dismissed_at`` = the user rejected it; it is never
    proposed again unless restored.
    """

    __tablename__ = "mangadex_follows"

    manga_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    reading_status: Mapped[str | None] = mapped_column(String(32))
    followed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = _now_col(nullable=False)
    last_seen_at: Mapped[datetime] = _now_col(nullable=False)
    unfollowed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArchiveFolder(Base):
    """A title folder of the read-only comics archive (see app.services.archive).

    ``files`` are the archive names directly inside the folder; ``analysis`` caches
    the newest chapter found there and is cleared whenever the file list changes.
    """

    __tablename__ = "archive_folders"

    rel_path: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_norm: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    language: Mapped[str | None] = mapped_column(String(16))
    files: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    files_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    # Folder from archive_custom_dirs: only its "… - Custom" archives are listed.
    custom_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    indexed_at: Mapped[datetime] = _now_col(nullable=False)


class VolumeArchive(Base):
    """A volume CBZ built by the app (see app.services.volumes).

    The chapters packed into it had their folders (or chapter CBZ files) deleted; their
    releases are *Folder deleted* like chapters moved out by hand, and ``release_ids``
    tells the UI they are in this volume.
    """

    __tablename__ = "volume_archives"
    __table_args__ = (UniqueConstraint("manga_id", "language_code", "volume", name="uq_volume_archive"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    manga_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), nullable=False, index=True
    )
    language_code: Mapped[str] = mapped_column(String(16), nullable=False)
    volume: Mapped[str] = mapped_column(String(32), nullable=False)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    release_ids: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    chapters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    automatic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _now_col(nullable=False)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONType, nullable=False)
    updated_at: Mapped[datetime] = _now_col(nullable=False, onupdate=func.now())


class ReviewItem(Base):
    """Library import / reconciliation findings that need a human decision."""

    __tablename__ = "review_items"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in_list(ReviewStatus)})", name="ck_review_status"),
        Index(
            "uq_review_open_path",
            "path",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    manga_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    candidates: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ReviewStatus.OPEN)
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col(nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LibraryScanRun(Base):
    __tablename__ = "library_scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manga_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("manga.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ScanStatus.RUNNING)
    started_at: Mapped[datetime] = _now_col(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "AppSetting",
    "ArchiveFolder",
    "ChapterRelease",
    "DownloadJob",
    "Float",
    "GroupSubscription",
    "JobEvent",
    "Language",
    "LibraryScanRun",
    "LoginAttempt",
    "MangaDexFollow",
    "LogicalChapter",
    "Manga",
    "MangaLanguage",
    "MonitoringConfig",
    "MonitoringRun",
    "PageRecord",
    "ReviewItem",
    "ScanlationGroup",
    "User",
    "UserSession",
]
