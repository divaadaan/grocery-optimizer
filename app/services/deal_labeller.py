"""Label each deal once with ``{item, role, package size}`` for the tray builder.

Approach C (``experiments/chef/DESIGN_APPROACH_C.md`` §3). The tray builder
groups this week's gated deals by a generic *item* ("chicken thighs") and
culinary *role* (protein, vegetable, ...), and compares prices per 100 g / ml
when the package size is known. An LLM produces those labels offline, once per
deal, in batches of 40; they are stored in ``deal_labels`` (migration 024).

WHAT A LABEL MAY DECIDE
-----------------------
Only what is *surfaced*: grouping, tray membership and price comparison. A
label never decides dietary access -- that is ``app/services/deal_gate.py``
over curated product attributes, and the trays are built only from the deals
the gate already passed. Labels are stored data; nothing downstream
string-matches a deal name (CLAUDE.md, Product framing).

FAILURE BEHAVIOUR
-----------------
* Malformed JSON or missing ids -> one retry of the batch; whatever is still
  missing stays unlabelled and is picked up by the next run. Unlabelled deals
  never reach a tray (fail closed for surfacing).
* Infrastructure failure (model unreachable, timeout, HTTP 401/402/404/429/5xx,
  see ``app/agents/llm_errors.py``) -> the run stops at that batch and reports
  it; there is no point burning the remaining batches against a dead model.
* Labelling never runs at request time, and a labelling failure never fails
  ingestion (:func:`start_background_labelling`).
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.agents import llm_chat
from app.agents.llm_errors import classify, INFRASTRUCTURE
from app.config import settings
from app.db.database import db

logger = logging.getLogger(__name__)

ROLES = ("protein", "vegetable", "fruit", "starch", "dairy", "pantry_staple",
         "condiment", "prepared_meal", "snack", "beverage", "non_food")
UNKNOWN_ROLE = "unknown"
SIZE_UNITS = ("g", "ml", "each")
#: Bump when PROMPT changes: rows at an older version are relabelled.
PROMPT_VERSION = "v3"
BATCH = 40
MAX_TOKENS = 8000
ITEM_MAX_LEN = 80

#: Unit conversions for sizes a model reports in another unit. These convert
#: units, they never interpret a deal name.
_TO_BASE = {
    "g": ("g", 1.0), "gram": ("g", 1.0), "grams": ("g", 1.0),
    "kg": ("g", 1000.0), "lb": ("g", 453.592), "lbs": ("g", 453.592),
    "oz": ("g", 28.3495),
    "ml": ("ml", 1.0), "l": ("ml", 1000.0), "litre": ("ml", 1000.0), "liter": ("ml", 1000.0),
    "each": ("each", 1.0), "ea": ("each", 1.0), "count": ("each", 1.0),
}

PROMPT = """Label each grocery deal below for meal planning.
For each row give:
- "item": the generic grocery item a cook would shop for, lowercase, singular-ish, no brand or size
  (e.g. "chicken thighs", "whole chicken", "ground beef", "russet potatoes", "broccoli", "dry pasta",
  "pasta sauce", "olive oil", "cheddar cheese"). Use the SAME item string for the same thing across
  stores so they can be compared. If a row offers several different items ("X or Y"), name the first.
  A plant-based substitute is its own item ("plant-based egg", "plant-based burger"), never the
  animal food it replaces.
- "role": one of {roles}.
  Decide the role from what the item IS. The category column is only the store's shelf and is
  often wrong for the role (eggs shelved in "Dairy & Eggs" are still protein).
  protein = raw/fresh meat, poultry, fish, seafood, eggs (whole eggs, egg whites, plant-based egg),
  and plant proteins: tofu, tempeh, seitan, textured vegetable protein, plant-based meat, and ALL
  legumes (beans, chickpeas, lentils, split peas) whether canned, dried or frozen. A can or bag
  of beans, chickpeas or lentils is ALWAYS protein, never pantry_staple. Eggs are NEVER dairy.
  dairy = milk, cheese, yogurt, butter, cream.
  pantry_staple = shelf-stable COOKING basics only: pasta, rice, flour, sugar, oil, canned tomatoes,
  stock, spices. Legumes, tea, coffee, coffee pods, candy, chocolate, drinks and snacks are
  NEVER pantry_staple.
  fruit = fresh or frozen fruit, including citrus and melons. Fruit is NEVER sugar or
  pantry_staple, even when a variety name contains a word like "sugar" or "honey" (a "Honeycrisp"
  apple or a "Sugar Baby" watermelon is fruit; the item is the fruit, "apple", "watermelon").
  Label each row on its own: neighbouring rows are often different things.
  beverage = anything you drink, including tea, coffee and juice. snack = chips, cookies, candy,
  chocolate, bars; a chocolate or candy shaped or flavoured like a food is still snack (peanut
  butter cups are snack, not peanut butter). prepared_meal = ready-to-eat or heat-and-serve dishes.
  non_food = anything that is not food.
