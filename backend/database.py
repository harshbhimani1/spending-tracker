import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(__file__).parent / "spending.db"
# Where the single bank lived before banks moved into the items table.
LEGACY_TOKEN_FILE = Path(__file__).parent / "plaid_token.json"


def active_environment():
    """Plaid environment from .env. Only banks linked in it show on the dashboard."""
    environment = os.getenv("PLAID_ENV", "sandbox")
    if not re.fullmatch(r"[a-z]+", environment):
        raise ValueError(f"Invalid PLAID_ENV: {environment!r}")
    return environment

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,
    subtype    TEXT,
    mask       TEXT,
    nickname   TEXT,
    ignored    INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS items (
    item_id          TEXT PRIMARY KEY,
    access_token     TEXT NOT NULL,
    institution_name TEXT,
    environment      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    item_id TEXT PRIMARY KEY,
    cursor  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('success', 'error')),
    added       INTEGER NOT NULL DEFAULT 0,
    modified    INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    error       TEXT
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
    # Databases created before these columns existed don't have them yet.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "nickname" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN nickname TEXT")
    if "ignored" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0")
    # Reads go through this view so sandbox test data never mixes into the
    # totals once a production key is in use (and vice versa). The environment
    # is validated to letters only, so inlining it is safe.
    conn.execute(
        f"""
        CREATE TEMP VIEW active_transactions AS
        SELECT t.* FROM transactions t
        JOIN accounts a USING (account_id)
        JOIN items i USING (item_id)
        WHERE i.environment = '{active_environment()}' AND a.ignored = 0
        """
    )
    import_legacy_token(conn)
    return conn


def import_legacy_token(conn):
    """One-time move of the bank saved in plaid_token.json into the items table.

    Runs at most once per database (a settings flag records it), so removing
    every bank later can't make the old token file bring one back.
    """
    if get_setting(conn, "legacy_token_checked"):
        return
    has_items = conn.execute("SELECT 1 FROM items LIMIT 1").fetchone()
    if LEGACY_TOKEN_FILE.exists() and not has_items:
        data = json.loads(LEGACY_TOKEN_FILE.read_text())
        # That file was only ever written by connect_bank.py, a sandbox-only script.
        add_item(conn, data["item_id"], data["access_token"], None, "sandbox")
    set_setting(conn, "legacy_token_checked", "1")
    conn.commit()


def add_item(conn, item_id, access_token, institution_name, environment):
    conn.execute(
        """
        INSERT INTO items (item_id, access_token, institution_name, environment, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            access_token = excluded.access_token,
            institution_name = COALESCE(excluded.institution_name, institution_name)
        """,
        (item_id, access_token, institution_name, environment, utc_now()),
    )


def list_items(conn):
    """Banks linked in the active environment, oldest first. Includes access tokens."""
    rows = conn.execute(
        "SELECT * FROM items WHERE environment = ? ORDER BY created_at, item_id",
        (active_environment(),),
    ).fetchall()
    return [dict(r) for r in rows]


def item_counts(conn):
    """{item_id: {"cards", "purchases"}} for the cards and purchases stored per bank."""
    counts = {}
    for row in conn.execute(
        "SELECT a.item_id, COUNT(*) AS cards, "
        "SUM((SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.account_id)) AS purchases "
        "FROM accounts a WHERE a.type = 'credit' AND a.ignored = 0 GROUP BY a.item_id"
    ):
        counts[row["item_id"]] = {"cards": row["cards"], "purchases": row["purchases"] or 0}
    return counts


def remove_item(conn, item_id):
    """Delete a bank and everything stored for it: purchases, cards, sync position.

    Revoking the connection at Plaid is the caller's job (and should come first).
    Returns how many purchases were deleted.
    """
    accounts = [r["account_id"] for r in conn.execute("SELECT account_id FROM accounts WHERE item_id = ?", (item_id,))]
    deleted = 0
    for account_id in accounts:
        deleted += conn.execute("DELETE FROM transactions WHERE account_id = ?", (account_id,)).rowcount
    conn.execute("DELETE FROM accounts WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM sync_state WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM items WHERE item_id = ?", (item_id,))
    return deleted


