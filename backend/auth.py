"""Who is signed in: verifies the Supabase session token sent with each request.

The browser signs in with Supabase and sends its access token as
`Authorization: Bearer <token>`. We check the token's signature against the
project's public signing keys (published by Supabase), plus its issuer,
audience and expiry. No Supabase secret is needed or stored here.
"""
import os
from dataclasses import dataclass

import jwt
from dotenv import load_dotenv

load_dotenv()


class AuthError(Exception):
    """The request isn't from a valid, allowed signed-in user."""


@dataclass(frozen=True)
class SignedInUser:
    user_id: str
    email: str | None


def supabase_url():
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL is not set")
    return url


def allowed_emails():
    """Optional extra lock on top of Supabase's invite-only sign-up."""
    raw = os.getenv("ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


_jwks_clients = {}


def _signing_key_for(token):
    url = supabase_url() + "/auth/v1/.well-known/jwks.json"
    client = _jwks_clients.get(url)
    if client is None:
        # Caches the keys; refetches when a token names a key it hasn't seen
        # (e.g. after a key rotation in the Supabase dashboard).
        client = _jwks_clients[url] = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)
    return client.get_signing_key_from_jwt(token).key


def verify_token(token):
    """Return the signed-in user for a Supabase access token, or raise AuthError."""
    if not token:
        raise AuthError("Not signed in")
    try:
        key = _signing_key_for(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=["ES256"],  # only the project's ECC signing keys
            audience="authenticated",
            issuer=supabase_url() + "/auth/v1",
            options={"require": ["exp", "sub", "iss", "aud"]},
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"Invalid session: {exc}") from exc

    email = (claims.get("email") or "").lower() or None
    allowed = allowed_emails()
    if allowed and email not in allowed:
        raise AuthError("This account isn't allowed to use this app")
    return SignedInUser(user_id=claims["sub"], email=email)


def bearer_token(authorization_header):
    scheme, _, token = (authorization_header or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""
