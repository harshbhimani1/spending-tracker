"""One family member must never see or change another's data."""
import database
from conftest import seed_user, txn

OCT = "2026-10"


def two_users():
    seed_user("alice", "item-a", ["acct-a1", "acct-a2"], [
        txn("ta1", "acct-a1", 10.00, "2026-10-03", "TRAVEL", "Airline"),
        txn("ta2", "acct-a2", 25.50, "2026-10-04", "FOOD_AND_DRINK", "Cafe"),
        txn("ta3", "acct-a1", 7.25, "2026-09-12", "FOOD_AND_DRINK", "Cafe"),
    ], bank="Alice Bank")
    seed_user("bob", "item-b", ["acct-b1"], [
        txn("tb1", "acct-b1", 999.99, "2026-10-03", "ENTERTAINMENT", "Concert"),
        txn("tb2", "acct-b1", 1.00, "2026-08-01", "MEDICAL", "Clinic"),
    ], bank="Bob Bank")


def test_every_dashboard_read_only_returns_the_users_own_data():
    two_users()
    with database.connect() as conn:
        assert [i["item_id"] for i in database.list_items(conn, "alice")] == ["item-a"]
        assert set(database.item_counts(conn, "alice")) == {"item-a"}
        assert {a["account_id"] for a in database.list_accounts(conn, "alice")} == {"acct-a1", "acct-a2"}
        assert database.list_months(conn, "alice") == ["2026-09", "2026-10"]
        assert database.month_totals(conn, "alice", OCT) == (2, 3550)
        assert database.month_top_merchant(conn, "alice", OCT) == {"merchant": "Cafe", "cents": 2550}
        assert {c["category"] for c in database.month_categories(conn, "alice", OCT)} == {"TRAVEL", "FOOD_AND_DRINK"}
        assert sum(d["cents"] for d in database.month_daily(conn, "alice", OCT)) == 3550
        assert {t["transaction_id"] for t in database.month_transactions(conn, "alice", OCT)} == {"ta1", "ta2"}
        assert set(database.category_by_month(conn, "alice")) == {"2026-09", "2026-10"}
        assert {t["transaction_id"] for t in database.recent_transactions(conn, "alice")} == {"ta1", "ta2", "ta3"}
        assert database.spending_summary(conn, "alice") == (3, 4275)

        # And Bob sees only Bob's.
        assert database.month_totals(conn, "bob", OCT) == (1, 99999)
        assert database.list_months(conn, "bob") == ["2026-08", "2026-10"]
        assert database.spending_summary(conn, "bob") == (2, 100099)


def test_a_new_user_sees_nothing():
    two_users()
    with database.connect() as conn:
        database.ensure_user(conn, "carol")
        assert database.list_items(conn, "carol") == []
        assert database.list_accounts(conn, "carol") == []
        assert database.list_months(conn, "carol") == []
        assert database.month_transactions(conn, "carol", OCT) == []
        assert database.spending_summary(conn, "carol") == (0, 0)


def test_cannot_touch_another_users_bank_or_cards_by_id():
    two_users()
    with database.connect() as conn:
        # Alice tries to use Bob's ids.
        assert database.get_item(conn, "alice", "item-b") is None
        assert database.remove_item(conn, "alice", "item-b") is None
        assert database.remove_account(conn, "alice", "acct-b1") is None
        assert database.set_account_nickname(conn, "alice", "acct-b1", "Mine now") is False
        database.delete_transactions(conn, "alice", ["tb1", "tb2"])
        database.set_institution_name(conn, "alice", "item-b", "Hijacked")

        # Bob's data is completely untouched.
        assert database.spending_summary(conn, "bob") == (2, 100099)
        bob_bank = database.get_item(conn, "bob", "item-b")
        assert bob_bank["institution_name"] == "Bob Bank"
        bob_card = database.list_accounts(conn, "bob")[0]
        assert bob_card["nickname"] is None and bob_card["purchases"] == 2


