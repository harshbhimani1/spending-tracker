"""Postgres storage for the multi-user spending tracker.

Every row belongs to a user, and every function that reads or changes user
data takes that user's id and filters on it. Nothing here trusts an id passed
in from a request (a bank, card or transaction id) without also checking it
belongs to the user asking.

Connections are opened in autocommit mode: a single statement commits on its
own, and anything that must succeed or fail together is wrapped in
`with conn.transaction():`.
"""
import os
import re
from datetime import date, datetime, timezone

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

import token_crypto

load_dotenv()

DEFAULT_DATABASE_URL = "postgresql://localhost/spending_tracker_dev"


def database_url():
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def active_environment():
    """Plaid environment from .env. Only banks linked in it show on the dashboard."""
    environment = os.getenv("PLAID_ENV", "sandbox")
    if not re.fullmatch(r"[a-z]+", environment):
        raise ValueError(f"Invalid PLAID_ENV: {environment!r}")
    return environment


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    email      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS items (
    item_id          TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    access_token     TEXT NOT NULL,  -- always encrypted (see token_crypto.py)
    institution_name TEXT,
    environment      TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,
    subtype    TEXT,
    mask       TEXT,
    nickname   TEXT,
    ignored    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_accounts_item ON accounts(item_id);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id         TEXT PRIMARY KEY,
    account_id             TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    user_id                TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    date                   DATE NOT NULL,
    amount_cents           BIGINT NOT NULL,
    name                   TEXT NOT NULL,
    merchant_name          TEXT,
    category_primary       TEXT,
    category_detailed      TEXT,
    pending                BOOLEAN NOT NULL DEFAULT FALSE,
    pending_transaction_id TEXT,
    iso_currency_code      TEXT
);
CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions(user_id, date);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);

CREATE TABLE IF NOT EXISTS sync_state (
    item_id TEXT PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
    cursor  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('success', 'error')),
    added       INTEGER NOT NULL DEFAULT 0,
    modified    INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_log_user ON sync_log(user_id, id DESC);
"""


def connect():
    """Open a connection. Use as `with connect() as conn:`, which closes it afterwards."""
    return psycopg.connect(database_url(), autocommit=True, row_factory=dict_row)


def init_schema():
    """Create any missing tables. Safe to run repeatedly."""
    with connect() as conn:
        conn.execute(SCHEMA)


def utc_now():
    return datetime.now(timezone.utc)


def iso(moment):
    """Timestamps go to the browser as ISO text without microseconds."""
    return moment.isoformat(timespec="seconds") if moment else None


# --- Users -------------------------------------------------------------

def ensure_user(conn, user_id, email=None):
    conn.execute(
        """
        INSERT INTO users (user_id, email) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET email = COALESCE(EXCLUDED.email, users.email)
        """,
        (user_id, email),
    )


def list_user_ids(conn):
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM users ORDER BY created_at")]


# --- Banks (Plaid items) -----------------------------------------------

def add_item(conn, user_id, item_id, access_token, institution_name, environment):
    # The WHERE keeps a bank from ever being moved to a different user.
    conn.execute(
        """
        INSERT INTO items (item_id, user_id, access_token, institution_name, environment)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (item_id) DO UPDATE SET
            access_token = EXCLUDED.access_token,
            institution_name = COALESCE(EXCLUDED.institution_name, items.institution_name)
        WHERE items.user_id = EXCLUDED.user_id
        """,
        (item_id, user_id, token_crypto.encrypt(access_token, user_id, item_id), institution_name, environment),
    )


def _readable_item(row):
    """An items row with its access token decrypted for use.

    If the token can't be decrypted (e.g. the encryption key was lost or
    changed), access_token is None and token_error explains, so the rest of
    the user's dashboard keeps working and only this bank reports a problem.
    """
    item = {**row, "created_at": iso(row["created_at"]), "token_error": None}
    try:
        item["access_token"] = token_crypto.decrypt(row["access_token"], row["user_id"], row["item_id"])
    except token_crypto.TokenDecryptError as exc:
        item["access_token"] = None
        item["token_error"] = str(exc)
    return item


def list_items(conn, user_id):
    """The user's banks in the active environment, oldest first. Includes decrypted access tokens."""
    rows = conn.execute(
        "SELECT * FROM items WHERE user_id = %s AND environment = %s ORDER BY created_at, item_id",
        (user_id, active_environment()),
    ).fetchall()
    return [_readable_item(r) for r in rows]


