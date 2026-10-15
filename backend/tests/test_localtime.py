"""Dates shown to people are the tenant's local dates (review round 3, N-024)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.conftest import by_type

SYDNEY = ZoneInfo("Australia/Sydney")


# Review round 3, N-024: the dashboard's days were UTC dates, so the traffic at 06:14 Sydney time on
# 10-20 was counted under 10-19 while every other time on the page is local. Days are now the tenant's
# (Australia/Sydney unless changed), and "the last 7 days" means 7 local calendar days, today included.

def _local_today_at(hour: int, minute: int = 0, days_ago: int = 0) -> datetime:
    local = datetime.now(SYDNEY).replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(days=days_ago)
    return local.astimezone(timezone.utc)


def test_the_dashboard_counts_days_in_the_tenants_time_zone(client, ask, login):
    from app.db import new_session
    from app.models import Run

    customer, admin = login("customer@harbour.demo"), login("admin@harbour.demo")
    early, oldest, too_old = (by_type(ask(customer, q)[1], "done")[0]["run_id"]
                              for q in ("how long do refunds take?", "how quickly do you dispatch in-stock items?",
                                        "what are the conditions for a change-of-mind return?"))
    with new_session() as db:
        db.get(Run, early).created_at = _local_today_at(0, 1)          # the previous UTC date
        db.get(Run, oldest).created_at = _local_today_at(0, 1, days_ago=6)
        db.get(Run, too_old).created_at = _local_today_at(23, 59, days_ago=7)
        db.commit()
    overview = client.get("/api/metrics/overview?days=7", headers=admin).json()
    today = datetime.now(SYDNEY).date()
    days = [d["day"] for d in overview["daily"]]
    assert days == [(today - timedelta(days=n)).strftime("%m-%d") for n in range(6, -1, -1)], "7 local days, in order"
    assert overview["daily"][-1]["runs"] == 1 and overview["daily"][0]["runs"] == 1
    assert overview["runs"] == 2, "the 7 days start at local midnight six days ago"
    assert overview["timezone"] == "Australia/Sydney"

    assert client.patch("/api/tenant", json={"timezone": "Mars/Olympus"}, headers=admin).status_code == 400
    assert client.patch("/api/tenant", json={"timezone": "UTC"}, headers=admin).status_code == 200
    utc = client.get("/api/metrics/overview?days=7", headers=admin).json()
    assert utc["timezone"] == "UTC" and len(utc["daily"]) == 7


def test_order_dates_shown_to_customers_are_local_dates(client, ask, login):
    from app.db import new_session
    from app.models import Order

    delivered = _local_today_at(6, 30, days_ago=2)  # 19:30 UTC the day before
    with new_session() as db:
        db.get(Order, "SO20260918001").delivered_at = delivered
        db.commit()
    _, events = ask(login("customer@harbour.demo"), "can you check order SO20260918001 for me?")
    order = next(e["output"] for e in by_type(events, "tool_result") if e["name"] == "get_order")
    assert order["delivered_at"] == delivered.astimezone(SYDNEY).strftime("%Y-%m-%d")
    assert order["delivered_at"] != delivered.strftime("%Y-%m-%d"), "the UTC date is the day before"
