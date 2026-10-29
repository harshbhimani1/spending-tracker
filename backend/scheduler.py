import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import database
from get_transactions import sync_and_log

log = logging.getLogger(__name__)

# Seconds between syncs; None means manual only.
INTERVALS = {"1h": 3600, "1d": 86400, "1w": 604800, "off": None}
DEFAULT_INTERVAL = "1d"
SETTING_KEY = "refresh_interval"
CHECK_EVERY = 30  # seconds between "is a sync due?" checks (local background thread)
# After a failed sync, retry sooner than the normal interval so a brief outage
# doesn't leave a weekly schedule without data for a week.
RETRY_AFTER_ERROR = 1800  # seconds
# How long a user's sync lease lasts. Longer than any single run, so a copy of
# the app that dies mid-sync only blocks that user until the lease runs out.
LOCK_SECONDS = 600
PAUSE_WAIT = 30  # seconds paused() waits for a running sync before giving up


class SyncBusy(Exception):
    """A sync is already in progress for this user."""


def background_thread_enabled():
    """The always-on background thread only makes sense on a server that stays up
    (your Mac). On Vercel nothing runs between requests, so Vercel Cron calls
    /api/cron/sync instead. RUN_SCHEDULER=1/0 overrides the default."""
    explicit = os.getenv("RUN_SCHEDULER")
    if explicit is not None:
        return explicit == "1"
    return not os.getenv("VERCEL")


class Scheduler:
    """Runs each user's syncs on their own saved interval.

    Due-ness is judged against the user's last attempt stored in the database,
    so it survives restarts. Each user has their own lock, held in the
    database so it also holds across several running copies of the app: one
    person's sync never blocks another's, and nobody's runs twice at once.
    """

    def __init__(self, sync=sync_and_log):
        self._sync = sync
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sync-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def run_now(self, user_id, deadline=None):
        """Run the user's sync immediately. Raises SyncBusy if theirs is already running."""
        with database.connect() as conn:
            if not database.try_lock_user(conn, user_id, LOCK_SECONDS):
                raise SyncBusy()
        try:
            return self._sync(user_id, deadline=deadline)
        finally:
            with database.connect() as conn:
                database.unlock_user(conn, user_id)

    @contextmanager
    def paused(self, user_id):
        """Wait (briefly) for the user's running sync to finish, then hold their
        syncs off while their data is removed. Raises SyncBusy if it doesn't finish."""
        give_up = time.monotonic() + PAUSE_WAIT
        while True:
            with database.connect() as conn:
                if database.try_lock_user(conn, user_id, LOCK_SECONDS):
                    break
            if time.monotonic() > give_up:
                raise SyncBusy()
            time.sleep(0.5)
        try:
            yield
        finally:
            with database.connect() as conn:
                database.unlock_user(conn, user_id)

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
            running = database.is_user_locked(conn, user_id)
        next_run = self._next_run(interval, last_attempt)
        return {
            "interval": interval,
            "intervals": list(INTERVALS),
            "running": running,
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
            "complete": attempt["complete"],
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
        elif not last_attempt["complete"]:
            seconds = 0  # an import that ran out of time carries on at the next chance
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
        # hammer Plaid on every check.
        return self._next_run(interval, last_attempt) <= datetime.now(timezone.utc)

    def run_due(self, deadline=None):
        """Sync every user whose sync is due, until the deadline (a time.monotonic()
        value). One user's failure doesn't stop the rest. Returns counts only."""
        summary = {"checked": 0, "synced": 0, "failed": 0, "busy": 0, "out_of_time": False}
        with database.connect() as conn:
            user_ids = database.list_user_ids(conn)
        for user_id in user_ids:
            if deadline is not None and time.monotonic() >= deadline:
                summary["out_of_time"] = True  # the rest are picked up next run
                break
            summary["checked"] += 1
            try:
                if self._is_due(user_id):
                    self.run_now(user_id, deadline=deadline)
                    summary["synced"] += 1
            except SyncBusy:
                summary["busy"] += 1
            except Exception:
                # sync_and_log already recorded the failure for that user's dashboard.
                summary["failed"] += 1
                log.exception("Scheduled sync failed for %s", user_id)
        return summary

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:
                log.exception("Scheduler check failed")
            self._stop.wait(CHECK_EVERY)
