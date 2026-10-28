import logging
import threading
from contextlib import closing
from datetime import datetime, timedelta

import database
from get_transactions import sync_and_log

log = logging.getLogger(__name__)

# Seconds between syncs; None means manual only.
INTERVALS = {"1h": 3600, "1d": 86400, "1w": 604800, "off": None}
DEFAULT_INTERVAL = "1d"
SETTING_KEY = "refresh_interval"
CHECK_EVERY = 30  # seconds between "is a sync due?" checks


class SyncBusy(Exception):
    """A sync is already in progress."""


class Scheduler:
    """Runs syncs on the saved interval from a background thread.

    Due-ness is judged against the time of the last attempt stored in the
    database, so it survives restarts and catches up after the Mac sleeps.
    """

    def __init__(self, sync=sync_and_log):
        self._sync = sync
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sync-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def run_now(self):
        """Run a sync immediately. Raises SyncBusy if one is already running."""
        if not self._lock.acquire(blocking=False):
            raise SyncBusy()
        try:
            return self._sync()
        finally:
            self._lock.release()

    def set_interval(self, interval):
        if interval not in INTERVALS:
            raise ValueError(f"interval must be one of {list(INTERVALS)}")
        with closing(database.connect()) as conn, conn:
            database.set_setting(conn, SETTING_KEY, interval)

    def status(self):
        with closing(database.connect()) as conn:
            interval = self._interval(conn)
            last_attempt = database.last_sync_attempt(conn)
            last_success = database.last_sync_attempt(conn, "success")
        return {
            "interval": interval,
            "intervals": list(INTERVALS),
            "running": self._lock.locked(),
            "last_attempt": last_attempt,
            "last_success_at": last_success["finished_at"] if last_success else None,
            "next_run_at": self._next_run(interval, last_attempt),
        }

    def _interval(self, conn):
        interval = database.get_setting(conn, SETTING_KEY, DEFAULT_INTERVAL)
        return interval if interval in INTERVALS else DEFAULT_INTERVAL

    def _next_run(self, interval, last_attempt):
        seconds = INTERVALS[interval]
        if seconds is None or last_attempt is None:
            return None
        finished = datetime.fromisoformat(last_attempt["finished_at"])
        return (finished + timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def _is_due(self):
        with closing(database.connect()) as conn:
            interval = self._interval(conn)
            last_attempt = database.last_sync_attempt(conn)
        if INTERVALS[interval] is None:
            return False
        if last_attempt is None:
            return True
        # Failed attempts count too, so an outage retries at the normal
        # interval instead of hammering Plaid every 30 seconds.
        return datetime.fromisoformat(self._next_run(interval, last_attempt)) <= datetime.now().astimezone()

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self._is_due():
                    self.run_now()
            except SyncBusy:
                pass
            except Exception:
                # sync_and_log already recorded the failure for the dashboard.
                log.exception("Scheduled sync failed")
            self._stop.wait(CHECK_EVERY)