def set_institution_name(conn, item_id, name):
    conn.execute("UPDATE items SET institution_name = ? WHERE item_id = ?", (name, item_id))


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


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_sync(conn, started_at, status, counts=None, error=None):
    """Record one sync attempt. `counts` has added/modified/removed on success."""
    counts = counts or {}
    conn.execute(
        """
        INSERT INTO sync_log (started_at, finished_at, status, added, modified, removed, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at, utc_now(), status,
            counts.get("added", 0), counts.get("modified", 0), counts.get("removed", 0),
            error,
        ),
    )


def last_sync_attempt(conn, status=None):
    """Most recent sync attempt, optionally only successful or failed ones."""
    query = "SELECT * FROM sync_log"
    params = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    row = conn.execute(query + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return dict(row) if row else None


def recent_transactions(conn, limit=20):
    return conn.execute(
        """
        SELECT t.*, a.name AS account_name
        FROM active_transactions t JOIN accounts a USING (account_id)
        ORDER BY t.date DESC, t.transaction_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def spending_summary(conn):
    """Return (purchase_count, total_cents) across all stored transactions."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total FROM active_transactions"
    ).fetchone()
    return row["n"], row["total"]


# --- Dashboard queries -------------------------------------------------
# `month` is "YYYY-MM". Dates are ISO text, so a BETWEEN on the first and
# last possible day of the month selects it without parsing.

def _month_range(month):
    return f"{month}-01", f"{month}-31"


def account_label(account):
    """Nickname if set, otherwise the Plaid name with the card's last digits."""
    if account["nickname"]:
        return account["nickname"]
    return f'{account["name"]} ••{account["mask"]}' if account["mask"] else account["name"]


def list_accounts(conn):
    rows = conn.execute(
        "SELECT a.account_id, a.item_id, a.name, a.mask, a.nickname, "
        "(SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.account_id) AS purchases "
        "FROM accounts a JOIN items i USING (item_id) "
        "WHERE a.type = 'credit' AND a.ignored = 0 AND i.environment = ? "
        "ORDER BY a.name, a.mask",
        (active_environment(),),
    ).fetchall()
    return [
        {
            "account_id": r["account_id"],
            "item_id": r["item_id"],
            "label": account_label(r),
            "default_label": account_label({**dict(r), "nickname": None}),
            "nickname": r["nickname"],
            "purchases": r["purchases"],
        }
        for r in rows
    ]


def remove_account(conn, account_id):
    """Permanently delete a card's stored purchases and stop tracking it.

    The account row stays, marked ignored, so later syncs skip it instead of
    bringing it back. Returns the number of purchases deleted, or None if
    there is no such card.
    """
    exists = conn.execute(
        "SELECT 1 FROM accounts WHERE account_id = ? AND ignored = 0", (account_id,)
    ).fetchone()
    if not exists:
        return None
    deleted = conn.execute("DELETE FROM transactions WHERE account_id = ?", (account_id,)).rowcount
    conn.execute("UPDATE accounts SET ignored = 1 WHERE account_id = ?", (account_id,))
    return deleted


def ignored_account_ids(conn):
    return {r["account_id"] for r in conn.execute("SELECT account_id FROM accounts WHERE ignored = 1")}


def set_account_nickname(conn, account_id, nickname):
    """Set a display name, or reset to the default with None. False if no such account."""
    cursor = conn.execute(
        "UPDATE accounts SET nickname = ? WHERE account_id = ?", (nickname, account_id)
    )
    return cursor.rowcount > 0


def list_months(conn):
    rows = conn.execute(
        "SELECT DISTINCT substr(date, 1, 7) AS month FROM active_transactions ORDER BY month"
    ).fetchall()
    return [r["month"] for r in rows]


def month_totals(conn, month):
    start, end = _month_range(month)
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total "
        "FROM active_transactions WHERE date BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    return row["n"], row["total"]


def month_top_merchant(conn, month):
    start, end = _month_range(month)
    row = conn.execute(
        "SELECT COALESCE(merchant_name, name) AS merchant, SUM(amount_cents) AS cents "
        "FROM active_transactions WHERE date BETWEEN ? AND ? "
        "GROUP BY merchant ORDER BY cents DESC LIMIT 1",
        (start, end),
    ).fetchone()
    return {"merchant": row["merchant"], "cents": row["cents"]} if row else None


def month_categories(conn, month):
    start, end = _month_range(month)
    rows = conn.execute(
        "SELECT COALESCE(category_primary, 'UNCATEGORIZED') AS category, "
        "SUM(amount_cents) AS cents, COUNT(*) AS n "
        "FROM active_transactions WHERE date BETWEEN ? AND ? "
        "GROUP BY category ORDER BY cents DESC",
        (start, end),
    ).fetchall()
    return [{"category": r["category"], "cents": r["cents"], "count": r["n"]} for r in rows]


def month_daily(conn, month):
    """Total spent and purchase count for each day of the month that had any."""
    start, end = _month_range(month)
    rows = conn.execute(
        "SELECT date, SUM(amount_cents) AS cents, COUNT(*) AS n "
        "FROM active_transactions WHERE date BETWEEN ? AND ? "
        "GROUP BY date ORDER BY date",
        (start, end),
    ).fetchall()
    return [{"date": r["date"], "cents": r["cents"], "count": r["n"]} for r in rows]


def month_transactions(conn, month):
    start, end = _month_range(month)
    rows = conn.execute(
        "SELECT transaction_id, date, amount_cents, pending, account_id, "
        "COALESCE(merchant_name, name) AS merchant, "
        "COALESCE(category_primary, 'UNCATEGORIZED') AS category "
        "FROM active_transactions WHERE date BETWEEN ? AND ? "
        "ORDER BY date DESC, transaction_id",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def category_by_month(conn):
    """Return {month: {category: cents}} across all stored transactions."""
    rows = conn.execute(
        "SELECT substr(date, 1, 7) AS month, "
        "COALESCE(category_primary, 'UNCATEGORIZED') AS category, "
        "SUM(amount_cents) AS cents FROM active_transactions GROUP BY month, category"
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["month"], {})[r["category"]] = r["cents"]
    return result