- "size_qty" and "size_unit": the package size if the row states it, converted to one of
  "g", "ml" or "each" (1.36 kg -> 1360 g; 12 x 355 mL -> 4260 ml; dozen eggs -> 12 each).
  Use null for both when the row does not state a size, or when the price is per lb / per kg.
Return ONLY a JSON object, no comments, exactly like this example:
{{"labels": [{{"id": 123, "item": "chicken thighs", "role": "protein", "size_qty": null, "size_unit": null}}, {{"id": 124, "item": "dry pasta", "role": "pantry_staple", "size_qty": 900, "size_unit": "g"}}]}}
One entry per row below.

Rows (id | name | price | category | store):
{rows}"""


@dataclass(frozen=True)
class Label:
    item: str
    role: str
    size_qty: Optional[float] = None
    size_unit: Optional[str] = None


@dataclass
class LabelReport:
    pending: int = 0
    labelled: int = 0
    unlabelled: int = 0
    batches: int = 0
    retries: int = 0
    stopped_on_infrastructure: Optional[str] = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_roles: int = 0
    by_role: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def deal_row(d: Dict[str, Any], name_len: int = 90) -> str:
    """One compact line per deal: ``id | name | $price/unit | category | store``."""
    name = " ".join(str(d.get("product_name") or "").split())
    if len(name) > name_len:
        name = name[: name_len - 1] + "…"
    price = f"${float(d['sale_price']):.2f}" if d.get("sale_price") is not None else "$?"
    if d.get("unit"):
        price += f"/{d['unit']}"
    store = d.get("chain") or d.get("store_name") or "-"
    return f"{d['deal_id']} | {name} | {price} | {d.get('category') or '-'} | {store}"


def build_prompt(deals: List[Dict[str, Any]]) -> str:
    return PROMPT.format(roles=", ".join(ROLES), rows="\n".join(deal_row(d) for d in deals))


def _norm_size(qty: Any, unit: Any) -> tuple:
    if qty is None or unit is None:
        return None, None
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return None, None
    conv = _TO_BASE.get(str(unit).strip().lower())
    if conv is None or not math.isfinite(q) or q <= 0:
        return None, None
    base, factor = conv
    value = round(q * factor, 2)
    if value <= 0 or value >= 1e8:
        return None, None
    return value, base


def parse_labels(text: str, ids: Iterable[int]) -> Dict[int, Label]:
    """Validated labels from one model reply, for the ids in this batch only.

    Entries for ids outside the batch, or without a usable item, are dropped.
    A role outside :data:`ROLES` is kept as ``unknown`` (never reaches a tray).
    """
    wanted = set(ids)
    got = llm_chat.parse_json_object(text or "") or {}
    out: Dict[int, Label] = {}
    entries = got.get("labels")
    if not isinstance(entries, list):
        return out
    for x in entries:
        if not isinstance(x, dict):
            continue
        try:
            deal_id = int(x["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if deal_id not in wanted:
            continue
        item = " ".join(str(x.get("item") or "").lower().split())[:ITEM_MAX_LEN]
        if not item:
            continue
        role = x.get("role") if x.get("role") in ROLES else UNKNOWN_ROLE
        qty, unit = _norm_size(x.get("size_qty"), x.get("size_unit"))
        out[deal_id] = Label(item=item, role=role, size_qty=qty, size_unit=unit)
    return out


def label_batch(deals: List[Dict[str, Any]], *, spec: str,
                chat_fn: Callable[..., llm_chat.Reply] = llm_chat.chat,
                report: Optional[LabelReport] = None) -> Dict[int, Label]:
    """Label one batch: one call, one retry for whatever is missing.

    Infrastructure errors propagate (the caller stops the run).
    """
    report = report if report is not None else LabelReport()
    labels: Dict[int, Label] = {}
    todo = list(deals)
    for attempt in range(2):
        if not todo:
            break
        if attempt:
            report.retries += 1
        reply = chat_fn(spec, [{"role": "user", "content": build_prompt(todo)}],
                        temperature=0.0, max_tokens=MAX_TOKENS, json_mode=True)
        report.input_tokens += reply.input_tokens or 0
        report.output_tokens += reply.output_tokens or 0
        labels.update(parse_labels(reply.text, (d["deal_id"] for d in todo)))
        todo = [d for d in todo if d["deal_id"] not in labels]
    return labels


# ---------------------------------------------------------------- database

_PENDING_SQL = """
SELECT d.deal_id, d.product_name, d.sale_price, d.unit, d.category,
       s.name AS store_name, s.chain
FROM deals d
JOIN stores s ON s.store_id = d.store_id
LEFT JOIN deal_labels l ON l.deal_id = d.deal_id
WHERE d.valid_until >= CURRENT_DATE
  AND (l.deal_id IS NULL OR l.prompt_version <> %(version)s)
