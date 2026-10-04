"""Local YouTube quota ledger.

YouTube charges *units*, not requests: search.list costs 100, almost everything
else costs 1. The daily allowance (10,000 by default) resets at midnight Pacific
time. Without a ledger the pipeline only learns it is out of budget when the API
starts returning 403s, possibly halfway through a stage. Charging before each
call turns "crash mid-run" into "stop cleanly, resume tomorrow".
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .common import QuotaBudgetExceeded, config
from .db import connect, init_db

UNIT_COST = {"search": 100}
DEFAULT_DAILY_BUDGET = 8000  # leaves headroom under YouTube's 10,000 default


def quota_day() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def daily_budget() -> int:
    return int(config().get("youtube_daily_quota_budget", DEFAULT_DAILY_BUDGET))


def units_today() -> int:
    init_db()
    with connect() as db:
        row = db.execute("SELECT COALESCE(SUM(units),0) FROM api_usage WHERE day=?", (quota_day(),)).fetchone()
    return int(row[0])


def charge(endpoint: str) -> int:
    """Reserve the cost of one call. Raises QuotaBudgetExceeded instead of overspending."""
    cost = UNIT_COST.get(endpoint, 1)
    init_db()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        used = db.execute("SELECT COALESCE(SUM(units),0) FROM api_usage WHERE day=?", (quota_day(),)).fetchone()[0]
        if used + cost > daily_budget():
            raise QuotaBudgetExceeded(
                f"Daily YouTube quota budget reached ({used}/{daily_budget()} units); resume after the reset")
        db.execute(
            """INSERT INTO api_usage (day,api,units,calls) VALUES (?,?,?,1)
               ON CONFLICT(day,api) DO UPDATE SET units=units+?, calls=calls+1""",
            (quota_day(), endpoint, cost, cost),
        )
    return used + cost


def mark_exhausted() -> None:
    """The API itself said we are out: trust it over our estimate."""
    init_db()
    remaining = max(0, daily_budget() - units_today())
    if remaining:
        with connect() as db:
            db.execute(
                """INSERT INTO api_usage (day,api,units,calls) VALUES (?,?,?,0)
                   ON CONFLICT(day,api) DO UPDATE SET units=units+?""",
                (quota_day(), "reconciliation", remaining, remaining),
            )


def usage_report() -> dict:
    init_db()
    with connect() as db:
        rows = db.execute("SELECT api,units,calls FROM api_usage WHERE day=? ORDER BY units DESC", (quota_day(),)).fetchall()
    return {"day_pacific": quota_day(), "budget": daily_budget(), "used": sum(r["units"] for r in rows),
            "by_endpoint": {r["api"]: {"units": r["units"], "calls": r["calls"]} for r in rows}}
