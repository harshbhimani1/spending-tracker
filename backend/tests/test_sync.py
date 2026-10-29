"""Syncing writes each user's purchases to that user, safely, on their own schedule."""
import pytest

import database
import get_transactions
import plaid_client
from conftest import seed_user, txn
from scheduler import Scheduler, SyncBusy

ALICE_TOKEN = "access-sandbox-item-a"
BOB_TOKEN = "access-sandbox-item-b"


def page(added=(), modified=(), removed=(), cursor="next", more=False):
    return {"added": list(added), "modified": list(modified),
            "removed": [{"transaction_id": i} for i in removed], "next_cursor": cursor, "has_more": more}


def credit(account_id):
    return {"account_id": account_id, "name": "Card", "type": "credit", "mask": account_id[-4:]}


def setup_two_users(fake):
    seed_user("alice", "item-a", ["acct-a1"], [txn("old", "acct-a1", 3.00)])
    seed_user("bob", "item-b", ["acct-b1"], [txn("bob-1", "acct-b1", 50.00)])
    fake.accounts[ALICE_TOKEN] = [credit("acct-a1"), {"account_id": "acct-chk", "name": "Checking", "type": "depository", "mask": "9999"}]
    fake.accounts[BOB_TOKEN] = [credit("acct-b1")]


def test_sync_writes_only_to_the_syncing_user(fake_plaid):
    setup_two_users(fake_plaid)
    fake_plaid.pages[ALICE_TOKEN] = [
        page(added=[txn("n1", "acct-a1", 12.00), txn("n2", "acct-a1", 8.00)], cursor="c1", more=True),
        page(added=[txn("n3", "acct-a1", 1.00),
                    txn("payment", "acct-a1", 100.00, category="LOAN_PAYMENTS"),
                    txn("refund", "acct-a1", -5.00),
                    txn("debit", "acct-chk", 40.00)],
             modified=[txn("n1", "acct-a1", 15.00)], removed=["old"], cursor="c2"),
    ]
    with database.connect() as conn:
        item = database.get_item(conn, "alice", "item-a")
    counts = get_transactions.sync_item("alice", item)

    assert counts == {"added": 3, "modified": 1, "removed": 1}
    with database.connect() as conn:
        rows = {t["transaction_id"]: t["amount_cents"] for t in database.recent_transactions(conn, "alice")}
        assert rows == {"n1": 1500, "n2": 800, "n3": 100}  # modified wins; non-purchases skipped; removed gone
        assert database.get_cursor(conn, "item-a") == "c2"
        assert database.spending_summary(conn, "bob") == (1, 5000)
        assert database.get_cursor(conn, "item-b") == "cursor-item-b"


def test_a_crash_mid_sync_keeps_saved_pages_and_their_cursor_together(fake_plaid):
    setup_two_users(fake_plaid)
    fake_plaid.pages[ALICE_TOKEN] = [page(added=[txn("p1", "acct-a1", 2.00)], cursor="after-page-1", more=True)]

    original = fake_plaid.post

    def post(path, body=None, timeout=30):
        if path == "/transactions/sync" and body["cursor"] == "after-page-1":
            raise plaid_client.PlaidError(500, "INTERNAL_SERVER_ERROR", "boom")
        return original(path, body, timeout)

    plaid_client.post = post
    with database.connect() as conn:
        item = database.get_item(conn, "alice", "item-a")
    with pytest.raises(plaid_client.PlaidError):
        get_transactions.sync_item("alice", item)

    with database.connect() as conn:
        assert database.get_cursor(conn, "item-a") == "after-page-1"
        assert {t["transaction_id"] for t in database.recent_transactions(conn, "alice")} == {"old", "p1"}


def test_removed_cards_are_never_synced_again(fake_plaid):
    setup_two_users(fake_plaid)
    with database.connect() as conn:
        database.remove_account(conn, "alice", "acct-a1")
        item = database.get_item(conn, "alice", "item-a")
    fake_plaid.pages[ALICE_TOKEN] = [page(added=[txn("new", "acct-a1", 9.00)], modified=[txn("old", "acct-a1", 3.00)])]
    get_transactions.sync_item("alice", item)
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE account_id = 'acct-a1'").fetchone()["n"] == 0
        assert database.ignored_account_ids(conn, "alice") == {"acct-a1"}


def test_one_failing_bank_does_not_stop_the_users_other_banks(fake_plaid):
    setup_two_users(fake_plaid)
    seed_user("alice", "item-a2", ["acct-a2"], bank="Broken Bank")
    fake_plaid.pages[ALICE_TOKEN] = [page(added=[txn("good", "acct-a1", 4.00)])]
    fake_plaid.fail[("/accounts/get", "access-sandbox-item-a2")] = plaid_client.PlaidError(400, "ITEM_LOGIN_REQUIRED", "login again")

    with pytest.raises(RuntimeError, match="Broken Bank: Plaid error 400: login again"):
        get_transactions.sync_and_log("alice")
    with database.connect() as conn:
        assert "good" in {t["transaction_id"] for t in database.recent_transactions(conn, "alice")}
        attempt = database.last_sync_attempt(conn, "alice")
        assert attempt["status"] == "error" and "Broken Bank" in attempt["error"]
        assert database.last_sync_attempt(conn, "bob") is None


def test_a_user_without_banks_gets_a_clear_error(fake_plaid):
    with database.connect() as conn:
        database.ensure_user(conn, "carol")
    with pytest.raises(get_transactions.NoBanksError):
        get_transactions.sync_user("carol")


def test_scheduler_only_runs_users_who_are_due(fake_plaid):
    setup_two_users(fake_plaid)
    with database.connect() as conn:
        database.ensure_user(conn, "carol")  # no banks
    synced = []
    scheduler = Scheduler(sync=lambda user_id: synced.append(user_id) or {"added": 0, "modified": 0, "removed": 0})
    scheduler.set_interval("bob", "off")

    scheduler.run_due()
    assert synced == ["alice"]  # alice: never synced; bob: auto-refresh off; carol: no banks


def test_scheduler_status_and_settings_are_per_user(fake_plaid):
    setup_two_users(fake_plaid)
    scheduler = Scheduler()
    scheduler.set_interval("alice", "1h")
    with database.connect() as conn:
        database.log_sync(conn, "bob", database.utc_now(), "success", {"added": 2})

    alice, bob = scheduler.status("alice"), scheduler.status("bob")
    assert alice["interval"] == "1h" and bob["interval"] == "1d"
    assert alice["last_attempt"] is None and alice["last_success_at"] is None
    assert bob["last_attempt"]["added"] == 2 and bob["last_success_at"] is not None
    assert bob["next_run_at"] > bob["last_success_at"]


def test_one_users_sync_does_not_block_anothers(fake_plaid):
    setup_two_users(fake_plaid)
    scheduler = Scheduler(sync=lambda user_id: {"added": 0, "modified": 0, "removed": 0})
    with scheduler.paused("alice"):
        with pytest.raises(SyncBusy):
            scheduler.run_now("alice")
        assert scheduler.run_now("bob") == {"added": 0, "modified": 0, "removed": 0}
        assert scheduler.status("alice")["running"] is True
        assert scheduler.status("bob")["running"] is False