def test_upserts_with_another_users_ids_cannot_take_over_their_rows():
    two_users()
    with database.connect() as conn:
        # Same bank id, card id and transaction id as Bob's, written as Alice.
        database.add_item(conn, "alice", "item-b", "access-sandbox-stolen", "Alice's", "sandbox")
        database.upsert_accounts(conn, "alice", "item-a", [{"account_id": "acct-b1", "name": "Hijack", "type": "credit", "mask": "0000"}])
        database.upsert_transactions(conn, "alice", [txn("tb1", "acct-a1", 0.01)])

        owner = conn.execute("SELECT user_id FROM items WHERE item_id = 'item-b'").fetchone()
        assert owner == {"user_id": "bob"}
        assert database.get_item(conn, "bob", "item-b")["access_token"] == "access-sandbox-item-b"
        card = conn.execute("SELECT user_id, item_id, name FROM accounts WHERE account_id = 'acct-b1'").fetchone()
        assert card == {"user_id": "bob", "item_id": "item-b", "name": "Card acct-b1"}
        purchase = conn.execute("SELECT user_id, amount_cents FROM transactions WHERE transaction_id = 'tb1'").fetchone()
        assert purchase == {"user_id": "bob", "amount_cents": 99999}
        assert database.spending_summary(conn, "alice") == (3, 4275)


def test_settings_and_sync_history_are_per_user():
    two_users()
    with database.connect() as conn:
        database.set_setting(conn, "alice", "refresh_interval", "1h")
        database.set_setting(conn, "bob", "refresh_interval", "1w")
        database.log_sync(conn, "alice", database.utc_now(), "error", error="alice's problem")
        assert database.get_setting(conn, "alice", "refresh_interval") == "1h"
        assert database.get_setting(conn, "bob", "refresh_interval") == "1w"
        assert database.get_setting(conn, "carol", "refresh_interval", "default") == "default"
        assert database.last_sync_attempt(conn, "alice")["error"] == "alice's problem"
        assert database.last_sync_attempt(conn, "bob") is None


def test_removing_a_bank_deletes_only_that_banks_data():
    two_users()
    seed_user("alice", "item-a2", ["acct-a3"], [txn("ta9", "acct-a3", 5.00)], bank="Second")
    with database.connect() as conn:
        assert database.remove_item(conn, "alice", "item-a") == 3
        left = conn.execute(
            "SELECT (SELECT COUNT(*) FROM accounts WHERE item_id = 'item-a') AS cards, "
            "(SELECT COUNT(*) FROM transactions WHERE account_id IN ('acct-a1','acct-a2')) AS purchases, "
            "(SELECT COUNT(*) FROM sync_state WHERE item_id = 'item-a') AS cursors"
        ).fetchone()
        assert left == {"cards": 0, "purchases": 0, "cursors": 0}
        assert [i["item_id"] for i in database.list_items(conn, "alice")] == ["item-a2"]
        assert database.spending_summary(conn, "alice") == (1, 500)
        assert database.spending_summary(conn, "bob") == (2, 100099)


def test_removing_a_card_hides_it_and_keeps_it_removed():
    two_users()
    with database.connect() as conn:
        assert database.remove_account(conn, "alice", "acct-a1") == 2
        assert [a["account_id"] for a in database.list_accounts(conn, "alice")] == ["acct-a2"]
        assert database.ignored_account_ids(conn, "alice") == {"acct-a1"}
        assert database.ignored_account_ids(conn, "bob") == set()
        # A later account refresh from Plaid doesn't un-remove it.
        database.upsert_accounts(conn, "alice", "item-a", [{"account_id": "acct-a1", "name": "Card", "type": "credit", "mask": "a1"}])
        assert database.ignored_account_ids(conn, "alice") == {"acct-a1"}
        assert database.remove_account(conn, "alice", "acct-a1") is None


def test_sandbox_and_production_banks_never_mix(monkeypatch):
    seed_user("alice", "item-sandbox", ["acct-s"], [txn("ts", "acct-s", 1.00)], environment="sandbox")
    seed_user("alice", "item-prod", ["acct-p"], [txn("tp", "acct-p", 2.00)], environment="production")
    with database.connect() as conn:
        assert database.spending_summary(conn, "alice") == (1, 100)
        monkeypatch.setenv("PLAID_ENV", "production")
        assert database.spending_summary(conn, "alice") == (1, 200)
        assert [i["item_id"] for i in database.list_items(conn, "alice")] == ["item-prod"]


def test_month_boundaries_are_exact():
    seed_user("alice", "item-a", ["acct-a1"], [
        txn("last-sep", "acct-a1", 1.00, "2026-09-30"),
        txn("first-oct", "acct-a1", 2.00, "2026-10-01"),
        txn("last-oct", "acct-a1", 4.00, "2026-10-31"),
        txn("first-nov", "acct-a1", 8.00, "2026-11-01"),
        txn("dec", "acct-a1", 16.00, "2026-12-31"),
    ])
    with database.connect() as conn:
        assert database.month_totals(conn, "alice", "2026-10") == (2, 600)
        assert database.month_totals(conn, "alice", "2026-12") == (1, 1600)