def encrypt_plaintext_tokens(conn):
    """Encrypt any tokens stored before encryption existed. Returns how many."""
    rows = conn.execute("SELECT item_id, user_id, access_token FROM items").fetchall()
    plain = [r for r in rows if not token_crypto.is_encrypted(r["access_token"])]
    with conn.transaction():
        for r in plain:
            conn.execute(
                "UPDATE items SET access_token = %s WHERE item_id = %s",
                (token_crypto.encrypt(r["access_token"], r["user_id"], r["item_id"]), r["item_id"]),
            )
    return len(plain)


def reencrypt_tokens(conn):
    """Re-encrypt every stored token with the newest key (after adding one). Returns how many."""
    rows = conn.execute("SELECT item_id, access_token FROM items").fetchall()
    with conn.transaction():
        for r in rows:
            conn.execute(
                "UPDATE items SET access_token = %s WHERE item_id = %s",
                (token_crypto.reencrypt(r["access_token"]), r["item_id"]),
            )
    return len(rows)


def get_item(conn, user_id, item_id):
    """One of the user's banks, or None if it isn't theirs (or doesn't exist)."""
    return next((i for i in list_items(conn, user_id) if i["item_id"] == item_id), None)


def item_counts(conn, user_id):
    """{item_id: {"cards", "purchases"}} for the user's visible cards and their purchases."""
    rows = conn.execute(
        """
        SELECT a.item_id, COUNT(*) AS cards,
               SUM((SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.account_id)) AS purchases
        FROM accounts a
        WHERE a.user_id = %s AND a.type = 'credit' AND NOT a.ignored
        GROUP BY a.item_id
        """,
        (user_id,),
    ).fetchall()
    return {r["item_id"]: {"cards": r["cards"], "purchases": int(r["purchases"] or 0)} for r in rows}


