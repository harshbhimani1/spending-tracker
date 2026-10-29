"""Only valid Supabase sessions get in, and each one only reaches its own data."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

import app as app_module
import auth
import database
from conftest import seed_user, txn

ISSUER = "https://test-project.supabase.co/auth/v1"
PROJECT_KEY = ec.generate_private_key(ec.SECP256R1())  # stands in for Supabase's signing key
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())    # an attacker's key


@pytest.fixture(autouse=True)
def supabase_keys(monkeypatch):
    # Instead of fetching Supabase's published keys, verify against our test key.
    monkeypatch.setattr(auth, "_signing_key_for", lambda token: PROJECT_KEY.public_key())


def token(sub="user-1", email="Me@Example.com", key=PROJECT_KEY, algorithm="ES256", **overrides):
    claims = {"sub": sub, "email": email, "aud": "authenticated", "iss": ISSUER,
              "exp": int(time.time()) + 3600, "iat": int(time.time()), **overrides}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=algorithm)


def hs256_signed_with_public_key():
    """The classic 'algorithm confusion' forgery: HMAC-sign a token using the
    server's *public* key as the secret. Built by hand because PyJWT refuses to."""
    import base64, hashlib, hmac, json

    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    public_pem = PROJECT_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"sub": "user-1", "aud": "authenticated", "iss": ISSUER,
                              "exp": int(time.time()) + 3600}).encode())
    signature = b64(hmac.new(public_pem, header + b"." + payload, hashlib.sha256).digest())
    return (header + b"." + payload + b"." + signature).decode()


def test_a_valid_session_identifies_the_user():
    user = auth.verify_token(token())
    assert user.user_id == "user-1" and user.email == "me@example.com"


@pytest.mark.parametrize("bad", [
    pytest.param(lambda: token(key=OTHER_KEY), id="signed-by-another-key"),
    pytest.param(lambda: token(exp=int(time.time()) - 120), id="expired"),
    pytest.param(lambda: token(aud="anon"), id="wrong-audience"),
    pytest.param(lambda: token(iss="https://evil.supabase.co/auth/v1"), id="wrong-issuer"),
    pytest.param(lambda: token(sub=None), id="no-user-id"),
    pytest.param(lambda: jwt.encode({"sub": "user-1", "aud": "authenticated", "iss": ISSUER,
                                     "exp": int(time.time()) + 3600}, None, algorithm="none"), id="alg-none"),
    pytest.param(hs256_signed_with_public_key, id="hs256-key-confusion"),
    pytest.param(lambda: "not-a-token", id="garbage"),
    pytest.param(lambda: "", id="missing"),
])
def test_invalid_sessions_are_rejected(bad):
    with pytest.raises(auth.AuthError):
        auth.verify_token(bad())


def test_optional_email_allowlist(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "mom@example.com, ME@example.com")
    assert auth.verify_token(token(email="me@example.com")).user_id == "user-1"
    with pytest.raises(auth.AuthError, match="isn't allowed"):
        auth.verify_token(token(email="stranger@example.com"))


def test_bearer_header_parsing():
    assert auth.bearer_token("Bearer abc.def") == "abc.def"
    assert auth.bearer_token("bearer abc") == "abc"
    assert auth.bearer_token("Basic abc") == ""
    assert auth.bearer_token(None) == ""


# --- Through the real API (no test override of who's signed in) -----------

@pytest.fixture
def client():
    app_module._known_users.clear()
    return TestClient(app_module.app)


def protected_routes():
    for route in app_module.app.routes:
        path = getattr(route, "path", "")
        # /api/config is public; /api/cron/sync has its own secret (tested in test_cron.py).
        if path.startswith("/api/") and path not in ("/api/config", "/api/cron/sync"):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                yield method, path.replace("{item_id}", "x").replace("{account_id}", "x")


def test_every_api_endpoint_requires_sign_in(client):
    routes = list(protected_routes())
    assert len(routes) >= 14  # all data and action endpoints
    for method, path in routes:
        url = path + ("?month=2026-10" if path in ("/api/overview", "/api/transactions") else "")
        assert client.request(method, url).status_code == 401, (method, path)
        bad = {"Authorization": f"Bearer {token(key=OTHER_KEY)}"}
        assert client.request(method, url, headers=bad).status_code == 401, (method, path)


def test_page_and_config_are_public_and_expose_no_secrets(client):
    assert client.get("/").status_code == 200
    config = client.get("/api/config").json()
    assert config == {"supabase_url": "https://test-project.supabase.co", "supabase_publishable_key": "sb_publishable_test"}


def test_each_signed_in_person_sees_only_their_own_data(client):
    seed_user("user-1", "item-1", ["acct-1"], [txn("t1", "acct-1", 11.00)], bank="Mine")
    seed_user("user-2", "item-2", ["acct-2"], [txn("t2", "acct-2", 22.00)], bank="Theirs")
    me = {"Authorization": f"Bearer {token(sub='user-1')}"}
    them = {"Authorization": f"Bearer {token(sub='user-2', email='mom@example.com')}"}

    assert [i["institution_name"] for i in client.get("/api/items", headers=me).json()["items"]] == ["Mine"]
    assert [i["institution_name"] for i in client.get("/api/items", headers=them).json()["items"]] == ["Theirs"]
    assert client.delete("/api/items/item-2", headers=me).status_code == 404
    with database.connect() as conn:
        emails = {r["user_id"]: r["email"] for r in conn.execute("SELECT user_id, email FROM users")}
    assert emails == {"user-1": "me@example.com", "user-2": "mom@example.com"}


def test_first_sign_in_creates_the_user(client):
    headers = {"Authorization": f"Bearer {token(sub='brand-new', email='new@example.com')}"}
    assert client.get("/api/items", headers=headers).json()["items"] == []
    with database.connect() as conn:
        assert database.list_user_ids(conn) == ["brand-new"]
