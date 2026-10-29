"""Encryption for Plaid access tokens at rest.

A Plaid access token is a standing key to someone's bank data, so it's never
stored in plain text. Each token is encrypted with Fernet (AES + HMAC, from
the `cryptography` package) together with the user and bank it belongs to;
decrypting checks both, so a token copied into another row won't open.

Keys live outside the database in TOKEN_ENCRYPTION_KEYS: a comma-separated
list, newest first. New tokens use the first key; any listed key can decrypt,
which allows rotating keys without downtime.

Command line (run from backend/):
    python token_crypto.py init-key        add a new key to .env (never printed)
    python token_crypto.py encrypt-existing   encrypt any plain-text tokens left in the database
    python token_crypto.py rotate-all      re-encrypt every token with the newest key
"""
import json
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from dotenv import load_dotenv

load_dotenv()

PREFIX = "enc1:"
ENV_NAME = "TOKEN_ENCRYPTION_KEYS"


class TokenKeyError(RuntimeError):
    """The encryption key is missing or malformed."""


class TokenDecryptError(RuntimeError):
    """A stored token can't be decrypted (wrong key, tampered, or not this bank's)."""


def _cipher():
    keys = [k.strip() for k in os.getenv(ENV_NAME, "").split(",") if k.strip()]
    if not keys:
        raise TokenKeyError(f"{ENV_NAME} is not set; run `python token_crypto.py init-key` in backend/")
    try:
        return MultiFernet([Fernet(k) for k in keys])
    except ValueError as exc:
        raise TokenKeyError(f"{ENV_NAME} contains an invalid key") from exc


def check_key():
    """Fail fast (e.g. at startup) if tokens couldn't be encrypted."""
    _cipher()


def is_encrypted(stored):
    return stored.startswith(PREFIX)


def encrypt(token, user_id, item_id):
    payload = json.dumps({"token": token, "user": user_id, "item": item_id}).encode()
    return PREFIX + _cipher().encrypt(payload).decode()


def decrypt(stored, user_id, item_id):
    if not is_encrypted(stored):
        raise TokenDecryptError("token is not encrypted")
    try:
        payload = json.loads(_cipher().decrypt(stored[len(PREFIX):].encode()))
    except InvalidToken as exc:
        raise TokenDecryptError("token can't be decrypted (wrong key, or it was tampered with)") from exc
    if payload.get("user") != user_id or payload.get("item") != item_id:
        raise TokenDecryptError("token belongs to a different bank or user")
    return payload["token"]


def reencrypt(stored):
    """Re-encrypt with the newest key, keeping the same contents."""
    return PREFIX + _cipher().rotate(stored[len(PREFIX):].encode()).decode()


def init_key(env_file):
    """Append a freshly generated key to the .env file, without ever printing it."""
    path = Path(env_file)
    text = path.read_text() if path.exists() else ""
    if any(line.startswith(f"{ENV_NAME}=") for line in text.splitlines()):
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + f"{ENV_NAME}={Fernet.generate_key().decode()}\n")
    path.chmod(0o600)
    return True


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "init-key":
        env_file = Path(__file__).parent / ".env"
        if init_key(env_file):
            print(f"Added a new {ENV_NAME} to {env_file.name} (not shown). Back it up somewhere safe.")
        else:
            print(f"{env_file.name} already has {ENV_NAME}; left it unchanged.")
    elif command in ("encrypt-existing", "rotate-all"):
        import database
        with database.connect() as conn:
            if command == "encrypt-existing":
                print(f"Encrypted {database.encrypt_plaintext_tokens(conn)} plain-text token(s).")
            else:
                print(f"Re-encrypted {database.reencrypt_tokens(conn)} token(s) with the newest key.")
    else:
        print(__doc__)
        sys.exit(2)
