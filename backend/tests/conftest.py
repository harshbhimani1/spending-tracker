"""Shared test setup.

Tests run against a separate Postgres database (TEST_DATABASE_URL, default
spending_tracker_test) that is wiped before every test, and Plaid is replaced
with an in-memory fake so nothing leaves the machine.
"""
import os
import sys
from pathlib import Path

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost/spending_tracker_test")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["PLAID_ENV"] = "sandbox"
os.environ["LOCAL_USER_ID"] = "local-dev"
# A fake Supabase project; tests sign their own tokens (see test_auth.py).
os.environ["SUPABASE_URL"] = "https://test-project.supabase.co"
os.environ["SUPABASE_PUBLISHABLE_KEY"] = "sb_publishable_test"
os.environ["ALLOWED_EMAILS"] = ""
# A throwaway encryption key just for this test run.
from cryptography.fernet import Fernet  # noqa: E402
os.environ["TOKEN_ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import database
import plaid_client


@pytest.fixture(autouse=True)
def fresh_db():
    # Guard: this wipes the whole database, so never point it at a real one.
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[-1]
    assert "test" in db_name, f"refusing to wipe non-test database {db_name!r}"
    with database.connect() as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    database.init_schema()
    yield


def txn(transaction_id, account_id, amount, day="2026-10-05", category="FOOD_AND_DRINK", merchant="Shop"):
    """A Plaid-shaped transaction."""
    return {
        "transaction_id": transaction_id, "account_id": account_id, "date": day,
        "amount": amount, "name": merchant, "merchant_name": merchant,
        "personal_finance_category": {"primary": category, "detailed": category + "_OTHER"},
    }


def seed_user(user_id, item_id, cards, transactions=(), environment="sandbox", bank="Bank"):
    """Create a user with one bank, its credit cards, and some purchases."""
    with database.connect() as conn:
        database.ensure_user(conn, user_id)
        database.add_item(conn, user_id, item_id, f"access-{environment}-{item_id}", bank, environment)
        database.upsert_accounts(conn, user_id, item_id, [
            {"account_id": c, "name": f"Card {c}", "type": "credit", "mask": c[-4:]} for c in cards
        ])
        database.upsert_transactions(conn, user_id, list(transactions))
        database.set_cursor(conn, item_id, f"cursor-{item_id}")


class FakePlaid:
    """Stands in for plaid_client.post. Configure per access token."""

    def __init__(self):
        self.accounts = {}   # token -> list of Plaid account dicts
        self.pages = {}      # token -> list of /transactions/sync responses
        self.fail = {}       # (path, token) -> PlaidError to raise
        self.calls = []      # (path, token)

    def post(self, path, body=None, timeout=30):
        token = (body or {}).get("access_token")
        self.calls.append((path, token))
        if (path, token) in self.fail:
            raise self.fail[(path, token)]
        if path == "/accounts/get":
            return {"accounts": self.accounts[token]}
        if path == "/transactions/sync":
            return self.pages[token].pop(0)
        if path == "/item/remove":
            return {"request_id": "req"}
        if path == "/link/token/create":
            return {"link_token": f"link-sandbox-for-{body['user']['client_user_id']}"}
        if path == "/item/public_token/exchange":
            return {"item_id": f"item-from-{body['public_token']}", "access_token": f"access-sandbox-{body['public_token']}"}
        raise AssertionError(f"unexpected Plaid call {path}")


@pytest.fixture
def fake_plaid(monkeypatch):
    fake = FakePlaid()
    monkeypatch.setattr(plaid_client, "post", fake.post)
    return fake
