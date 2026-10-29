"""The HTTP API only ever shows or changes the signed-in user's data."""
import pytest
from fastapi.testclient import TestClient

import app as app_module
import database
from conftest import seed_user, txn

OCT = "2026-10"


@pytest.fixture
def client():
    # No `with`: the app's startup (background scheduler) stays off in tests.
    yield TestClient(app_module.app)
    app_module.app.dependency_overrides.clear()


def as_user(user_id):
    """Make following requests come from this user (who exists, as after sign-in)."""
    with database.connect() as conn:
        database.ensure_user(conn, user_id)
    app_module.app.dependency_overrides[app_module.current_user] = lambda: user_id


def seed():
    seed_user("alice", "item-a", ["acct-a1"], [txn("ta1", "acct-a1", 20.00, "2026-10-02", "TRAVEL", "Airline")], bank="Alice Bank")
    seed_user("bob", "item-b", ["acct-b1"], [txn("tb1", "acct-b1", 70.00, "2026-10-02", "MEDICAL", "Clinic")], bank="Bob Bank")


def test_reads_are_scoped_to_the_signed_in_user(client):
    seed()
    as_user("alice")
    assert client.get("/api/months").json() == {"months": [OCT]}
    overview = client.get(f"/api/overview?month={OCT}").json()
    assert overview["total_cents"] == 2000 and [c["category"] for c in overview["categories"]] == ["TRAVEL"]
    assert [t["transaction_id"] for t in client.get(f"/api/transactions?month={OCT}").json()["transactions"]] == ["ta1"]
    assert client.get("/api/trend").json()["categories"] == ["TRAVEL"]
    assert [a["account_id"] for a in client.get("/api/accounts").json()["accounts"]] == ["acct-a1"]
    items = client.get("/api/items").json()["items"]
    assert [i["institution_name"] for i in items] == ["Alice Bank"]
    assert "access" not in str(items)  # tokens never leave the server

    as_user("bob")
    assert client.get(f"/api/overview?month={OCT}").json()["total_cents"] == 7000
    assert [i["institution_name"] for i in client.get("/api/items").json()["items"]] == ["Bob Bank"]


def test_cannot_change_another_users_cards_or_banks(client, fake_plaid):
    seed()
    as_user("alice")
    assert client.put("/api/accounts/acct-b1", json={"nickname": "mine"}).status_code == 404
    assert client.delete("/api/accounts/acct-b1").status_code == 404
    assert client.delete("/api/items/item-b").status_code == 404
    assert fake_plaid.calls == []  # Bob's bank was never disconnected at Plaid

    as_user("bob")
    accounts = client.get("/api/accounts").json()["accounts"]
    assert accounts[0]["nickname"] is None and accounts[0]["purchases"] == 1
    assert client.get(f"/api/overview?month={OCT}").json()["total_cents"] == 7000


def test_users_can_manage_their_own_cards_and_banks(client, fake_plaid):
    seed()
    as_user("alice")
    renamed = client.put("/api/accounts/acct-a1", json={"nickname": "Travel card"}).json()["accounts"]
    assert renamed[0]["label"] == "Travel card"
    assert client.delete("/api/items/item-a").json() == {"deleted": 1}
    assert fake_plaid.calls == [("/item/remove", "access-sandbox-item-a")]
    assert client.get("/api/items").json()["items"] == []
    as_user("bob")
    assert len(client.get("/api/items").json()["items"]) == 1


def test_connecting_a_bank_saves_it_to_the_signed_in_user(client, fake_plaid):
    as_user("alice")
    assert client.post("/api/plaid/link-token").json() == {"link_token": "link-sandbox-for-alice"}

    new_token = "access-sandbox-pub-1"
    fake_plaid.accounts[new_token] = [{"account_id": "acct-new", "name": "New Card", "type": "credit", "mask": "4242"}]
    fake_plaid.pages[new_token] = [{"added": [txn("first", "acct-new", 12.34)], "modified": [], "removed": [],
                                    "next_cursor": "c1", "has_more": False}]
    result = client.post("/api/plaid/exchange", json={"public_token": "pub-1", "institution_name": "New Bank"}).json()
    assert result["item_id"] == "item-from-pub-1"
    assert result["status"]["last_attempt"]["status"] == "success"  # first sync ran right away

    assert [i["institution_name"] for i in client.get("/api/items").json()["items"]] == ["New Bank"]
    assert client.get(f"/api/overview?month={OCT}").json()["total_cents"] == 1234
    as_user("bob")
    assert client.get("/api/items").json()["items"] == []
    assert client.get("/api/sync").json()["last_attempt"] is None


def test_sync_settings_are_per_user(client):
    seed()
    as_user("alice")
    assert client.put("/api/sync/interval", json={"interval": "1w"}).json()["interval"] == "1w"
    assert client.put("/api/sync/interval", json={"interval": "5m"}).status_code == 422
    as_user("bob")
    assert client.get("/api/sync").json()["interval"] == "1d"