def remove_item(conn, user_id, item_id):
    """Delete one of the user's banks and everything stored for it.

    Cards, purchases and the sync position go with it (ON DELETE CASCADE).
    Revoking the connection at Plaid is the caller's job and should come first.
    Returns how many purchases were deleted, or None if it isn't the user's bank.
    """
    with conn.transaction():
        row = conn.execute(
            """
            SELECT COUNT(t.transaction_id) AS purchases
            FROM items i
            LEFT JOIN accounts a ON a.item_id = i.item_id
            LEFT JOIN transactions t ON t.account_id = a.account_id
            WHERE i.item_id = %s AND i.user_id = %s
            GROUP BY i.item_id
            """,
            (item_id, user_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM items WHERE item_id = %s AND user_id = %s", (item_id, user_id))
    return row["purchases"]


def set_institution_name(conn, user_id, item_id, name):
    conn.execute(
        "UPDATE items SET institution_name = %s WHERE item_id = %s AND user_id = %s",
        (name, item_id, user_id),
    )


# --- Syncing -----------------------------------------------------------

def dollars_to_cents(amount):
    return round(amount * 100)


def get_cursor(conn, item_id):
    row = conn.execute("SELECT cursor FROM sync_state WHERE item_id = %s", (item_id,)).fetchone()
    return row["cursor"] if row else ""


def set_cursor(conn, item_id, cursor):
    conn.execute(
        """
        INSERT INTO sync_state (item_id, cursor) VALUES (%s, %s)
        ON CONFLICT (item_id) DO UPDATE SET cursor = EXCLUDED.cursor
        """,
        (item_id, cursor),
    )


def upsert_accounts(conn, user_id, item_id, accounts):
    # Refreshing a card's details never touches its nickname or removed flag,
    # and never moves it to another user.
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO accounts (account_id, item_id, user_id, name, type, subtype, mask)
            VALUES (%(account_id)s, %(item_id)s, %(user_id)s, %(name)s, %(type)s, %(subtype)s, %(mask)s)
            ON CONFLICT (account_id) DO UPDATE SET
                name = EXCLUDED.name,
                type = EXCLUDED.type,
                subtype = EXCLUDED.subtype,
                mask = EXCLUDED.mask
            WHERE accounts.user_id = EXCLUDED.user_id
            """,
            [
                {
                    "account_id": a["account_id"],
                    "item_id": item_id,
                    "user_id": user_id,
                    "name": a["name"],
                    "type": a["type"],
                    "subtype": a.get("subtype"),
                    "mask": a.get("mask"),
                }
                for a in accounts
            ],
        )


def upsert_transactions(conn, user_id, transactions):
    """Insert or update Plaid transaction objects, converting amounts to cents."""
    if not transactions:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO transactions (
                transaction_id, account_id, user_id, date, amount_cents, name, merchant_name,
                category_primary, category_detailed, pending, pending_transaction_id,
                iso_currency_code
            )
            VALUES (
                %(transaction_id)s, %(account_id)s, %(user_id)s, %(date)s, %(amount_cents)s,
                %(name)s, %(merchant_name)s, %(category_primary)s, %(category_detailed)s,
                %(pending)s, %(pending_transaction_id)s, %(iso_currency_code)s
            )
            ON CONFLICT (transaction_id) DO UPDATE SET
                account_id = EXCLUDED.account_id,
                date = EXCLUDED.date,
                amount_cents = EXCLUDED.amount_cents,
                name = EXCLUDED.name,
                merchant_name = EXCLUDED.merchant_name,
                category_primary = EXCLUDED.category_primary,
                category_detailed = EXCLUDED.category_detailed,
                pending = EXCLUDED.pending,
                pending_transaction_id = EXCLUDED.pending_transaction_id,
                iso_currency_code = EXCLUDED.iso_currency_code
            WHERE transactions.user_id = EXCLUDED.user_id
            """,
            [
                {
                    "transaction_id": t["transaction_id"],
                    "account_id": t["account_id"],
                    "user_id": user_id,
                    "date": date.fromisoformat(t["date"]),
                    "amount_cents": dollars_to_cents(t["amount"]),
                    "name": t["name"],
                    "merchant_name": t.get("merchant_name"),
                    "category_primary": (t.get("personal_finance_category") or {}).get("primary"),
                    "category_detailed": (t.get("personal_finance_category") or {}).get("detailed"),
                    "pending": bool(t.get("pending", False)),
                    "pending_transaction_id": t.get("pending_transaction_id"),
                    "iso_currency_code": t.get("iso_currency_code"),
                }
                for t in transactions
            ],
        )


def delete_transactions(conn, user_id, transaction_ids):
    if not transaction_ids:
        return
    conn.execute(
        "DELETE FROM transactions WHERE user_id = %s AND transaction_id = ANY(%s)",
        (user_id, list(transaction_ids)),
    )


def ignored_account_ids(conn, user_id):
    rows = conn.execute("SELECT account_id FROM accounts WHERE user_id = %s AND ignored", (user_id,))
    return {r["account_id"] for r in rows}


def log_sync(conn, user_id, started_at, status, counts=None, error=None):
    """Record one sync attempt. `counts` has added/modified/removed on success."""
    counts = counts or {}
    conn.execute(
        """
        INSERT INTO sync_log (user_id, started_at, finished_at, status, added, modified, removed, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            user_id, started_at, utc_now(), status,
            counts.get("added", 0), counts.get("modified", 0), counts.get("removed", 0),
            error,
        ),
    )


def last_sync_attempt(conn, user_id, status=None):
    """The user's most recent sync attempt, optionally only successful or failed ones.

    Timestamps are returned as datetimes; callers format them for the browser.
    """
    query = "SELECT * FROM sync_log WHERE user_id = %s"
    params = [user_id]
    if status:
        query += " AND status = %s"
        params.append(status)
    return conn.execute(query + " ORDER BY id DESC LIMIT 1", params).fetchone()


# --- Per-user settings -------------------------------------------------

def get_setting(conn, user_id, key, default=None):
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id = %s AND key = %s", (user_id, key)
    ).fetchone()
    return row["value"] if row else default


def set_setting(conn, user_id, key, value):
    conn.execute(
        """
        INSERT INTO user_settings (user_id, key, value) VALUES (%s, %s, %s)
        ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value
        """,
        (user_id, key, value),
    )


# --- Cards -------------------------------------------------------------

def account_label(account):
    """Nickname if set, otherwise the Plaid name with the card's last digits."""
    if account["nickname"]:
        return account["nickname"]
    return f'{account["name"]} ••{account["mask"]}' if account["mask"] else account["name"]


def list_accounts(conn, user_id):
    rows = conn.execute(
        """
        SELECT a.account_id, a.item_id, a.name, a.mask, a.nickname,
               (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.account_id) AS purchases
        FROM accounts a JOIN items i ON i.item_id = a.item_id
        WHERE a.user_id = %s AND a.type = 'credit' AND NOT a.ignored AND i.environment = %s
        ORDER BY a.name, a.mask
        """,
        (user_id, active_environment()),
    ).fetchall()
    return [
        {
            "account_id": r["account_id"],
            "item_id": r["item_id"],
            "label": account_label(r),
            "default_label": account_label({**r, "nickname": None}),
            "nickname": r["nickname"],
            "purchases": r["purchases"],
        }
        for r in rows
    ]


def remove_account(conn, user_id, account_id):
    """Permanently delete one of the user's cards' purchases and stop tracking it.

    The card stays, marked ignored, so later syncs skip it instead of bringing
    it back. Returns the number of purchases deleted, or None if it isn't one
    of the user's (visible) cards.
    """
    with conn.transaction():
        exists = conn.execute(
            "SELECT 1 FROM accounts WHERE account_id = %s AND user_id = %s AND NOT ignored",
            (account_id, user_id),
        ).fetchone()
        if not exists:
            return None
        deleted = conn.execute(
            "DELETE FROM transactions WHERE account_id = %s AND user_id = %s", (account_id, user_id)
        ).rowcount
        conn.execute(
            "UPDATE accounts SET ignored = TRUE WHERE account_id = %s AND user_id = %s",
            (account_id, user_id),
        )
    return deleted


def set_account_nickname(conn, user_id, account_id, nickname):
    """Set a display name, or reset to the default with None. False if it isn't the user's card."""
    cur = conn.execute(
        "UPDATE accounts SET nickname = %s WHERE account_id = %s AND user_id = %s",
        (nickname, account_id, user_id),
    )
    return cur.rowcount > 0


# --- Dashboard queries -------------------------------------------------
# Every dashboard read goes through VISIBLE: the user's own purchases, from
# banks linked in the active Plaid environment (so sandbox test data never
# mixes with real data), on cards they haven't removed.

VISIBLE = """
    FROM transactions t
    JOIN accounts a ON a.account_id = t.account_id
    JOIN items i ON i.item_id = a.item_id
    WHERE t.user_id = %(user_id)s AND i.environment = %(env)s AND NOT a.ignored
"""


def _scope(user_id, **extra):
    return {"user_id": user_id, "env": active_environment(), **extra}


def _month_range(month):
    """`month` is "YYYY-MM"; returns [first day, first day of next month)."""
    year, number = map(int, month.split("-"))
    start = date(year, number, 1)
    end = date(year + 1, 1, 1) if number == 12 else date(year, number + 1, 1)
    return start, end


def _in_month(user_id, month):
    start, end = _month_range(month)
    return _scope(user_id, start=start, end=end)


IN_MONTH = " AND t.date >= %(start)s AND t.date < %(end)s"


def list_months(conn, user_id):
    rows = conn.execute(
        "SELECT DISTINCT to_char(t.date, 'YYYY-MM') AS month" + VISIBLE + " ORDER BY month",
        _scope(user_id),
    ).fetchall()
    return [r["month"] for r in rows]


def month_totals(conn, user_id, month):
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(t.amount_cents), 0) AS total" + VISIBLE + IN_MONTH,
        _in_month(user_id, month),
    ).fetchone()
    return row["n"], int(row["total"])


