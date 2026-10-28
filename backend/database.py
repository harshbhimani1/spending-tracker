import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "spending.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,
    subtype    TEXT,
    mask       TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id         TEXT PRIMARY KEY,
    account_id             TEXT NOT NULL REFERENCES accounts(account_id),
    date                   TEXT NOT NULL,
    amount_cents           INTEGER NOT NULL,
    name                   TEXT NOT NULL,
    merchant_name          TEXT,
    category_primary       TEXT,
    category_detailed      TEXT,
    pending                INTEGER NOT NULL DEFAULT 0,
    pending_transaction_id TEXT,
    iso_currency_code      TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);

CREATE TABLE IF NOT EXISTS sync_state (
    item_id TEXT PRIMARY KEY,
    cursor  TEXT NOT NULL
);
"""


def connect():
    """Open the database, creating the schema on first use.

    Use as `with connect() as conn:`: the block commits on success and rolls
    back on error (the connection itself stays open until garbage collected).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def dollars_to_cents(amount):
    return round(amount * 100)


def get_cursor(conn, item_id):
    row = conn.execute(
        "SELECT cursor FROM sync_state WHERE item_id = ?", (item_id,)
    ).fetchone()
    return row["cursor"] if row else ""


def upsert_accounts(conn, item_id, accounts):
    conn.executemany(
        """
        INSERT INTO accounts (account_id, item_id, name, type, subtype, mask)
        VALUES (:account_id, :item_id, :name, :type, :subtype, :mask)
        ON CONFLICT(account_id) DO UPDATE SET
            name = excluded.name,
            type = excluded.type,
            subtype = excluded.subtype,
            mask = excluded.mask
        """,
        [
            {
                "account_id": a["account_id"],
                "item_id": item_id,
                "name": a["name"],
                "type": a["type"],
                "subtype": a.get("subtype"),
                "mask": a.get("mask"),
            }
            for a in accounts
        ],
    )


def upsert_transactions(conn, transactions):
    """Insert or update Plaid transaction objects, converting amounts to cents."""
    conn.executemany(
        """
        INSERT INTO transactions (
            transaction_id, account_id, date, amount_cents, name, merchant_name,
            category_primary, category_detailed, pending, pending_transaction_id,
            iso_currency_code
        )
        VALUES (
            :transaction_id, :account_id, :date, :amount_cents, :name, :merchant_name,
            :category_primary, :category_detailed, :pending, :pending_transaction_id,
            :iso_currency_code
        )
        ON CONFLICT(transaction_id) DO UPDATE SET
            account_id = excluded.account_id,
            date = excluded.date,
            amount_cents = excluded.amount_cents,
            name = excluded.name,
            merchant_name = excluded.merchant_name,
            category_primary = excluded.category_primary,
            category_detailed = excluded.category_detailed,
            pending = excluded.pending,
            pending_transaction_id = excluded.pending_transaction_id,
            iso_currency_code = excluded.iso_currency_code
        """,
        [
            {
                "transaction_id": t["transaction_id"],
                "account_id": t["account_id"],
                "date": t["date"],
                "amount_cents": dollars_to_cents(t["amount"]),
                "name": t["name"],
                "merchant_name": t.get("merchant_name"),
                "category_primary": (t.get("personal_finance_category") or {}).get("primary"),
                "category_detailed": (t.get("personal_finance_category") or {}).get("detailed"),
                "pending": int(t.get("pending", False)),
                "pending_transaction_id": t.get("pending_transaction_id"),
                "iso_currency_code": t.get("iso_currency_code"),
            }
            for t in transactions
        ],
    )


def delete_transactions(conn, transaction_ids):
    conn.executemany(
        "DELETE FROM transactions WHERE transaction_id = ?",
        [(transaction_id,) for transaction_id in transaction_ids],
    )


def set_cursor(conn, item_id, cursor):
    conn.execute(
        """
        INSERT INTO sync_state (item_id, cursor) VALUES (?, ?)
        ON CONFLICT(item_id) DO UPDATE SET cursor = excluded.cursor
        """,
        (item_id, cursor),
    )


def recent_transactions(conn, limit=20):
    return conn.execute(
        """
        SELECT t.*, a.name AS account_name
        FROM transactions t JOIN accounts a USING (account_id)
        ORDER BY t.date DESC, t.transaction_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def spending_summary(conn):
    """Return (purchase_count, total_cents) across all stored transactions."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total FROM transactions"
    ).fetchone()
    return row["n"], row["total"]
