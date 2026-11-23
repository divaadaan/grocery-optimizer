"""Code-built trays: this week's best gated deals, sorted for the dish planner.

Approach C (``experiments/chef/DESIGN_APPROACH_C.md`` §4), ported from
``experiments/chef/arms/c_trays.py``. Deterministic: same deals and labels in,
same trays out.

1. Start from the **gated** deal set (``fetch_current_deals`` with the user's
   restrictions -- ``deal_gate`` decides dietary access, nothing here does).
2. Join each deal to its stored label (``deal_labels``). Unlabelled deals never
   reach a tray.
3. Normalise each price to a comparable basis: $/lb for weight, $/L for
   volume, $/each for counts, else $/package.
4. Group by ``(role, item, basis)``. Keep each group's cheapest deal and how it
   compares with the median across chains (the loss-leader signal).
5. Fill three trays:

   ========  ====================================  ====
   ANCHORS   protein                                15
   SIDES     vegetable 12, starch 5, fruit 4,       25
             dairy 4 (unused slots: best leftovers)
   STOCK-UP  pantry_staple, condiment (>=2 chains)  15
   ========  ====================================  ====

   Biggest cross-chain gap first, then the more reliable basis, then cheapest.
   prepared_meal, snack, beverage, non_food and unknown never reach a tray.

Items are compared by exact equality of the stored ``item`` label -- data
produced by the labelling job. Nothing here reads or matches a deal name.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.services import products
from app.services.deal_labeller import Label, PROMPT_VERSION

ANCHOR_ROLES = frozenset({"protein"})
SIDE_ROLES = frozenset({"vegetable", "starch", "fruit", "dairy"})
STOCK_UP_ROLES = frozenset({"pantry_staple", "condiment"})
N_ANCHORS, N_SIDES, N_STOCK_UP = 15, 25, 15
#: SIDES slots per role (owner, 2026-11-23: dairy was crowding out the
#: vegetables -- milk, cream cheese and butter took ~10 of 25). Sums to
#: N_SIDES. A role with fewer good deals than its quota leaves its slots to
#: the best remaining sides of any role.
SIDE_QUOTAS = {"vegetable": 12, "starch": 5, "fruit": 4, "dairy": 4}

TRAYS = ("anchors", "sides", "stock_up")

G_PER_LB = 453.592
#: Basis reliability for ordering: weight and volume compare like with like;
#: a bare package price may just be a smaller package.
BASIS_RANK = {"lb": 0, "l": 1, "each": 2, "pkg": 3}

_PER_100G = re.compile(r"100\s?g")
_PER_KG = re.compile(r"[\d.]+\s?/?\s?kg!?")


def price_basis(deal: Dict[str, Any], label: Optional[Label]) -> Optional[Tuple[str, float]]:
    """``(basis, comparable price)`` or ``None`` when the deal has no price.

    Reads the flyer's *unit* field (a unit of measure, not a product name)
    and the label's package size.
    """
    p = deal.get("sale_price")
    if p is None:
        return None
    p = float(p)
    unit = (deal.get("unit") or "").strip().lower()
    if unit.startswith("lb"):
        return "lb", p
    if _PER_100G.fullmatch(unit):
        return "lb", p * G_PER_LB / 100
    if _PER_KG.fullmatch(unit):
        # Flyer convention: "$3.49 / 7.69kg" -- the sale price is per lb and
        # the unit text restates it per kg.
        return "lb", p
    if label is not None and label.size_qty:
        if label.size_unit == "g":
            return "lb", p / label.size_qty * G_PER_LB
        if label.size_unit == "ml":
            return "l", p / label.size_qty * 1000
        if label.size_unit == "each":
            return "each", p / label.size_qty
    return "pkg", p


@dataclass
class TrayItem:
    deal_id: int
    product_name: str
    store_name: Optional[str]
    chain: Optional[str]
    sale_price: float
    unit: Optional[str]
    item: str
    role: str
    basis: str
    compare_price: float
    median_price: float
    n_chains: int
    gap: float


@dataclass
class TrayProposal:
    anchors: List[TrayItem] = field(default_factory=list)
    sides: List[TrayItem] = field(default_factory=list)
    stock_up: List[TrayItem] = field(default_factory=list)
    pantry: List[Dict[str, Any]] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    label_version: str = PROMPT_VERSION

    def tray(self, name: str) -> List[TrayItem]:
        return getattr(self, name)

    def offered_ids(self) -> List[int]:
        return [t.deal_id for name in TRAYS for t in self.tray(name)]

    def by_id(self) -> Dict[int, TrayItem]:
        return {t.deal_id: t for name in TRAYS for t in self.tray(name)}

    def tray_of(self) -> Dict[int, str]:
        return {t.deal_id: name for name in TRAYS for t in self.tray(name)}

    def as_dict(self) -> Dict[str, Any]:
        return {"trays": {name: [asdict(t) for t in self.tray(name)] for name in TRAYS},
                "pantry": list(self.pantry), "counts": dict(self.counts),
                "label_version": self.label_version}


def _group_best(deals: Iterable[Dict[str, Any]], labels: Dict[int, Label]
                ) -> List[TrayItem]:
    """One row per (role, item, basis): the cheapest deal and its cross-chain gap."""
    groups: Dict[Tuple[str, str, str], List[Tuple[float, Dict[str, Any]]]] = {}
    for d in deals:
        label = labels.get(int(d["deal_id"]))
        if label is None:
            continue
        pb = price_basis(d, label)
        if pb is None:
            continue
        groups.setdefault((label.role, label.item, pb[0]), []).append((pb[1], d))

    best: List[TrayItem] = []
    for (role, item, basis), rows in groups.items():
        rows.sort(key=lambda r: (r[0], int(r[1]["deal_id"])))
        cmp_price, top = rows[0]
        per_chain: Dict[str, float] = {}
        for price, d in rows:  # sorted, so the first per chain is its cheapest
            per_chain.setdefault(d.get("chain") or d.get("store_name") or "?", price)
        n = len(per_chain)
        median = statistics.median(per_chain.values())
        gap = 1 - cmp_price / median if n >= 2 and median > 0 else 0.0
        best.append(TrayItem(
            deal_id=int(top["deal_id"]), product_name=top.get("product_name") or "",
            store_name=top.get("store_name"), chain=top.get("chain"),
            sale_price=float(top["sale_price"]), unit=top.get("unit"),
            item=item, role=role, basis=basis, compare_price=round(cmp_price, 2),
            median_price=round(median, 2), n_chains=n, gap=round(gap, 3)))
    return best


def _pick(best: List[TrayItem], roles: frozenset, k: int, need_compare: bool = False
          ) -> List[TrayItem]:
    rows = [b for b in best if b.role in roles and (b.n_chains >= 2 or not need_compare)]
    rows.sort(key=lambda b: (-round(b.gap, 2), BASIS_RANK[b.basis], b.compare_price, b.deal_id))
    # One row per item per tray: the same item priced per lb and per package
    # forms two groups (never compared with each other), but showing both
    # wastes a slot. The better-ranked one stays.
    seen: set = set()
    unique = []
    for b in rows:
        if b.item not in seen:  # across roles too ("tomato" as vegetable and fruit)
            seen.add(b.item)
            unique.append(b)
    return unique[:k]


def _pick_with_quotas(best: List[TrayItem], quotas: Dict[str, int], k: int
                      ) -> List[TrayItem]:
    """Fill ``k`` slots: each role first takes up to its quota of its best rows,
    then any unused slots go to the best leftovers regardless of role. The
    result is in the usual tray order."""
    ranked = _pick(best, frozenset(quotas), len(best))
    chosen: List[TrayItem] = []
    taken = {role: 0 for role in quotas}
    for row in ranked:
        if taken[row.role] < quotas[row.role]:
            chosen.append(row)
            taken[row.role] += 1
    chosen_ids = {r.deal_id for r in chosen}
    chosen += [r for r in ranked if r.deal_id not in chosen_ids][: max(0, k - len(chosen))]
    order = {r.deal_id: i for i, r in enumerate(ranked)}
    return sorted(chosen[:k], key=lambda r: order[r.deal_id])


def build_trays(deals: List[Dict[str, Any]], labels: Dict[int, Label]) -> TrayProposal:
    """Trays from already-gated deals and their labels (pure)."""
    best = _group_best(deals, labels)
    proposal = TrayProposal(
        anchors=_pick(best, ANCHOR_ROLES, N_ANCHORS),
        sides=_pick_with_quotas(best, SIDE_QUOTAS, N_SIDES),
        stock_up=_pick(best, STOCK_UP_ROLES, N_STOCK_UP, need_compare=True),
    )
    labelled = sum(1 for d in deals if int(d["deal_id"]) in labels)
    proposal.counts = {
        "gated_deals": len(deals),
        "labelled": labelled,
        "unlabelled": len(deals) - labelled,
        "anchors": len(proposal.anchors),
        "sides": len(proposal.sides),
        "stock_up": len(proposal.stock_up),
        "in_trays": len(proposal.offered_ids()),
    }
    return proposal


def offered_pantry(pantry_staples: List[Dict[str, Any]],
                   dietary_restrictions: Optional[List[str]]) -> List[Dict[str, Any]]:
    """Confirmed on-hand staples whose curated attributes clear the user's
    restrictions (the same rule as the deal gate), as ``{product_id, name}``."""
    return [
        {"product_id": int(p["product_id"]), "name": p.get("canonical_name") or ""}
        for p in pantry_staples or []
        if p.get("status", "confirmed") == "confirmed"
        and products.product_allows(p, dietary_restrictions)
    ]


def propose_trays(postal_code: str, dietary_restrictions: Optional[List[str]], *,
                  fetch_deals: Callable[..., List[Dict[str, Any]]],
                  fetch_labels: Callable[[Iterable[int]], Dict[int, Label]],
                  fetch_pantry: Callable[[], List[Dict[str, Any]]]) -> TrayProposal:
    """Gated deals -> labels -> trays, plus the pantry to confirm.

    Dependencies are injected so the route and the generation job share one
    code path and tests need no database.
    """
    restrictions = list(dietary_restrictions or [])
    deals = fetch_deals(postal_code, restrictions)
    labels = fetch_labels(int(d["deal_id"]) for d in deals)
    proposal = build_trays(deals, labels)
    proposal.pantry = offered_pantry(fetch_pantry(), restrictions)
    return proposal


def tray_row(t: TrayItem, name_len: int = 70) -> str:
    """One prompt line per tray item, for the dish planner."""
    name = " ".join(t.product_name.split())
    if len(name) > name_len:
        name = name[: name_len - 1] + "…"
    price = f"${t.sale_price:.2f}" + (f"/{t.unit}" if t.unit else "")
    per = {"lb": "/lb", "l": "/L", "each": " each", "pkg": " per pkg"}[t.basis]
    cmp_ = (f"cheapest of {t.n_chains} chains (median ${t.median_price:.2f}{per})"
            if t.n_chains >= 2 else "only chain with it")
    return f"{t.deal_id} | {name} | {price} | {t.chain or t.store_name} | item: {t.item}; {cmp_}"
