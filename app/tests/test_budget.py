"""Tests for budget periods, accumulated spend, and what `budget_remaining` means.

No database and no LLM. The period arithmetic and the "does this row count as
spend" rule are pure functions over plain dicts (``app/services/budget.py``),
so the interesting cases — a list completed either side of a period boundary,
a list that was never completed — are reachable here exactly.

What is deliberately NOT covered: the SQL in
``DatabaseService.get_completed_shopping_lists_since``. There is no Postgres in
the test environment (the same limitation ``test_migrations.py`` documents).
That query is kept dumb on purpose — it narrows to "this user's completed lists
since <instant>" and decides nothing — so the rule it cannot drift away from
lives here instead.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.agents.graph import budget_remaining_after
from app.routes import users as users_route
from app.services import budget as budget_service
from app.services.budget import (
    MONTHLY,
    WEEKLY,
    available_for_plan,
    budget_status,
    counts_as_spend,
    normalize_period,
    period_bounds,
    spend_in_period,
)

# 2026-10-14 is a Wednesday; 2026-10-12 is its Monday.
WEDNESDAY = datetime(2026, 10, 14, 15, 30)


def _list(cost, completed_at, is_completed=True, list_id=1):
    return {
        "list_id": list_id,
        "total_cost": cost,
        "completed_at": completed_at,
        "is_completed": is_completed,
    }


# ---------------------------------------------------------------------------
# Period arithmetic
# ---------------------------------------------------------------------------

def test_monthly_period_is_the_calendar_month():
    start, end = period_bounds(MONTHLY, WEDNESDAY)
    assert start == datetime(2026, 10, 1, 0, 0)
    assert end == datetime(2026, 11, 1, 0, 0)


def test_monthly_period_rolls_the_year_over_in_december():
    start, end = period_bounds(MONTHLY, datetime(2026, 12, 20, 9, 0))
    assert start == datetime(2026, 12, 1, 0, 0)
    assert end == datetime(2027, 1, 1, 0, 0)


def test_weekly_period_is_anchored_on_monday():
    start, end = period_bounds(WEEKLY, WEDNESDAY)
    assert start == datetime(2026, 10, 12, 0, 0)  # Monday
    assert end == datetime(2026, 10, 19, 0, 0)    # the next Monday


def test_a_monday_is_the_first_day_of_its_own_week_not_the_last():
    start, _ = period_bounds(WEEKLY, datetime(2026, 10, 12, 0, 0))
    assert start == datetime(2026, 10, 12, 0, 0)


def test_periods_are_half_open_so_no_instant_belongs_to_two():
    _, end = period_bounds(WEEKLY, WEDNESDAY)
    next_start, _ = period_bounds(WEEKLY, end)
    assert next_start == end


def test_missing_period_defaults_to_monthly_and_a_typo_raises():
    assert normalize_period(None) == MONTHLY
    assert normalize_period("") == MONTHLY
    assert normalize_period("MONTHLY") == MONTHLY
    with pytest.raises(ValueError):
        normalize_period("fortnightly")


def test_an_aware_now_is_accepted_rather_than_crashing_the_comparison():
    aware = datetime(2026, 10, 14, 15, 30, tzinfo=timezone.utc)
    start, end = period_bounds(MONTHLY, aware)
    assert start.tzinfo is None and end.tzinfo is None


# ---------------------------------------------------------------------------
# What counts as spend
# ---------------------------------------------------------------------------

def test_a_list_completed_inside_the_period_is_spend():
    start, end = period_bounds(MONTHLY, WEDNESDAY)
    total, counted = spend_in_period([_list(40.25, datetime(2026, 10, 3, 11, 0))], start, end)
    assert total == 40.25
    assert len(counted) == 1


def test_a_list_completed_just_before_the_boundary_belongs_to_the_old_period():
    """One second either side of a boundary must land in different periods."""
    start, end = period_bounds(MONTHLY, WEDNESDAY)
    just_before = start - timedelta(seconds=1)      # 2026-09-30 23:59:59
    just_after = start                               # 2026-10-01 00:00:00

    rows = [_list(30.0, just_before, list_id=1), _list(12.0, just_after, list_id=2)]
    total, counted = spend_in_period(rows, start, end)

    assert total == 12.0, "September's shopping must not count against October"
    assert [row["list_id"] for row in counted] == [2]


def test_a_list_completed_at_the_end_boundary_belongs_to_the_next_period():
    start, end = period_bounds(MONTHLY, WEDNESDAY)
    rows = [_list(9.99, end - timedelta(seconds=1), list_id=1),
            _list(50.0, end, list_id=2)]
    total, counted = spend_in_period(rows, start, end)
    assert total == 9.99
    assert [row["list_id"] for row in counted] == [1]


def test_an_incomplete_list_is_a_plan_not_spend():
    start, end = period_bounds(MONTHLY, WEDNESDAY)
    rows = [
        {"list_id": 1, "total_cost": 85.0, "completed_at": None, "is_completed": False},
        # is_completed true with no timestamp (pre-012 shape) is still not
        # datable spend, so it cannot be attributed to a period.
        {"list_id": 2, "total_cost": 70.0, "completed_at": None, "is_completed": True},
        # The inverse mismatch: a timestamp with the flag cleared. The schema's
        # CHECK makes this unreachable; the rule rejects it anyway.
        _list(60.0, datetime(2026, 10, 2), is_completed=False, list_id=3),
    ]
    total, counted = spend_in_period(rows, start, end)
    assert total == 0.0
    assert counted == []


def test_several_completed_lists_accumulate():
    start, end = period_bounds(WEEKLY, WEDNESDAY)
    rows = [_list(21.50, datetime(2026, 10, 12, 18, 0), list_id=1),
            _list(13.25, datetime(2026, 10, 14, 9, 0), list_id=2)]
    total, _ = spend_in_period(rows, start, end)
    assert total == 34.75


def test_counts_as_spend_is_the_single_rule_both_ways():
    start, end = period_bounds(MONTHLY, WEDNESDAY)
    assert counts_as_spend(_list(1.0, datetime(2026, 10, 5)), start, end)
    assert not counts_as_spend(_list(1.0, datetime(2026, 9, 5)), start, end)


# ---------------------------------------------------------------------------
# budget_status
# ---------------------------------------------------------------------------

def _user(budget=200.0, period=MONTHLY, user_id=7):
    return {"user_id": user_id, "budget": budget, "budget_period": period}


def _loader(rows, seen=None):
    def load(user_id, since):
        if seen is not None:
            seen.append((user_id, since))
        return list(rows)
    return load


def test_status_for_a_user_with_no_completed_lists_is_the_whole_budget():
    status = budget_status(_user(), now=WEDNESDAY, load_completed_lists=_loader([]))
    assert status["spent"] == 0.0
    assert status["remaining"] == 200.0
    assert status["available_for_plan"] == 200.0
    assert status["is_over_budget"] is False
    assert status["completed_lists"] == 0


def test_status_subtracts_completed_spend_and_reports_the_window():
    rows = [_list(45.0, datetime(2026, 10, 2), list_id=1),
            _list(55.5, datetime(2026, 10, 9), list_id=2),
            _list(99.0, datetime(2026, 9, 28), list_id=3)]  # last month
    status = budget_status(_user(), now=WEDNESDAY, load_completed_lists=_loader(rows))

    assert status["spent"] == 100.5
    assert status["remaining"] == 99.5
    assert status["completed_lists"] == 2
    assert status["period_start"] == datetime(2026, 10, 1)
    assert status["period_end"] == datetime(2026, 11, 1)
    assert status["budget_period"] == MONTHLY


def test_over_budget_is_reported_as_a_negative_remaining_not_clamped():
    rows = [_list(250.0, datetime(2026, 10, 4))]
    status = budget_status(_user(), now=WEDNESDAY, load_completed_lists=_loader(rows))

    assert status["spent"] == 250.0
    assert status["remaining"] == -50.0
    assert status["is_over_budget"] is True
    # ...but what a new plan may spend is floored at zero, so no prompt and no
    # UI ever has to render "available: $-50.00".
    assert status["available_for_plan"] == 0.0


def test_available_for_plan_floors_at_zero_only_on_the_downside():
    assert available_for_plan(12.5) == 12.5
    assert available_for_plan(0.0) == 0.0
    assert available_for_plan(-0.01) == 0.0


def test_a_weekly_user_and_a_monthly_user_see_different_windows():
    rows = [_list(30.0, datetime(2026, 10, 5), list_id=1),    # Mon of last week
            _list(20.0, datetime(2026, 10, 13), list_id=2)]   # this week
    monthly = budget_status(_user(period=MONTHLY), now=WEDNESDAY,
                            load_completed_lists=_loader(rows))
    weekly = budget_status(_user(period=WEEKLY), now=WEDNESDAY,
                           load_completed_lists=_loader(rows))

    assert monthly["spent"] == 50.0
    assert weekly["spent"] == 20.0


def test_the_loader_is_asked_only_for_rows_since_the_period_start():
    """The DB query narrows on the same bound the rule then applies."""
    seen = []
    budget_status(_user(), now=WEDNESDAY, load_completed_lists=_loader([], seen))
    assert seen == [(7, datetime(2026, 10, 1, 0, 0))]


def test_the_period_is_anchored_to_the_database_clock(monkeypatch):
    """Not this process's clock: the DB is what stamps completed_at.

    Here the DB "is" in November while local time is 14 October; the window
    must follow the database.
    """
    from app.services.database import DatabaseService

    monkeypatch.setattr(DatabaseService, "db_now",
                        lambda self: datetime(2026, 11, 20, 4, 0))

    status = budget_status(_user(), load_completed_lists=_loader([]))
    assert status["period_start"] == datetime(2026, 11, 1)
    assert status["period_end"] == datetime(2026, 12, 1)


def test_a_purchase_late_on_the_last_day_is_not_double_counted_across_clocks(monkeypatch):
    """The case that breaks if the window and the timestamps use two clocks.

    The app process here is in a zone behind the database (Neon is UTC): at
    2026-11-01 00:30 database time it is still 2026-10-31 20:30 locally. A
    list bought "late last night" carries a database timestamp of
    2026-10-31 23:50 -- October by the database's clock, which is the clock
    that wrote it.

    Anchoring the window to the database clock puts that purchase in October,
    where it belongs, and leaves November at zero. Anchoring it to local time
    would compute an October window at this instant and charge the purchase to
    October *again* on the next call, once local time has rolled over: the same
    money reported in two periods, or in none, depending on which side of the
    skew the call lands. One clock for both is the only arithmetic that
    reconciles.
    """
    from app.services.database import DatabaseService

    late_october = _list(37.00, datetime(2026, 10, 31, 23, 50))

    # Database clock: just into November. Local clock: still October.
    monkeypatch.setattr(DatabaseService, "db_now",
                        lambda self: datetime(2026, 11, 1, 0, 30))
    november = budget_status(_user(), load_completed_lists=_loader([late_october]))

    assert november["period_start"] == datetime(2026, 11, 1)
    assert november["spent"] == 0.0, "October's shopping is not November's spend"
    assert november["remaining"] == 200.0

    # Half an hour earlier by the same clock, the purchase is inside the window.
    monkeypatch.setattr(DatabaseService, "db_now",
                        lambda self: datetime(2026, 10, 31, 23, 59))
    october = budget_status(_user(), load_completed_lists=_loader([late_october]))

    assert october["period_start"] == datetime(2026, 10, 1)
    assert october["spent"] == 37.0
    # Counted exactly once, in exactly one of the two periods.
    assert october["spent"] + november["spent"] == 37.0


def test_an_unreadable_database_clock_degrades_to_local_time(monkeypatch):
    from app.services.database import DatabaseService

    def boom(self):
        raise RuntimeError("pool is closed")

    monkeypatch.setattr(DatabaseService, "db_now", boom)
    before = datetime.now()
    fallback = budget_service._default_now()
    assert before <= fallback <= datetime.now() + timedelta(seconds=5)


def test_a_user_with_no_period_set_is_treated_as_monthly():
    status = budget_status({"user_id": 3, "budget": 100.0}, now=WEDNESDAY,
                           load_completed_lists=_loader([]))
    assert status["budget_period"] == MONTHLY


# ---------------------------------------------------------------------------
# What the generation graph does with it
# ---------------------------------------------------------------------------

def test_budget_remaining_subtracts_prior_spend_as_well_as_this_plan():
    state = {"budget": 200.0, "budget_spent_this_period": 120.0}
    assert budget_remaining_after(state, 50.0) == 30.0


def test_budget_remaining_goes_negative_when_a_plan_breaks_the_limit():
    state = {"budget": 100.0, "budget_spent_this_period": 90.0}
    assert budget_remaining_after(state, 25.0) == -15.0


def test_budget_remaining_falls_back_to_the_old_arithmetic_without_the_channel():
    """A state assembled by an older caller must not raise."""
    assert budget_remaining_after({"budget": 100.0}, 40.0) == 60.0
    assert budget_remaining_after({"budget": 100.0, "budget_spent_this_period": None}, 40.0) == 60.0


# ---------------------------------------------------------------------------
# GET /users/{user_id}/budget
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def stub_user(monkeypatch):
    """Serve one user, and one canned set of completed lists, without a DB."""
    rows = [_list(40.0, datetime(2026, 10, 3), list_id=1)]

    monkeypatch.setattr(users_route.UserService, "get_user_by_id",
                        staticmethod(lambda user_id: _user() if user_id == 7 else None))
    monkeypatch.setattr(
        users_route, "budget_status",
        lambda user: budget_service.budget_status(
            user, now=WEDNESDAY, load_completed_lists=_loader(rows)),
    )
    return rows


@pytest.mark.asyncio
async def test_budget_endpoint_answers_how_much_is_left(client, stub_user):
    async with client as http:
        response = await http.get("/api/v1/users/7/budget")

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == 7
    # Decimal fields: compare numerically, not by their JSON spelling.
    assert float(body["budget"]) == 200.0
    assert body["budget_period"] == "monthly"
    assert float(body["spent"]) == 40.0
    assert float(body["remaining"]) == 160.0
    assert body["is_over_budget"] is False
    assert body["completed_lists"] == 1
    assert body["period_start"].startswith("2026-10-01")
    assert body["period_end"].startswith("2026-11-01")


@pytest.mark.asyncio
async def test_budget_endpoint_404s_for_an_unknown_user(client, stub_user):
    async with client as http:
        response = await http.get("/api/v1/users/999/budget")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_budget_endpoint_does_not_shadow_the_user_endpoint(client, stub_user):
    """`/users/{id}/budget` must not be swallowed by `/users/{id}`."""
    async with client as http:
        response = await http.get("/api/v1/users/7/budget")
    assert "spent" in response.json()
