import calendar
from contextlib import asynccontextmanager, closing
from datetime import date
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import database
from scheduler import INTERVALS, Scheduler, SyncBusy

STATIC_DIR = Path(__file__).parent / "static"
MAX_TREND_MONTHS = 12

scheduler = Scheduler()


@asynccontextmanager
async def lifespan(app):
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Spending dashboard", lifespan=lifespan)

Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


def db():
    # Each request gets its own connection; sqlite3 connections can't be
    # shared across the threads FastAPI runs sync endpoints on.
    return closing(database.connect())


def previous_month(month):
    year, number = map(int, month.split("-"))
    return f"{year - 1}-12" if number == 1 else f"{year}-{number - 1:02d}"


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/months")
def months():
    with db() as conn:
        return {"months": database.list_months(conn)}


@app.get("/api/overview")
def overview(month: Month):
    with db() as conn:
        count, total = database.month_totals(conn, month)
        _, previous_total = database.month_totals(conn, previous_month(month))
        top_merchant = database.month_top_merchant(conn, month)
        categories = database.month_categories(conn, month)

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
    }


@app.get("/api/transactions")
def transactions(month: Month):
    with db() as conn:
        return {"transactions": database.month_transactions(conn, month)}


@app.get("/api/trend")
def trend():
    with db() as conn:
        by_month = database.category_by_month(conn)

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
def accounts():
    with db() as conn:
        return {"accounts": database.list_accounts(conn)}


@app.get("/api/sync")
def sync_status():
    return scheduler.status()


class IntervalUpdate(BaseModel):
    interval: str


@app.put("/api/sync/interval")
def set_sync_interval(update: IntervalUpdate):
    if update.interval not in INTERVALS:
        raise HTTPException(status_code=422, detail=f"interval must be one of {list(INTERVALS)}")
    scheduler.set_interval(update.interval)
    return scheduler.status()


@app.post("/api/sync")
def sync_now():
    """Run a sync right now and wait for it. Failures are reported in the status."""
    try:
        scheduler.run_now()
    except SyncBusy:
        raise HTTPException(status_code=409, detail="A sync is already running")
    except Exception:
        pass  # already recorded in sync_log; the status below carries the error
    return scheduler.status()


class AccountUpdate(BaseModel):
    nickname: Optional[str] = Field(default=None, max_length=60)


@app.put("/api/accounts/{account_id}")
def rename_account(account_id: str, update: AccountUpdate):
    """Set the display name; an empty or missing nickname resets to the default."""
    nickname = (update.nickname or "").strip() or None
    with db() as conn, conn:
        if not database.set_account_nickname(conn, account_id, nickname):
            raise HTTPException(status_code=404, detail="Unknown account")
        return {"accounts": database.list_accounts(conn)}
