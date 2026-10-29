import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import database
from get_transactions import sync_and_log

log = logging.getLogger(__name__)

# Seconds between syncs; None means manual only.
INTERVALS = {"1h": 3600, "1d": 86400, "1w": 604800, "off": None}
DEFAULT_INTERVAL = "1d"
SETTING_KEY = "refresh_interval"
CHECK_EVERY = 30  # seconds between "is a sync due?" checks
# After a failed sync, retry sooner than the normal interval so a brief outage
# doesn't leave a weekly schedule without data for a week.
RETRY_AFTER_ERROR = 1800  # seconds


class SyncBusy(Exception):
    """A sync is already in progress for this user."""


class Scheduler:
    """Runs each user's syncs on their own saved interval from a background thread.

    Due-ness is judged against the time of the user's last attempt stored in
    the database, so it survives restarts and catches up after downtime. Each
    user has their own lock: one person's sync never blocks another's.
    """

    def __init__(self, sync=sync_and_log):
        self._sync = sync
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _lock(self, user_id):
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sync-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def run_now(self, user_id):
        """Run the user's sync immediately. Raises SyncBusy if theirs is already running."""
        lock = self._lock(user_id)
        if not lock.acquire(blocking=False):
            raise SyncBusy()
        try:
            return self._sync(user_id)
        finally:
            lock.release()

    @contextmanager
    def paused(self, user_id):
        """Wait for the user's running sync to finish, then hold their syncs off
        (scheduled ones skip, Sync now reports busy) while their data is removed."""
        with self._lock(user_id):
            yield

    def set_interval(self, user_id, interval):
        if interval not in INTERVALS:
            raise ValueError(f"interval must be one of {list(INTERVALS)}")
        with database.connect() as conn:
            database.set_setting(conn, user_id, SETTING_KEY, interval)

    def status(self, user_id):
        with database.connect() as conn:
            interval = self._interval(conn, user_id)
            last_attempt = database.last_sync_attempt(conn, user_id)
            last_success = database.last_sync_attempt(conn, user_id, "success")
        next_run = self._next_run(interval, last_attempt)
        return {
            "interval": interval,
            "intervals": list(INTERVALS),
            "running": self._lock(user_id).locked(),
            "last_attempt": self._public(last_attempt),
            "last_success_at": database.iso(last_success["finished_at"]) if last_success else None,
            "next_run_at": database.iso(next_run),
        }

    @staticmethod
    def _public(attempt):
        if attempt is None:
            return None
        return {
            "status": attempt["status"],
            "started_at": database.iso(attempt["started_at"]),
            "finished_at": database.iso(attempt["finished_at"]),
            "added": attempt["added"],
            "modified": attempt["modified"],
            "removed": attempt["removed"],
            "error": attempt["error"],
        }

    def _interval(self, conn, user_id):
        interval = database.get_setting(conn, user_id, SETTING_KEY, DEFAULT_INTERVAL)
        return interval if interval in INTERVALS else DEFAULT_INTERVAL

    def _next_run(self, interval, last_attempt):
        seconds = INTERVALS[interval]
        if seconds is None or last_attempt is None:
            return None
        if last_attempt["status"] == "error":
            seconds = min(seconds, RETRY_AFTER_ERROR)
        return last_attempt["finished_at"] + timedelta(seconds=seconds)

    def _is_due(self, user_id):
        with database.connect() as conn:
            interval = self._interval(conn, user_id)
            last_attempt = database.last_sync_attempt(conn, user_id)
            has_banks = bool(database.list_items(conn, user_id))
        if INTERVALS[interval] is None or not has_banks:
            return False
        if last_attempt is None:
            return True
        # _next_run spaces out retries after a failure, so an outage doesn't
        # hammer Plaid every 30 seconds.
        return self._next_run(interval, last_attempt) <= datetime.now(timezone.utc)

    def run_due(self):
        """Sync every user whose sync is due. One user's failure doesn't stop the rest."""
        with database.connect() as conn:
            user_ids = database.list_user_ids(conn)
        for user_id in user_ids:
            try:
                if self._is_due(user_id):
                    self.run_now(user_id)
            except SyncBusy:
                pass
            except Exception:
                # sync_and_log already recorded the failure for that user's dashboard.
                log.exception("Scheduled sync failed for %s", user_id)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:
                log.exception("Scheduler check failed")
            self._stop.wait(CHECK_EVERY)
