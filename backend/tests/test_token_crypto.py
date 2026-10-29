"""Plaid access tokens are never stored readable, and only open for their own bank."""
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app as app_module
import database
import token_crypto
from conftest import seed_user, txn

TOKEN = "access-fake-1234abcd-for-tests"


def test_round_trip_and_ciphertext_reveals_nothing():
    stored = token_crypto.encrypt(TOKEN, "alice", "item-a")
    assert stored.startswith("enc1:") and "access-" not in stored and "alice" not in stored
    assert token_crypto.encrypt(TOKEN, "alice", "item-a") != stored  # fresh randomness every time
    assert token_crypto.decrypt(stored, "alice", "item-a") == TOKEN


@pytest.mark.parametrize("user, item", [("bob", "item-a"), ("alice", "item-b")])
def test_a_token_only_opens_for_its_own_user_and_bank(user, item):
    stored = token_crypto.encrypt(TOKEN, "alice", "item-a")
    with pytest.raises(token_crypto.TokenDecryptError, match="different bank or user"):
        token_crypto.decrypt(stored, user, item)


def test_tampering_or_the_wrong_key_is_detected(monkeypatch):
    stored = token_crypto.encrypt(TOKEN, "alice", "item-a")
    flipped = stored[:-6] + ("A" if stored[-6] != "A" else "B") + stored[-5:]
    with pytest.raises(token_crypto.TokenDecryptError):
        token_crypto.decrypt(flipped, "alice", "item-a")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    with pytest.raises(token_crypto.TokenDecryptError, match="wrong key"):
        token_crypto.decrypt(stored, "alice", "item-a")


def test_missing_or_bad_key_refuses_to_work(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", "")
    with pytest.raises(token_crypto.TokenKeyError, match="not set"):
        token_crypto.encrypt(TOKEN, "alice", "item-a")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", "not-a-real-key")
    with pytest.raises(token_crypto.TokenKeyError, match="invalid"):
        token_crypto.check_key()


def test_key_rotation(monkeypatch):
    old = token_crypto.os.environ["TOKEN_ENCRYPTION_KEYS"]
    seed_user("alice", "item-a", ["acct-a1"])
    new = Fernet.generate_key().decode()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", f"{new},{old}")  # newest first, old still accepted
    with database.connect() as conn:
        assert database.get_item(conn, "alice", "item-a")["access_token"] == "access-sandbox-item-a"
        assert database.reencrypt_tokens(conn) == 1
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", new)  # old key retired
    with database.connect() as conn:
        assert database.get_item(conn, "alice", "item-a")["access_token"] == "access-sandbox-item-a"


def test_the_database_only_ever_holds_ciphertext():
    seed_user("alice", "item-a", ["acct-a1"])
    with database.connect() as conn:
        raw = conn.execute("SELECT access_token FROM items").fetchone()["access_token"]
        assert raw.startswith("enc1:") and "access-sandbox" not in raw
        assert database.get_item(conn, "alice", "item-a")["access_token"] == "access-sandbox-item-a"


def test_plain_text_tokens_from_before_get_encrypted():
    seed_user("alice", "item-a", ["acct-a1"])
    with database.connect() as conn:
        conn.execute("UPDATE items SET access_token = 'access-sandbox-legacy' WHERE item_id = 'item-a'")
        assert database.encrypt_plaintext_tokens(conn) == 1
        assert database.encrypt_plaintext_tokens(conn) == 0  # nothing left to do
        raw = conn.execute("SELECT access_token FROM items").fetchone()["access_token"]
        assert raw.startswith("enc1:")
        assert database.get_item(conn, "alice", "item-a")["access_token"] == "access-sandbox-legacy"


def test_a_moved_token_disables_only_that_bank(fake_plaid):
    """Copying Bob's encrypted token into Alice's bank row doesn't give Alice Bob's bank."""
    seed_user("alice", "item-a", ["acct-a1"], [txn("ta", "acct-a1", 1.00)], bank="Alice Bank")
    seed_user("bob", "item-b", ["acct-b1"], bank="Bob Bank")
    with database.connect() as conn:
        conn.execute("UPDATE items SET access_token = (SELECT access_token FROM items WHERE item_id = 'item-b') "
                     "WHERE item_id = 'item-a'")
        item = database.get_item(conn, "alice", "item-a")
        assert item["access_token"] is None and "different bank or user" in item["token_error"]
        # The rest of Alice's dashboard still works.
        assert database.spending_summary(conn, "alice") == (1, 100)

    import get_transactions
    with pytest.raises(RuntimeError, match="reconnect this bank"):
        get_transactions.sync_and_log("alice")
    assert all(token != "access-sandbox-item-b" for _, token in fake_plaid.calls)  # Bob's token never used

    app_module.app.dependency_overrides[app_module.current_user] = lambda: "alice"
    try:
        response = TestClient(app_module.app).delete("/api/items/item-a")
        assert response.status_code == 409 and "can't be decrypted" in response.json()["detail"]
    finally:
        app_module.app.dependency_overrides.clear()


def test_init_key_adds_a_key_once_without_breaking_the_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PLAID_ENV=sandbox")  # no trailing newline, like a hand-edited file
    assert token_crypto.init_key(env) is True
    assert token_crypto.init_key(env) is False  # never replaces an existing key
    lines = env.read_text().splitlines()
    assert lines[0] == "PLAID_ENV=sandbox" and lines[1].startswith("TOKEN_ENCRYPTION_KEYS=")
    Fernet(lines[1].split("=", 1)[1])  # a valid key
    assert oct(env.stat().st_mode)[-3:] == "600"
