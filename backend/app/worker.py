"""Worker supervisor: runs several RQ workers as child processes.

* ``MDM_DOWNLOAD_WORKERS`` processes consume the ``downloads`` queue. The number of
  chapters actually downloading at once is governed by the *Max concurrent
  chapter downloads* setting (the scheduler never dispatches more), so this only
  needs to be >= that setting's maximum (4).
* one process consumes ``scans`` and ``default`` (monitoring scans, library scans).
* one process consumes ``follows`` (MangaDex account sync), so the fast followed-feed
  check is never stuck behind a long library scan.

Children that die are restarted. Jobs interrupted by a crash are recovered by the
scheduler from the database.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import socket
import threading
import time

from app.config import get_config
from app.logging_setup import configure_logging, get_logger

log = get_logger("worker")


def _child(queues: list[str], name: str) -> None:
    cfg = get_config()
    configure_logging(cfg.log_level, cfg.log_json)
    from rq import SimpleWorker

    from app.redis_client import get_redis

    worker = SimpleWorker(queues, connection=get_redis(), name=f"{name}-{socket.gethostname()}-{os.getpid()}")
    # Download processes run every job in-process (SimpleWorker) and their memory
    # grows by a few MB per chapter; exit cleanly after N jobs so the supervisor
    # starts a fresh process instead of the kernel OOM-killing one mid-chapter.
    max_jobs = None
    if "downloads" in queues:
        max_jobs = int(os.environ.get("MDM_DOWNLOAD_MAX_JOBS", "20")) or None
    worker.work(with_scheduler=("scans" in queues), logging_level=cfg.log_level.upper(), max_jobs=max_jobs)


def main() -> None:
    cfg = get_config()
    configure_logging(cfg.log_level, cfg.log_json)
    n_download = max(1, int(os.environ.get("MDM_DOWNLOAD_WORKERS", "4")))
    specs = [(["downloads"], f"dl{i}") for i in range(n_download)] + [(["scans", "default"], "scan"), (["follows"], "follow")]
    ctx = mp.get_context("spawn")
    procs: dict[str, mp.Process] = {}
    stopping = threading.Event()

    def start(queues, name):
        p = ctx.Process(target=_child, args=(queues, name), name=name, daemon=False)
        p.start()
        procs[name] = p
        log.info("worker_started", name=name, queues=queues, pid=p.pid)

    def shutdown(signum, _frame):
        log.info("worker_shutdown", signal=signum)
        stopping.set()
        for p in procs.values():
            if p.is_alive():
                os.kill(p.pid, signal.SIGTERM)  # RQ warm shutdown

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for queues, name in specs:
        start(queues, name)

    from app.services.control import heartbeat

    while not stopping.is_set():
        try:
            heartbeat("worker")
        except Exception:
            log.warning("worker_heartbeat_failed")
        for queues, name in specs:
            p = procs.get(name)
            if p is not None and not p.is_alive() and not stopping.is_set():
                if p.exitcode == 0:
                    log.info("worker_recycled", name=name)  # reached its job limit
                else:
                    log.warning("worker_restarting", name=name, exitcode=p.exitcode)
                    time.sleep(2)
                start(queues, name)
        stopping.wait(10)
    for p in procs.values():
        p.join(timeout=25)
        if p.is_alive():
            p.kill()


if __name__ == "__main__":
    main()
