import os

import requests

import database  # also loads .env

ENVIRONMENT = database.active_environment()
BASE_URL = f"https://{ENVIRONMENT}.plaid.com"


class PlaidError(RuntimeError):
    """A Plaid call that came back with an error; `code` is Plaid's error_code."""

    def __init__(self, status, code, message):
        super().__init__(f"Plaid error {status}: {message}")
        self.code = code


def post(path, body=None, timeout=30):
    """Call a Plaid endpoint with our API keys added and return the JSON.

    Raises PlaidError (a RuntimeError) carrying Plaid's own explanation when the call fails.
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
            error = response.json()
        except ValueError:
            error = {}
        raise PlaidError(response.status_code, error.get("error_code"), error.get("error_message") or response.reason)
    return response.json()
