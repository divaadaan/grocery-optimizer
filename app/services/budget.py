"""Budget periods and the spend accumulated inside one.

``users.budget`` is the ceiling for **one budget period**, not for one meal
plan. Which period is now data, not prose: ``users.budget_period`` is
``'weekly'`` or ``'monthly'`` (migration 012). Before that column existed the
docstring on ``POST /users/register`` said "monthly" while
``app/agents/graph.py`` spent the whole of it on every single generation, and
the frontend's own label said "Weekly budget (CAD)" -- three answers to one
question.

**The period is calendar-anchored, not rolling.** Monthly starts on the 1st at
00:00; weekly starts Monday at 00:00 (ISO week). A rolling 30-day window was
rejected deliberately: it never resets, so one over-budget week follows the
user forever, and "remaining" changes on days they bought nothing simply
because an old purchase aged out. A calendar anchor is explainable in one
sentence, matches how people already talk about grocery money ("this month"),
and gives a boundary worth testing.

Bounds are **half-open**, ``start <= t < end``. A list completed at the stroke
of the boundary belongs to the new period, and no instant belongs to two.

The window is anchored to the **database's** clock, because the database is
what writes ``completed_at`` (Neon runs UTC; this app process may not). Not the
user's clock -- that would need a per-user timezone column, and is noted as a
limitation on ``_default_now`` rather than quietly approximated.

**Spend is derived, never stored.** A completed shopping list is money spent;
an uncompleted one is not. The sum lives nowhere -- it is computed from
``shopping_lists`` on each read, so there is no running total to drift out of
agreement with the rows it claims to summarise. The dating signal is
``shopping_lists.completed_at`` (also 012), not ``created_at``: a list built on
31 January and actually bought on 2 February is February's money.

The period arithmetic and the "does this row count" rule live here, in pure
functions over plain dicts. The database layer only narrows (give me this
user's completed lists since <instant>); this module decides. That keeps one
implementation of the rule that tests can reach without a database.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

WEEKLY = "weekly"
MONTHLY = "monthly"
PERIODS = (WEEKLY, MONTHLY)

DEFAULT_PERIOD = MONTHLY


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    """Drop to naive local wall-clock time.

    Every timestamp in this schema is ``TIMESTAMP`` (no zone) written by
    Postgres ``NOW()``, so the comparisons here are wall-clock against
    wall-clock. An aware datetime handed in by a caller is converted to local
    time first rather than compared across representations, which would raise.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def normalize_period(period: Optional[str]) -> str:
    """Accept ``None``/blank as the default period; reject anything unknown.

    Unknown values fail loudly instead of silently becoming "monthly": a typo
    in a period name must not quietly quadruple someone's spend limit.
    """
    if period is None or period == "":
        return DEFAULT_PERIOD
    value = str(period).strip().lower()
    if value not in PERIODS:
        raise ValueError(
            f"unknown budget period {period!r}; expected one of {PERIODS}"
        )
    return value


def period_bounds(period: Optional[str],
                  now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Half-open ``[start, end)`` of the calendar period containing ``now``."""
    period = normalize_period(period)
    moment = _naive(now) if now is not None else datetime.now()
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == WEEKLY:
        start = midnight - timedelta(days=midnight.weekday())  # Monday
        return start, start + timedelta(days=7)

    start = midnight.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def counts_as_spend(row: Dict, start: datetime, end: datetime) -> bool:
    """Is this shopping-list row money spent inside ``[start, end)``?

    Two ways a row does not count: it was never marked complete (no
    ``completed_at``, or ``is_completed`` false -- the schema's CHECK keeps
    those two in step, this reads both anyway so neither alone can let a
    planned-but-unbought list through), or it was completed outside the window.
    """
    completed_at = _naive(row.get("completed_at"))
    if completed_at is None:
        return False
    if row.get("is_completed") is False:
        return False
    return start <= completed_at < end


def spend_in_period(rows: Iterable[Dict],
                    start: datetime,
                    end: datetime) -> Tuple[float, List[Dict]]:
    """Total spent and the rows that make it up, for ``[start, end)``."""
    counted = [row for row in rows if counts_as_spend(row, start, end)]
    total = sum(float(row.get("total_cost") or 0.0) for row in counted)
    return round(total, 2), counted


def available_for_plan(remaining: float) -> float:
    """What a new meal plan is allowed to cost: ``remaining``, floored at 0.

    The prompts interpolate this. An already-overspent user has a negative
    ``remaining``, which is the honest number to *report*, but telling a
    SousChef its budget is ``$-12.40`` invites nonsense; zero means "you have
    nothing left", which it can act on.
    """
    return remaining if remaining > 0 else 0.0


def _default_loader(user_id: int, since: datetime) -> List[Dict]:
    # Imported here so this module stays importable (and unit-testable)
    # without a database/connection pool in the process.
    from app.services.database import DatabaseService

    return DatabaseService().get_completed_shopping_lists_since(user_id, since)


def _default_now() -> datetime:
    """The clock the period is anchored to: the database's, not this process's.

    ``completed_at`` is written by Postgres ``NOW()``. Neon runs in UTC while
    the app process may not, so anchoring the window with ``datetime.now()``
    would compare two clocks and could drop a late-evening purchase out of
    every period. One clock for both keeps the arithmetic reconcilable.

    Known limitation, deliberately not solved here: that clock is the server's,
    not the user's. For a user west of UTC a purchase late on the last day of
    the month lands in the next month's window. Fixing it properly needs a
    per-user timezone, which is a schema change of its own.
    """
    from app.services.database import DatabaseService

    try:
        return DatabaseService().db_now()
    except Exception as exc:  # noqa: BLE001 - degrade, but say so
        logger.warning(
            "Could not read the database clock (%s); anchoring the budget "
            "period to local time instead, which may be skewed against "
            "completed_at", exc
        )
        return datetime.now()


def budget_status(
    user: Dict,
    now: Optional[datetime] = None,
    load_completed_lists: Optional[Callable[[int, datetime], List[Dict]]] = None,
) -> Dict:
    """Answer "how much of my budget is left this period" for one user.

    ``user`` is a row from ``UserService``. ``load_completed_lists`` and the
    clock are the only database touches; both are injectable (pass ``now``) so
    this is testable without a database.
    """
    period = normalize_period(user.get("budget_period"))
    start, end = period_bounds(period, now if now is not None else _default_now())

    loader = load_completed_lists or _default_loader
    rows = loader(user["user_id"], start)

    spent, counted = spend_in_period(rows, start, end)
    spent = float(spent)
    budget = round(float(user.get("budget") or 0.0), 2)
    remaining = round(budget - spent, 2)

    return {
        "user_id": user["user_id"],
        "budget": budget,
        "budget_period": period,
        "period_start": start,
        "period_end": end,
        "spent": spent,
        "remaining": remaining,
        "available_for_plan": available_for_plan(remaining),
        "is_over_budget": remaining < 0,
        "completed_lists": len(counted),
    }
