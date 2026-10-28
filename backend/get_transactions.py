from contextlib import closing

import database
import plaid_client

# Money leaving the card that isn't spending: card payments, transfers.
NON_SPENDING = {"LOAN_PAYMENTS", "TRANSFER_IN", "TRANSFER_OUT"}


class NoBanksError(RuntimeError):
    """Nothing to sync because no bank has been connected yet."""


def is_purchase(transaction, credit_ids):
    """Credit card spending only: no debits, payments or transfers."""
    if transaction["account_id"] not in credit_ids or transaction["amount"] <= 0:
        return False
    category = (transaction.get("personal_finance_category") or {}).get("primary")
    return category not in NON_SPENDING


def fetch_institution_name(access_token):
    item = plaid_client.post("/item/get", {"access_token": access_token})["item"]
    institution = plaid_client.post("/institutions/get_by_id", {
        "institution_id": item["institution_id"],
        "country_codes": ["US"],
    })["institution"]
    return institution["name"]


def sync_item(item):
    """Pull new and changed purchases for one bank into the database.

    Returns {"added", "modified", "removed"} counts. The saved cursor only ever
    advances together with the writes it covers.
    """
    item_id = item["item_id"]
    access = {"access_token": item["access_token"]}

    accounts = plaid_client.post("/accounts/get", access)["accounts"]
    credit_ids = {a["account_id"] for a in accounts if a["type"] == "credit"}

    counts = {"added": 0, "modified": 0, "removed": 0}

    with closing(database.connect()) as conn:
        with conn:
            database.upsert_accounts(conn, item_id, accounts)
        cursor = database.get_cursor(conn, item_id)

        # Banks imported before names were stored get theirs filled in here.
        if not item["institution_name"]:
            try:
                with conn:
                    database.set_institution_name(conn, item_id, fetch_institution_name(item["access_token"]))
            except Exception:
                pass  # cosmetic only; try again next sync

        while True:
            data = plaid_client.post("/transactions/sync", {**access, "cursor": cursor}, timeout=60)

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


def sync_transactions():
    """Sync every connected bank. Returns the summed {"added", "modified", "removed"}.

    A bank that fails doesn't stop the others; the failures are raised together
    once the rest have finished.
    """
    with closing(database.connect()) as conn:
        items = database.list_items(conn)
    if not items:
        raise NoBanksError("No bank connected yet. Use Connect a bank first.")

    counts = {"added": 0, "modified": 0, "removed": 0}
    failures = []
    for item in items:
        try:
            item_counts = sync_item(item)
        except Exception as exc:
            failures.append(f"{item['institution_name'] or 'Bank'}: {exc}")
            continue
        for key in counts:
            counts[key] += item_counts[key]

    if failures:
        raise RuntimeError("; ".join(failures))
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
    except NoBanksError as exc:
        raise SystemExit(f"{exc} Run connect_bank.py or use the dashboard.")

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
