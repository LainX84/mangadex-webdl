"""Dedicated scheduler process.

Every few seconds it:
* checks that the library mount is present and writable (publishes status);
* restores the MangaDex access-block state from the database into Redis;
* recovers jobs orphaned by worker crashes or a flushed Redis;
* dispatches pending / due-retry download jobs within the concurrency limit;
* starts at most one due monitoring scan, spaced out over time;
* queues the MangaDex account syncs (followed list, followed feed) when due;
* about once an hour, queues the removal of junk files (``Thumbs.db``) from the library;
* once a week (Sunday 05:00 by default), queues the library check that marks chapters moved out
  of the library by hand (``app.services.library_check``).

All schedules live in PostgreSQL, so restarts lose nothing.
"""

from __future__ import annotations

import signal
import threading
import time

from app.config import get_config
from app.db import session_scope
from app.logging_setup import configure_logging, get_logger

log = get_logger("scheduler")

TICK_SECONDS = 10
MOUNT_CHECK_SECONDS = 30
MAINTENANCE_SECONDS = 60
TIER_REFRESH_SECONDS = 3600
FOLLOW_CHECK_SECONDS = 30


class Scheduler:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self._last_mount = 0.0
        self._last_maint = 0.0
        self._last_tiers = 0.0
        self._last_follow = 0.0
        self.storage_ok = False

    def tick(self) -> None:
        from app.services.control import heartbeat, refresh_mount_status, sync_access_block_to_redis
        from app.services.jobs import dispatch_jobs, recover_orphans, schedule_due_scans

        heartbeat("scheduler")
        now = time.monotonic()
        if now - self._last_mount >= MOUNT_CHECK_SECONDS:
            st = refresh_mount_status()
            if st.ok != self.storage_ok:
                log.info("library_status_changed", ok=st.ok, error=st.error)
            self.storage_ok = st.ok
            self._last_mount = now
        if now - self._last_maint >= MAINTENANCE_SECONDS:
            with session_scope() as s:
                sync_access_block_to_redis(s)
                n = recover_orphans(s)
                if n:
                    log.info("jobs_recovered", count=n)
                _purge_old_rows(s)
            self._last_maint = now
        if now - self._last_tiers >= TIER_REFRESH_SECONDS or self._last_tiers == 0.0:
            # Levels decay with time even without scans (no new chapter for months).
            from app.services.tiers import refresh_tiers

            self._last_tiers = now
            try:
                with session_scope() as s:
                    refresh_tiers(s)
            except Exception:
                log.exception("tier_refresh_failed")
        with session_scope() as s:
            dispatch_jobs(s, self.storage_ok)
        with session_scope() as s:
            schedule_due_scans(s)
        if now - self._last_follow >= FOLLOW_CHECK_SECONDS:
            from app.services.follows import schedule_follow_syncs

            self._last_follow = now
            try:
                with session_scope() as s:
                    started = schedule_follow_syncs(s)
                if started:
                    log.info("follow_sync_queued", kinds=started)
            except Exception:
                log.exception("follow_sync_schedule_failed")
            try:
                from app.services.archive import enqueue_index, index_due

                with session_scope() as s:
                    due = index_due(s)
                if due and enqueue_index():
                    log.info("archive_index_queued")
            except Exception:
                log.exception("archive_index_schedule_failed")
            if self.storage_ok:
                try:
                    from app.services.library_check import check_due
                    from app.tasks import enqueue_library_scan

                    with session_scope() as s:
                        due = check_due(s)
                    if due and enqueue_library_scan(None, queue_missing=False):
                        log.info("weekly_library_check_queued")
                except Exception:
                    log.exception("weekly_library_check_schedule_failed")
                try:
                    from app.services.junk import enqueue_cleanup

                    if enqueue_cleanup():
                        log.info("junk_cleanup_queued")
                except Exception:
                    log.exception("junk_cleanup_schedule_failed")

    def run(self) -> None:
        log.info("scheduler_started")
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler_tick_failed")
            self.stop.wait(TICK_SECONDS)
        log.info("scheduler_stopped")


def _purge_old_rows(session) -> None:
    """Housekeeping for unbounded tables (sessions, login attempts)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import delete

    from app.models import LoginAttempt, UserSession

    now = datetime.now(UTC)
    session.execute(delete(UserSession).where(UserSession.expires_at < now))
    session.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < now - timedelta(days=30)))


def main() -> None:
    cfg = get_config()
    configure_logging(cfg.log_level, cfg.log_json)
    sched = Scheduler()

    def _stop(*_):
        sched.stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sched.run()


if __name__ == "__main__":
    main()
