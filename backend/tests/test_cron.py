"""Syncing on a schedule without an always-on server (Vercel Cron), within time limits."""
import time

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database
import get_transactions
import scheduler as scheduler_module
from conftest import seed_user, txn
from scheduler import Scheduler, SyncBusy

ALICE_TOKEN = "access-sandbox-item-a"


def page(added=(), cursor="next", more=False):
    return {"added": list(added), "modified": [], "removed": [], "next_cursor": cursor, "has_more": more}


def card(account_id):
    return {"account_id": account_id, "name": "Card", "type": "credit", "mask": "1111"}


# --- The shared, database-held lock -------------------------------------------

def test_two_copies_of_the_app_never_sync_the_same_person_at_once():
    seed_user("alice", "item-a", ["acct-a1"])
    seed_user("bob", "item-b", ["acct-b1"])
    copy_one, copy_two = Scheduler(), Scheduler()  # e.g. two Vercel instances

    def slow_sync(user_id, deadline=None):
        # While copy one is mid-sync for Alice, copy two can't start hers, but can do Bob's.
        with pytest.raises(SyncBusy):
            copy_two.run_now("alice")
        assert copy_two.run_now("bob") == "bob synced"
        assert copy_two.status("alice")["running"] is True
        return "alice synced"

    copy_two._sync = lambda user_id, deadline=None: f"{user_id} synced"
    copy_one._sync = slow_sync
    assert copy_one.run_now("alice") == "alice synced"
    assert copy_two.status("alice")["running"] is False  # released afterwards


def test_the_lock_is_released_even_when_a_sync_fails():
    seed_user("alice", "item-a", ["acct-a1"])

    def boom(user_id, deadline=None):
        raise RuntimeError("plaid down")

    s = Scheduler(sync=boom)
    with pytest.raises(RuntimeError):
        s.run_now("alice")
    s._sync = lambda user_id, deadline=None: "ok"
    assert s.run_now("alice") == "ok"


def test_a_crashed_copys_lock_expires():
    seed_user("alice", "item-a", ["acct-a1"])
    with database.connect() as conn:
        assert database.try_lock_user(conn, "alice", 600)  # a copy took it, then died
        assert not database.try_lock_user(conn, "alice", 600)
        conn.execute("UPDATE sync_locks SET locked_until = now() - interval '1 second'")  # lease ran out
        assert database.try_lock_user(conn, "alice", 600)


def test_removing_a_bank_waits_out_a_running_sync_then_gives_up(monkeypatch):
    seed_user("alice", "item-a", ["acct-a1"])
    monkeypatch.setattr(scheduler_module, "PAUSE_WAIT", 0.3)
    with database.connect() as conn:
        database.try_lock_user(conn, "alice", 600)
    with pytest.raises(SyncBusy):
        with Scheduler().paused("alice"):
            pass


# --- Time budgets: stop cleanly, carry on next time ---------------------------

def test_a_sync_out_of_time_stops_between_pages_and_resumes_next_time(fake_plaid, monkeypatch):
    seed_user("alice", "item-a", ["acct-a1"])
    fake_plaid.accounts[ALICE_TOKEN] = [card("acct-a1")]
    fake_plaid.pages[ALICE_TOKEN] = [
        page([txn("p1", "acct-a1", 1.00)], cursor="c1", more=True),
        page([txn("p2", "acct-a1", 2.00)], cursor="c2", more=True),
        page([txn("p3", "acct-a1", 3.00)], cursor="c3", more=False),
    ]
    # Each Plaid page takes 0.3s and the budget is 0.2s: time runs out during page one.
    plaid_post = fake_plaid.post

    def slow_post(path, body=None, timeout=30):
        if path == "/transactions/sync":
            time.sleep(0.3)
        return plaid_post(path, body, timeout)

    monkeypatch.setattr(get_transactions.plaid_client, "post", slow_post)
    first = get_transactions.sync_and_log("alice", deadline=time.monotonic() + 0.2)
    assert first == {"added": 1, "modified": 0, "removed": 0, "complete": False}
    with database.connect() as conn:
        assert database.get_cursor(conn, "item-a") == "c1"  # first page saved with its cursor
        assert database.last_sync_attempt(conn, "alice")["complete"] is False

    s = Scheduler()
    s.set_interval("alice", "1w")
    assert s._is_due("alice")  # an unfinished import is due right away, whatever the interval
    assert s.status("alice")["last_attempt"]["complete"] is False

    second = get_transactions.sync_and_log("alice")  # no deadline: finishes
    assert second == {"added": 2, "modified": 0, "removed": 0, "complete": True}
    with database.connect() as conn:
        assert database.spending_summary(conn, "alice") == (3, 600)
        assert database.get_cursor(conn, "item-a") == "c3"
    assert not s._is_due("alice")  # done; back to the weekly schedule


def test_run_due_stops_starting_new_people_when_out_of_time():
    for name in ("alice", "bob", "carol"):
        seed_user(name, f"item-{name}", [f"acct-{name}"])
    synced = []
    s = Scheduler(sync=lambda user_id, deadline=None: synced.append(user_id) or {})
    summary = s.run_due(deadline=time.monotonic() - 1)
    assert synced == [] and summary["out_of_time"] is True and summary["checked"] == 0
    summary = s.run_due()
    assert sorted(synced) == ["alice", "bob", "carol"]
    assert summary == {"checked": 3, "synced": 3, "failed": 0, "busy": 0, "out_of_time": False}


# --- The cron endpoint ----------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app_module.app)


def test_cron_endpoint_is_off_without_a_secret(client, monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert client.get("/api/cron/sync").status_code == 404


def test_cron_endpoint_needs_the_exact_secret(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret-value-for-tests")
    assert client.get("/api/cron/sync").status_code == 401
    assert client.get("/api/cron/sync", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/cron/sync", headers={"Authorization": "Bearer s3cret-value-for-test"}).status_code == 401


def test_cron_runs_due_syncs_and_reveals_no_data(client, monkeypatch, fake_plaid):
    monkeypatch.setenv("CRON_SECRET", "s3cret-value-for-tests")
    seed_user("alice", "item-a", ["acct-a1"], bank="Alice's Bank")
    fake_plaid.accounts[ALICE_TOKEN] = [card("acct-a1")]
    fake_plaid.pages[ALICE_TOKEN] = [page([txn("new", "acct-a1", 4.20)])]

    response = client.get("/api/cron/sync", headers={"Authorization": "Bearer s3cret-value-for-tests"})
    assert response.status_code == 200
    assert response.json() == {"checked": 1, "synced": 1, "failed": 0, "busy": 0, "out_of_time": False}
    assert "alice" not in response.text.lower()
    with database.connect() as conn:
        assert "new" in {t["transaction_id"] for t in database.recent_transactions(conn, "alice")}


# --- Where the background thread runs ---------------------------------------------

@pytest.mark.parametrize("env, expected", [
    ({}, True),                                    # your Mac: always-on server
    ({"VERCEL": "1"}, False),                      # Vercel: Cron drives syncing instead
    ({"VERCEL": "1", "RUN_SCHEDULER": "1"}, True),  # explicit override
    ({"RUN_SCHEDULER": "0"}, False),
])
def test_background_thread_only_where_a_server_stays_up(monkeypatch, env, expected):
    for name in ("VERCEL", "RUN_SCHEDULER"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert scheduler_module.background_thread_enabled() is expected