"""

_UPSERT_SQL = """
INSERT INTO deal_labels (deal_id, item, role, size_qty, size_unit, model, prompt_version)
VALUES (%(deal_id)s, %(item)s, %(role)s, %(size_qty)s, %(size_unit)s, %(model)s, %(version)s)
ON CONFLICT (deal_id) DO UPDATE SET
    item = EXCLUDED.item, role = EXCLUDED.role,
    size_qty = EXCLUDED.size_qty, size_unit = EXCLUDED.size_unit,
    model = EXCLUDED.model, prompt_version = EXCLUDED.prompt_version,
    labelled_at = CURRENT_TIMESTAMP
"""


def fetch_pending(postal_code: Optional[str] = None, database=db) -> List[Dict[str, Any]]:
    """Current deals with no label at :data:`PROMPT_VERSION`, oldest id first."""
    query = _PENDING_SQL
    params: Dict[str, Any] = {"version": PROMPT_VERSION}
    if postal_code:
        query += "  AND s.postal_code = %(postal_code)s\n"
        params["postal_code"] = postal_code
    query += "ORDER BY d.deal_id"
    with database.get_cursor() as cursor:
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]


def save_labels(labels: Dict[int, Label], *, model: str, database=db) -> int:
    if not labels:
        return 0
    rows = [{"deal_id": deal_id, "item": lab.item, "role": lab.role,
             "size_qty": lab.size_qty, "size_unit": lab.size_unit,
             "model": model, "version": PROMPT_VERSION}
            for deal_id, lab in sorted(labels.items())]
    with database.get_cursor() as cursor:
        cursor.executemany(_UPSERT_SQL, rows)
    return len(rows)


def fetch_labels(deal_ids: Iterable[int], database=db) -> Dict[int, Label]:
    """Stored labels (any prompt version) for these deals."""
    ids = sorted({int(i) for i in deal_ids})
    if not ids:
        return {}
    with database.get_cursor() as cursor:
        cursor.execute(
            "SELECT deal_id, item, role, size_qty, size_unit FROM deal_labels "
            "WHERE deal_id = ANY(%(ids)s)", {"ids": ids})
        rows = cursor.fetchall()
    return {int(r["deal_id"]): Label(
        item=r["item"], role=r["role"],
        size_qty=float(r["size_qty"]) if r["size_qty"] is not None else None,
        size_unit=r["size_unit"]) for r in rows}


def label_pending(postal_code: Optional[str] = None, *, spec: Optional[str] = None,
                  chat_fn: Callable[..., llm_chat.Reply] = llm_chat.chat,
                  database=db, limit: Optional[int] = None,
                  batch_size: int = BATCH) -> LabelReport:
    """Label every current, unlabelled deal (optionally one postal code).

    Each batch is saved as soon as it is labelled, so an interrupted run keeps
    its progress. Stops at the first infrastructure failure.
    """
    spec = spec or settings.llm_labeller_model
    llm_chat.parse_spec(spec)  # fail on a bad spec before touching anything
    report = LabelReport(model=spec)
    pending = fetch_pending(postal_code, database=database)
    if limit is not None:
        pending = pending[:limit]
    report.pending = len(pending)
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        report.batches += 1
        try:
            labels = label_batch(batch, spec=spec, chat_fn=chat_fn, report=report)
        except Exception as exc:  # noqa: BLE001 - classified below
            if classify(exc) == INFRASTRUCTURE:
                report.stopped_on_infrastructure = f"{type(exc).__name__}: {exc}"[:500]
                logger.error("Labelling stopped at batch %d: %s", report.batches,
                             report.stopped_on_infrastructure)
                break
            logger.warning("Labelling batch %d failed (model output): %s", report.batches, exc)
            continue
        save_labels(labels, model=spec, database=database)
        report.labelled += len(labels)
        for lab in labels.values():
            report.by_role[lab.role] = report.by_role.get(lab.role, 0) + 1
            if lab.role == UNKNOWN_ROLE:
                report.unknown_roles += 1
    report.unlabelled = report.pending - report.labelled
    return report


# ------------------------------------------------------- after ingestion

_background_lock = threading.Lock()


def start_background_labelling(postal_code: Optional[str] = None, *,
                               runner: Callable[..., LabelReport] = label_pending
                               ) -> Optional[threading.Thread]:
    """Label new deals on a daemon thread after an ingestion run.

    No-op (returns ``None``) when ``DEAL_LABELLING_ENABLED`` is off or a
    labelling run is already going. Never raises: a labelling failure must not
    fail the ingestion that triggered it.
    """
    if not settings.deal_labelling_enabled:
        return None
    if not _background_lock.acquire(blocking=False):
        logger.info("Labelling already running; skipping the post-ingestion run")
        return None

    def _run():
        try:
            report = runner(postal_code)
            logger.info("Post-ingestion labelling for %s: %s", postal_code, report.as_dict())
        except Exception:  # noqa: BLE001 - background; logged, never raised
            logger.exception("Post-ingestion labelling for %s failed", postal_code)
        finally:
            _background_lock.release()

    thread = threading.Thread(target=_run, name=f"deal-labelling-{postal_code}", daemon=True)
    thread.start()
    return thread
