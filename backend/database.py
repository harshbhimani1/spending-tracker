import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "spending.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,
    subtype    TEXT,
    mask       TEXT,
    nickname   TEXT
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
    # Databases created before nicknames existed don't have the column yet.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "nickname" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN nickname TEXT")
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
        "SELECT account_id, name, mask, nickname FROM accounts "
        "WHERE type = 'credit' ORDER BY name, mask"
    ).fetchall()
    return [
        {
            "account_id": r["account_id"],
            "label": account_label(r),
            "default_label": account_label({**dict(r), "nickname": None}),
            "nickname": r["nickname"],
        }
        for r in rows
    ]


def set_account_nickname(conn, account_id, nickname):
    """Set a display name, or reset to the default with None. False if no such account."""
    cursor = conn.execute(
        "UPDATE accounts SET nickname = ? WHERE account_id = ?", (nickname, account_id)
    )
    return cursor.rowcount > 0


def list_months(conn):
    rows = conn.execute(
        "SELECT DISTINCT substr(date, 1, 7) AS month FROM transactions ORDER BY month"
    ).fetchall()
    return [r["month"] for r in rows]


def month_totals(conn, month):
    start, end = _month_range(month)
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total "
        "FROM transactions WHERE date BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    return row["n"], row["total"]


def month_top_merchant(conn, month):
    start, end = _month_range(month)
    row = conn.execute(
        "SELECT COALESCE(merchant_name, name) AS merchant, SUM(amount_cents) AS cents "
        "FROM transactions WHERE date BETWEEN ? AND ? "
        "GROUP BY merchant ORDER BY cents DESC LIMIT 1",
        (start, end),
    ).fetchone()
    return {"merchant": row["merchant"], "cents": row["cents"]} if row else None


def month_categories(conn, month):
    start, end = _month_range(month)
    rows = conn.execute(
        "SELECT COALESCE(category_primary, 'UNCATEGORIZED') AS category, "
        "SUM(amount_cents) AS cents, COUNT(*) AS n "
        "FROM transactions WHERE date BETWEEN ? AND ? "
        "GROUP BY category ORDER BY cents DESC",
        (start, end),
    ).fetchall()
    return [{"category": r["category"], "cents": r["cents"], "count": r["n"]} for r in rows]


def month_transactions(conn, month):
    start, end = _month_range(month)
    rows = conn.execute(
        "SELECT transaction_id, date, amount_cents, pending, account_id, "
        "COALESCE(merchant_name, name) AS merchant, "
        "COALESCE(category_primary, 'UNCATEGORIZED') AS category "
        "FROM transactions WHERE date BETWEEN ? AND ? "
        "ORDER BY date DESC, transaction_id",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def category_by_month(conn):
    """Return {month: {category: cents}} across all stored transactions."""
    rows = conn.execute(
        "SELECT substr(date, 1, 7) AS month, "
        "COALESCE(category_primary, 'UNCATEGORIZED') AS category, "
        "SUM(amount_cents) AS cents FROM transactions GROUP BY month, category"
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["month"], {})[r["category"]] = r["cents"]
    return result
