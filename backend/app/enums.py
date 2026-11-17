from __future__ import annotations

import enum


class JobStatus(enum.StrEnum):
    PENDING = "pending"  # created, waiting for a dispatch slot
    QUEUED = "queued"  # handed to the worker queue
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"  # retries exhausted
    NEEDS_ATTENTION = "needs_attention"  # permanent error, user action required
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = (
    JobStatus.PENDING,
    JobStatus.QUEUED,
    JobStatus.DOWNLOADING,
    JobStatus.PAUSED,
    JobStatus.RETRY_SCHEDULED,
)
# Statuses that keep the "one open job per release" constraint.
OPEN_JOB_STATUSES = (*ACTIVE_JOB_STATUSES, JobStatus.FAILED, JobStatus.NEEDS_ATTENTION)


class PauseReason(enum.StrEnum):
    USER = "user"  # paused individually; resumes only on user request
    QUEUE = "queue"  # global queue pause; resumes when the queue is resumed
    STORAGE = "storage"  # library unavailable; resumes automatically
    BLOCKED = "blocked"  # MangaDex access restricted; resumes after acknowledgement


class ErrorKind(enum.StrEnum):
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    MAINTENANCE = "maintenance"
    NOT_FOUND = "not_found"
    INVALID_RESPONSE = "invalid_response"
    ACCESS = "access"
    INVALID_IMAGE = "invalid_image"
    STORAGE = "storage"
    CONFLICT = "conflict"
    INTERNAL = "internal"


TRANSIENT_ERRORS = {
    ErrorKind.NETWORK,
    ErrorKind.RATE_LIMIT,
    ErrorKind.SERVER,
    ErrorKind.MAINTENANCE,
    ErrorKind.INVALID_RESPONSE,
    ErrorKind.INVALID_IMAGE,
    ErrorKind.INTERNAL,
}


class LocalStatus(enum.StrEnum):
    """What we know about a release on disk."""

    MISSING = "missing"  # not on disk
    DOWNLOADED = "downloaded"  # downloaded by this app, folder intact
    IMPORTED = "imported"  # matched from an existing library folder
    MODIFIED = "modified"  # folder exists but differs from the original manifest (user edits)
    MISSING_ON_DISK = "missing_on_disk"  # was downloaded/imported, folder now gone
    SKIPPED = "skipped"  # user chose not to download
    EXTERNAL = "external"  # already owned outside this library (e.g. imported from JAM)
    REPLACED = "replaced"  # superseded by a newer upload of the same group; old folder moved to the replaced folder
    # Same-group re-upload of a chapter already moved out of the library (within the revision window):
    # downloaded straight into the replaced folder, never into the library.
    IN_REPLACED = "in_replaced"


HAVE_LOCAL_STATUSES = (
    LocalStatus.DOWNLOADED,
    LocalStatus.IMPORTED,
    LocalStatus.MODIFIED,
    LocalStatus.MISSING_ON_DISK,
    LocalStatus.SKIPPED,
    LocalStatus.REPLACED,
    LocalStatus.IN_REPLACED,
)


class PageStatus(enum.StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class GroupMode(enum.StrEnum):
    ALL = "all"
    SELECTED = "selected"


class ScanTrigger(enum.StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    SETTINGS_CHANGE = "settings_change"
    ADDED = "added"


class ScanStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ReviewKind(enum.StrEnum):
    UNMATCHED_FOLDER = "unmatched_folder"
    AMBIGUOUS_MATCH = "ambiguous_match"
    UNKNOWN_MANGA_FOLDER = "unknown_manga_folder"
    DESTINATION_CONFLICT = "destination_conflict"
    # Not a problem: a revision of a chapter the user had moved out was saved in the replaced folder.
    REVISION_AFTER_MOVE = "revision_after_move"


class ReviewStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
