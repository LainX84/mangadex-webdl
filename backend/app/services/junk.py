"""Periodic removal of junk files that file managers leave in the library (Windows ``Thumbs.db``).

Runs on the ``default`` queue about once an hour (see ``app.scheduler``). It walks the library, the staging
folder and the replaced folder without following symlinks and deletes only regular files whose name is in
``JUNK_FILE_NAMES`` (case-insensitive). Nothing else is ever touched, and nothing runs while the library check
fails (an unmounted NAS share is never walked).
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import get_config
from app.library.storage import check_library, replaced_root, staging_root
from app.logging_setup import get_logger

log = get_logger(__name__)

JUNK_FILE_NAMES = frozenset({"thumbs.db"})
PENDING_KEY = "mdm:junk_cleanup"
INTERVAL_SECONDS = 3600


def _roots() -> list[Path]:
    lib = Path(get_config().downloads_root)
    out: list[Path] = []
    for p in (lib, staging_root(lib), replaced_root(lib)):
        if p.is_dir() and not any(p == r or r in p.parents for r in out):
            out.append(p)
    return out


def remove_junk_files() -> dict:
    st = check_library(write_probe=False)
    if not st.ok:
        log.info("junk_cleanup_skipped", reason=st.error)
        return {"skipped": st.error, "removed": []}
    removed: list[str] = []
    errors = 0
    for root in _roots():
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                if name.lower() not in JUNK_FILE_NAMES:
                    continue
                path = Path(dirpath) / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    path.unlink()
                    removed.append(str(path))
                except OSError as exc:
                    errors += 1
                    log.warning("junk_cleanup_failed", path=str(path), error=exc.strerror or str(exc))
    if removed:
        log.info("junk_files_removed", count=len(removed), paths=removed[:50])
    return {"removed": removed, "errors": errors}


def enqueue_cleanup() -> bool:
    """Queue a cleanup unless one ran (or was queued) within the last hour."""
    from app.redis_client import get_redis
    from app.services.jobs import QUEUE_DEFAULT, get_queue

    r = get_redis()
    if not r.set(PENDING_KEY, "1", nx=True, ex=INTERVAL_SECONDS):
        return False
    try:
        get_queue(QUEUE_DEFAULT).enqueue("app.tasks.task_junk_cleanup", job_timeout=1800, result_ttl=3600,
                                         failure_ttl=7 * 86400)
    except Exception:
        r.delete(PENDING_KEY)
        raise
    return True
