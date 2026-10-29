import calendar
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Optional

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import auth
import database
import plaid_client
import token_crypto
from scheduler import INTERVALS, Scheduler, SyncBusy

STATIC_DIR = Path(__file__).parent / "static"
MAX_TREND_MONTHS = 12

scheduler = Scheduler()


@asynccontextmanager
async def lifespan(app):
    # Refuse to start without an encryption key, rather than store bank tokens in plain text.
    token_crypto.check_key()
    database.init_schema()
    with database.connect() as conn:
        database.encrypt_plaintext_tokens(conn)
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Spending dashboard", lifespan=lifespan)

Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


def db():
    return database.connect()


_known_users = set()


def current_user(authorization: Annotated[Optional[str], Header()] = None) -> str:
    """Who is making this request: the Supabase user whose session token it carries.

    Every endpoint that takes `user: User` is scoped to this id, and answers
    401 when there's no valid session.
    """
    try:
        signed_in = auth.verify_token(auth.bearer_token(authorization))
    except auth.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"})
    if (signed_in.user_id, signed_in.email) not in _known_users:
        with db() as conn:
            database.ensure_user(conn, signed_in.user_id, signed_in.email)
        _known_users.add((signed_in.user_id, signed_in.email))
    return signed_in.user_id


User = Annotated[str, Depends(current_user)]


def previous_month(month):
    year, number = map(int, month.split("-"))
    return f"{year - 1}-12" if number == 1 else f"{year}-{number - 1:02d}"


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def config():
    """Public: tells the page which Supabase project to sign in with.
    The publishable key is meant for browsers; no secret is exposed."""
    return {
        "supabase_url": auth.supabase_url(),
        "supabase_publishable_key": os.getenv("SUPABASE_PUBLISHABLE_KEY", ""),
    }


@app.get("/api/months")
def months(user: User):
    with db() as conn:
        return {"months": database.list_months(conn, user)}


@app.get("/api/overview")
def overview(user: User, month: Month):
    with db() as conn:
        count, total = database.month_totals(conn, user, month)
        _, previous_total = database.month_totals(conn, user, previous_month(month))
        top_merchant = database.month_top_merchant(conn, user, month)
        categories = database.month_categories(conn, user, month)
        daily = database.month_daily(conn, user, month)

    # A month still in progress is averaged over the days elapsed so far.
    today = date.today()
    year, number = map(int, month.split("-"))
    if (year, number) == (today.year, today.month):
        days = today.day
    else:
        days = calendar.monthrange(year, number)[1]

    return {
        "total_cents": total,
        "count": count,
        "previous_total_cents": previous_total,
        "daily_average_cents": round(total / days),
        "top_merchant": top_merchant,
        "categories": categories,
        "daily": daily,
    }


@app.get("/api/transactions")
def transactions(user: User, month: Month):
    with db() as conn:
        return {"transactions": database.month_transactions(conn, user, month)}


@app.get("/api/trend")
def trend(user: User):
    with db() as conn:
        by_month = database.category_by_month(conn, user)

    months = sorted(by_month)[-MAX_TREND_MONTHS:]
    totals = {}
    for month in months:
        for category, cents in by_month[month].items():
            totals[category] = totals.get(category, 0) + cents
    categories = sorted(totals, key=totals.get, reverse=True)
    return {
        "months": months,
        "categories": categories,
        "series": {c: [by_month[m].get(c, 0) for m in months] for c in categories},
    }


@app.get("/api/accounts")
def accounts(user: User):
    with db() as conn:
        return {"accounts": database.list_accounts(conn, user)}


@app.get("/api/items")
def items(user: User):
    """The user's connected banks in the active environment (never exposes access tokens)."""
    with db() as conn:
        banks = database.list_items(conn, user)
        counts = database.item_counts(conn, user)
    return {
        "environment": database.active_environment(),
        "items": [
            {
                "item_id": b["item_id"],
                "institution_name": b["institution_name"],
                "created_at": b["created_at"],
                "cards": counts.get(b["item_id"], {}).get("cards", 0),
                "purchases": counts.get(b["item_id"], {}).get("purchases", 0),
            }
            for b in banks
        ],
    }


