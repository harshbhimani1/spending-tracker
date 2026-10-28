#!/usr/bin/env python3
"""Connect to Plaid's sandbox test bank and save the access token for later use."""
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

client_id = os.getenv("PLAID_CLIENT_ID")
key = os.getenv("PLAID_SECRET")
base_url = f"https://{os.getenv('PLAID_ENV', 'sandbox')}.plaid.com"

# First Platypus Bank, Plaid's sandbox test institution
institution_id = "ins_109508"
token_file = Path(__file__).parent / "plaid_token.json"

# Step 1: sandbox shortcut for the Link flow, hands back a public token
response = requests.post(
    f"{base_url}/sandbox/public_token/create",
    json={
        "client_id": client_id,
        "secret": key,
        "institution_id": institution_id,
        "initial_products": ["transactions"],
    },
    timeout=10,
)
response.raise_for_status()
public_token = response.json()["public_token"]
print(f"got public token for {institution_id}")

# Step 2: trade the short-lived public token for a permanent access token
response = requests.post(
    f"{base_url}/item/public_token/exchange",
    json={
        "client_id": client_id,
        "secret": key,
        "public_token": public_token,
    },
    timeout=10,
)
response.raise_for_status()
data = response.json()
access_token = data["access_token"]
item_id = data["item_id"]

token_file.write_text(json.dumps({"access_token": access_token, "item_id": item_id}, indent=2))
token_file.chmod(0o600)
print(f"saved access token for item {item_id} to {token_file.name}")

# Step 3: confirm the token works by pulling the linked accounts
response = requests.post(
    f"{base_url}/accounts/get",
    json={"client_id": client_id, "secret": key, "access_token": access_token},
    timeout=10,
)
response.raise_for_status()
accounts = response.json()["accounts"]
print(f"linked {len(accounts)} accounts:")
for account in accounts:
    balance = account["balances"]["current"]
    print(f"  {account['name']} ({account['subtype']})  ${balance}")
