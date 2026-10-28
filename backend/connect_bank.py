#!/usr/bin/env python3
"""Connect Plaid's sandbox test bank without the Link window, for quick testing.

Each run adds another test bank. Real banks are connected from the dashboard.
"""
from contextlib import closing

import database
import plaid_client

if plaid_client.ENVIRONMENT != "sandbox":
    raise SystemExit("This shortcut only works with PLAID_ENV=sandbox; use Connect a bank in the dashboard.")

# First Platypus Bank, Plaid's sandbox test institution
institution_id = "ins_109508"
institution_name = "First Platypus Bank"

# Step 1: sandbox shortcut for the Link flow, hands back a public token
public_token = plaid_client.post("/sandbox/public_token/create", {
    "institution_id": institution_id,
    "initial_products": ["transactions"],
})["public_token"]
print(f"got public token for {institution_id}")

# Step 2: trade the short-lived public token for a permanent access token
data = plaid_client.post("/item/public_token/exchange", {"public_token": public_token})
access_token = data["access_token"]
item_id = data["item_id"]

with closing(database.connect()) as conn, conn:
    database.add_item(conn, item_id, access_token, institution_name, "sandbox")
print(f"saved item {item_id} to the database")

# Step 3: confirm the token works by pulling the linked accounts
accounts = plaid_client.post("/accounts/get", {"access_token": access_token})["accounts"]
print(f"linked {len(accounts)} accounts:")
for account in accounts:
    balance = account["balances"]["current"]
    print(f"  {account['name']} ({account['subtype']})  ${balance}")
