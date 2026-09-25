"""
Keep the job store current from inside the web process.

In Kubernetes a CronJob runs ingest. A single hosted instance (Render, Railway)
has no scheduler, and on a free tier its disk is wiped on every restart - so
with JOB_REFRESH_HOURS set, the app fetches every configured board on startup
and again every N hours, on a background thread.

Gunicorn may run several worker processes. Only one of them should hit the job
boards, so the first to take an exclusive lock on a file next to the database
becomes the refresher and holds the lock for its lifetime; the others skip.
"""

import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from ..config import Config
from ..llm import LLMUnavailable
from .store import JobStore

try:
    import fcntl
except ImportError:  # Windows: no flock, and no multi-process gunicorn either
    fcntl = None

logger = logging.getLogger(__name__)

_started = False
_lock_handle = None   # kept open for the process lifetime; closing it drops the lock


def _status_path(db_path: str) -> Path:
    return Path(db_path).with_suffix(".refresh.json")


def _write_status(db_path: str, **status) -> None:
    # A file rather than a module global: the page may be served by a gunicorn
    # worker other than the one refreshing, and it must see the same status.
    try:
        _status_path(db_path).write_text(json.dumps(status), encoding="utf-8")
    except OSError:
        logger.warning("could not write refresh status")


def refresh_status(db_path: str) -> dict:
    """Whether a refresh is in flight and when the last one finished, for the UI."""
    try:
        data = json.loads(_status_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"refreshing": bool(data.get("refreshing")), "last_refresh": data.get("last_refresh")}


def _take_refresher_lock(db_path: str) -> bool:
    global _lock_handle
    if fcntl is None:
        return True
    path = Path(db_path).with_suffix(".refresh.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")  # noqa: SIM115 - must outlive this function
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _lock_handle = handle
    return True


def refresh_once(config: Config, store: JobStore) -> None:
    """Fetch every board, drop postings no longer listed, embed what is new."""
    from .ingest import embed_missing, fetch_and_store

    previous = refresh_status(config.job_db_path)["last_refresh"]
    _write_status(config.job_db_path, refreshing=True, last_refresh=previous)
    started = time.perf_counter()
    try:
        inserted, updated = fetch_and_store(store)
        # A posting not seen in two refresh cycles has been taken down.
        stale_days = max(1, round(2 * config.job_refresh_hours / 24))
        removed = store.delete_older_than(stale_days)
        embedded = 0
        if config.gemini_api_key:
            try:
                embedded = embed_missing(store, config, batch_limit=200)
            except LLMUnavailable:
                pass
        logger.info("job refresh complete", extra={
            "inserted": inserted, "updated": updated, "removed": removed,
            "embedded": embedded, "event.duration_ms": int((time.perf_counter() - started) * 1000),
        })
    except Exception:
        logger.exception("job refresh failed")
    finally:
        _write_status(config.job_db_path, refreshing=False,
                      last_refresh=datetime.now(UTC).isoformat())


def start_background_refresh(config: Config) -> bool:
    """
    Start the refresh loop if this process wins the refresher lock.

    Returns whether this process is the refresher. Safe to call more than once.
    """
    global _started
    if _started or config.job_refresh_hours <= 0:
        return False
    if not _take_refresher_lock(config.job_db_path):
        return False
    _started = True
    store = JobStore(config.job_db_path)
    interval = config.job_refresh_hours * 3600

    def loop():
        while True:
            refresh_once(config, store)
            time.sleep(interval)

    threading.Thread(target=loop, name="job-refresh", daemon=True).start()
    logger.info("background job refresh started",
                extra={"interval_hours": config.job_refresh_hours})
    return True
