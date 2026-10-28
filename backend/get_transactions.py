import json
import os
import requests
from contextlib import closing
from pathlib import Path
from dotenv import load_dotenv

import database

load_dotenv()

BASE_URL = f"https://{os.getenv('PLAID_ENV', 'sandbox')}.plaid.com"

# Money leaving the card that isn't spending: card payments, transfers.
NON_SPENDING = {"LOAN_PAYMENTS", "TRANSFER_IN", "TRANSFER_OUT"}

token_file = Path(__file__).parent / "plaid_token.json"


def is_purchase(transaction, credit_ids):
    """Credit card spending only: no debits, payments or transfers."""
    if transaction["account_id"] not in credit_ids or transaction["amount"] <= 0:
        return False
    category = (transaction.get("personal_finance_category") or {}).get("primary")
    return category not in NON_SPENDING


def check_plaid(response):
    """Like raise_for_status, but includes Plaid's own explanation of the error."""
    if response.ok:
        return
    try:
        detail = response.json().get("error_message")
    except ValueError:
        detail = None
    raise RuntimeError(f"Plaid error {response.status_code}: {detail or response.reason}")


def sync_transactions():
    """Pull new and changed purchases from Plaid into the database.

    Returns {"added", "modified", "removed"} counts. Raises on any failure; the
    saved cursor only ever advances together with the writes it covers.
    """
    if not token_file.exists():
        raise FileNotFoundError(f"{token_file.name} not found; run connect_bank.py first")

    token_data = json.loads(token_file.read_text())
    item_id = token_data["item_id"]
    credentials = {
        "client_id": os.getenv("PLAID_CLIENT_ID"),
        "secret": os.getenv("PLAID_SECRET"),
        "access_token": token_data["access_token"],
    }

    response = requests.post(
        f"{BASE_URL}/accounts/get",
        json=credentials,
        timeout=30
    )
    check_plaid(response)

    accounts = response.json()["accounts"]
    credit_ids = {a["account_id"] for a in accounts if a["type"] == "credit"}

    counts = {"added": 0, "modified": 0, "removed": 0}

    with closing(database.connect()) as conn:
        with conn:
            database.upsert_accounts(conn, item_id, accounts)
        cursor = database.get_cursor(conn, item_id)

        while True:
            response = requests.post(
                f"{BASE_URL}/transactions/sync",
                json={**credentials, "cursor": cursor},
                timeout=60
            )
            check_plaid(response)
            data = response.json()

            # A modified transaction may stop qualifying (e.g. recategorized as a
            # transfer), so anything modified that isn't a purchase is dropped.
            new = [t for t in data["added"] if is_purchase(t, credit_ids)]
            changed = [t for t in data["modified"] if is_purchase(t, credit_ids)]
            gone = [t["transaction_id"] for t in data["modified"] if not is_purchase(t, credit_ids)]
            gone += [t["transaction_id"] for t in data["removed"]]

            # Order matters: if an ID shows up in both added and modified, the
            # modified version must win. Upserts run in list order, so `changed`
            # goes after `new` to overwrite it. Deletes run after the upserts, so a
            # modified transaction that stopped being a purchase ends up removed.
            #
            # Cursor is saved in the same SQLite transaction as the page it
            # covers, so a crash can't skip data.
            with conn:
                database.upsert_transactions(conn, new + changed)
                database.delete_transactions(conn, gone)
                cursor = data["next_cursor"]
                database.set_cursor(conn, item_id, cursor)

            counts["added"] += len(new)
            counts["modified"] += len(changed)
            counts["removed"] += len(gone)

            if not data["has_more"]:
                break

    return counts


def sync_and_log():
    """Run a sync and record the attempt, so the dashboard can show when it last worked."""
    started_at = database.utc_now()
    try:
        counts = sync_transactions()
    except Exception as exc:
        with closing(database.connect()) as conn, conn:
            database.log_sync(conn, started_at, "error", error=str(exc)[:300])
        raise
    with closing(database.connect()) as conn, conn:
        database.log_sync(conn, started_at, "success", counts)
    return counts


def main():
    try:
        counts = sync_and_log()
    except FileNotFoundError as exc:
        raise SystemExit(str(exc))

    print(f"Synced: {counts['added']} added, {counts['modified']} modified, {counts['removed']} removed")

    with closing(database.connect()) as conn:
        for t in database.recent_transactions(conn):
            print(
                t["merchant_name"] or t["name"],
                f'${t["amount_cents"] / 100:.2f}',
                "Category:", t["category_primary"],
                "Subcategory:", t["category_detailed"]
            )
            print()

        count, total_cents = database.spending_summary(conn)

    print(f"\nDatabase holds {count} credit card purchases totaling ${total_cents / 100:,.2f}")


if __name__ == "__main__":
    main()