def month_top_merchant(conn, user_id, month):
    row = conn.execute(
        "SELECT COALESCE(t.merchant_name, t.name) AS merchant, SUM(t.amount_cents) AS cents"
        + VISIBLE + IN_MONTH + " GROUP BY 1 ORDER BY cents DESC, merchant LIMIT 1",
        _in_month(user_id, month),
    ).fetchone()
    return {"merchant": row["merchant"], "cents": int(row["cents"])} if row else None


def month_categories(conn, user_id, month):
    rows = conn.execute(
        "SELECT COALESCE(t.category_primary, 'UNCATEGORIZED') AS category, "
        "SUM(t.amount_cents) AS cents, COUNT(*) AS n"
        + VISIBLE + IN_MONTH + " GROUP BY 1 ORDER BY cents DESC, category",
        _in_month(user_id, month),
    ).fetchall()
    return [{"category": r["category"], "cents": int(r["cents"]), "count": r["n"]} for r in rows]


def month_daily(conn, user_id, month):
    """Total spent and purchase count for each day of the month that had any."""
    rows = conn.execute(
        "SELECT t.date, SUM(t.amount_cents) AS cents, COUNT(*) AS n"
        + VISIBLE + IN_MONTH + " GROUP BY t.date ORDER BY t.date",
        _in_month(user_id, month),
    ).fetchall()
    return [{"date": r["date"].isoformat(), "cents": int(r["cents"]), "count": r["n"]} for r in rows]


