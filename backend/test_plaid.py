#!/usr/bin/env python3
import os
import requests
from dotenv import load_dotenv

load_dotenv()

client_id = os.getenv("PLAID_CLIENT_ID")
key = os.getenv("PLAID_SECRET")
url = "https://sandbox.plaid.com/institutions/get"

payload = {
    "client_id": client_id,
    "secret": key,
    "count": 5,
    "offset": 0,
    "country_codes": ["US"],
}

response = requests.post(url, json=payload, timeout=10)

response.raise_for_status

if response.ok:
    data = response.json()
    print(f"total institutions: {data['total']}")
    for institution in data["institutions"]:
        print(f"  {institution['institution_id']}  {institution['name']}")
else:
    print(response.text)
