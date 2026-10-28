import os

import requests

import database  # also loads .env

ENVIRONMENT = database.active_environment()
BASE_URL = f"https://{ENVIRONMENT}.plaid.com"


def post(path, body=None, timeout=30):
    """Call a Plaid endpoint with our API keys added and return the JSON.

    Raises RuntimeError carrying Plaid's own explanation when the call fails.
    """
    response = requests.post(
        f"{BASE_URL}{path}",
        json={
            "client_id": os.getenv("PLAID_CLIENT_ID"),
            "secret": os.getenv("PLAID_SECRET"),
            **(body or {}),
        },
        timeout=timeout,
    )
    if not response.ok:
        try:
            detail = response.json().get("error_message")
        except ValueError:
            detail = None
        raise RuntimeError(f"Plaid error {response.status_code}: {detail or response.reason}")
    return response.json()