@app.delete("/api/items/{item_id}")
def remove_bank(user: User, item_id: str):
    """Disconnect one of the user's banks at Plaid, then delete it and all its data here.

    If Plaid can't be told, nothing is deleted, so the bank never ends up
    removed here while still connected (and billed) there.
    """
    with scheduler.paused(user):
        with db() as conn:
            bank = database.get_item(conn, user, item_id)
        if bank is None:
            raise HTTPException(status_code=404, detail="Unknown bank")
        if bank["access_token"] is None:
            raise HTTPException(
                status_code=409,
                detail="This bank's saved connection can't be decrypted, so it can't be disconnected at Plaid. "
                       "Restore the previous encryption key, or remove the connection in the Plaid dashboard.",
            )
        try:
            plaid_client.post("/item/remove", {"access_token": bank["access_token"]})
        except plaid_client.PlaidError as exc:
            # Already gone on Plaid's side (e.g. revoked at the bank): nothing to revoke.
            if exc.code not in ("ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"):
                raise HTTPException(
                    status_code=502,
                    detail=f"Could not disconnect the bank at Plaid, so nothing was deleted. {exc}",
                )
        except requests.RequestException:
            raise HTTPException(
                status_code=502,
                detail="Could not reach Plaid, so nothing was deleted. Check your connection and try again.",
            )
        with db() as conn:
            deleted = database.remove_item(conn, user, item_id)
    return {"deleted": deleted or 0}


def call_plaid(path, body):
    try:
        return plaid_client.post(path, body)
    except (RuntimeError, requests.RequestException) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/plaid/link-token")
def create_link_token(user: User):
    """Start a Plaid Link session for this user; the page hands the token to Plaid's Link widget."""
    data = call_plaid("/link/token/create", {
        "client_name": "Spending Tracker",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": user},
        "products": ["transactions"],
        # Fixed when the bank is first linked: ask for the most history Plaid allows.
        "transactions": {"days_requested": 730},
    })
    return {"link_token": data["link_token"]}


class PublicTokenExchange(BaseModel):
    public_token: str = Field(min_length=1, max_length=200)
    institution_name: Optional[str] = Field(default=None, max_length=100)


@app.post("/api/plaid/exchange")
def exchange_public_token(user: User, body: PublicTokenExchange):
    """Trade Link's one-time public token for a permanent access token, save the
    bank to this user, and pull its first transactions."""
    data = call_plaid("/item/public_token/exchange", {"public_token": body.public_token})
    with db() as conn:
        database.add_item(
            conn, user, data["item_id"], data["access_token"],
            body.institution_name, database.active_environment(),
        )
    # Plaid can take a while to prepare a new bank's history. If this first sync
    # fails or another one is running, the scheduler retries and the status
    # reports it, so the bank is still saved either way.
    try:
        scheduler.run_now(user)
    except Exception:
        pass
    return {"item_id": data["item_id"], "status": scheduler.status(user)}


@app.get("/api/sync")
def sync_status(user: User):
    return scheduler.status(user)


class IntervalUpdate(BaseModel):
    interval: str


@app.put("/api/sync/interval")
def set_sync_interval(user: User, update: IntervalUpdate):
    if update.interval not in INTERVALS:
        raise HTTPException(status_code=422, detail=f"interval must be one of {list(INTERVALS)}")
    scheduler.set_interval(user, update.interval)
    return scheduler.status(user)


@app.post("/api/sync")
def sync_now(user: User):
    """Run the user's sync right now and wait for it. Failures are reported in the status."""
    try:
        scheduler.run_now(user)
    except SyncBusy:
        raise HTTPException(status_code=409, detail="A sync is already running")
    except Exception:
        pass  # already recorded in sync_log; the status below carries the error
    return scheduler.status(user)


@app.delete("/api/accounts/{account_id}")
def remove_account(user: User, account_id: str):
    """Permanently delete one of the user's cards' purchases and stop tracking it."""
    with db() as conn:
        deleted = database.remove_account(conn, user, account_id)
        if deleted is None:
            raise HTTPException(status_code=404, detail="Unknown account")
        return {"deleted": deleted, "accounts": database.list_accounts(conn, user)}


class AccountUpdate(BaseModel):
    nickname: Optional[str] = Field(default=None, max_length=60)


@app.put("/api/accounts/{account_id}")
def rename_account(user: User, account_id: str, update: AccountUpdate):
    """Set the display name; an empty or missing nickname resets to the default."""
    nickname = (update.nickname or "").strip() or None
    with db() as conn:
        if not database.set_account_nickname(conn, user, account_id, nickname):
            raise HTTPException(status_code=404, detail="Unknown account")
        return {"accounts": database.list_accounts(conn, user)}