def month_transactions(conn, user_id, month):
    rows = conn.execute(
        "SELECT t.transaction_id, t.date, t.amount_cents, t.pending, t.account_id, "
        "COALESCE(t.merchant_name, t.name) AS merchant, "
        "COALESCE(t.category_primary, 'UNCATEGORIZED') AS category"
        + VISIBLE + IN_MONTH + " ORDER BY t.date DESC, t.transaction_id",
        _in_month(user_id, month),
    ).fetchall()
    return [{**r, "date": r["date"].isoformat()} for r in rows]


def category_by_month(conn, user_id):
    """Return {month: {category: cents}} across all of the user's purchases."""
    rows = conn.execute(
        "SELECT to_char(t.date, 'YYYY-MM') AS month, "
        "COALESCE(t.category_primary, 'UNCATEGORIZED') AS category, "
        "SUM(t.amount_cents) AS cents" + VISIBLE + " GROUP BY 1, 2",
        _scope(user_id),
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["month"], {})[r["category"]] = int(r["cents"])
    return result


def recent_transactions(conn, user_id, limit=20):
    return conn.execute(
        "SELECT t.*" + VISIBLE + " ORDER BY t.date DESC, t.transaction_id LIMIT %(limit)s",
        _scope(user_id, limit=limit),
    ).fetchall()


def spending_summary(conn, user_id):
    """Return (purchase_count, total_cents) across all of the user's purchases."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(t.amount_cents), 0) AS total" + VISIBLE,
        _scope(user_id),
    ).fetchone()
    return row["n"], int(row["total"])


if __name__ == "__main__":
    init_schema()
    print(f"Schema is up to date in {database_url()}")
