"""Frozen deal/product snapshots, and the offline graph dependencies built on them.

A snapshot is a versioned JSON file holding everything a recipe-generation
run reads from the database, captured once (``scripts/capture_snapshot.py``)
and replayed forever:

* ``stores``         -- the stores the captured deals belong to
* ``deals``          -- every deal live at ``meta.as_of`` for ``meta.postal_codes``
* ``deal_products``  -- the deal <-> product junction rows for those deals
* ``products``       -- every product those rows link, plus every
                        ``assumed_on_hand`` product (the pantry staples), with
                        ``status`` and the curated ``contains_*`` attributes

From it :class:`Snapshot` builds the two read dependencies of
``GraphDeps`` (``app/agents/deps.py``):

* :class:`SnapshotDeals`   -> ``GraphDeps.deals`` (``fetch_current_deals``)
* :class:`SnapshotCatalog` -> ``GraphDeps.catalog`` (``fetch_pantry_staples``,
  ``fetch_deal_products``)

Compliance is decided from the same curated attributes as production.
``SnapshotDeals`` applies the dietary gate with
:func:`app.services.products.deal_allows`, the in-memory twin of the SQL gate
(``compliant_product_filter``), clause for clause: an unrestricted user sees
every deal; otherwise a deal needs at least one linked product and every
linked product must be ``confirmed`` and clear the restrictions. The
catalog's rows go to the same ``product_allows`` the Nutritionist's
deterministic verdict uses. No name is read to decide anything.

Validation is strict and loud: an unknown ``format_version``, a missing or
unexpected field, a wrongly typed value, a dangling reference or a deal that
is not live at ``as_of`` raises :class:`SnapshotError`. A ``null`` attribute
stays ``None`` -- it is never defaulted -- so it fails closed downstream
exactly as an undecided attribute does in the database.

The on-disk form is canonical (:func:`dump_snapshot`): records sorted by id,
keys sorted, fixed indentation. Capturing the same database state at the same
``captured_at`` gives byte-identical files, and ``load -> dump`` round-trips.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.services.products import ATTRIBUTES, deal_allows

#: Bump on any change to the file layout; the loader refuses other versions.
#:
#: 2 -- products carry ``not_kosher`` (migration 021). Version 1 files lack it
#: and are refused, not read with a silent default: the committed v1 files
#: were migrated in place with ``"not_kosher": null`` (undecided -- truthful,
#: since no product had a kosher decision when they were captured, and it
#: fails closed). Any other v1 file must be re-captured or migrated the same
#: way.
FORMAT_VERSION = 2

#: Where the committed snapshots live.
SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "snapshots"

#: ``DatabaseService.fetch_current_deals`` caps its result at this many rows.
DEAL_LIMIT = 200

_INT = (int,)
_NUM = (int, float)
_STR = (str,)
_BOOL = (bool,)
_LIST = (list,)

# field -> (accepted types, nullable)
META_FIELDS: dict[str, tuple[tuple, bool]] = {
    "name": (_STR, False),
    "format_version": (_INT, False),
    "captured_at": (_STR, False),
    "source": (_STR, False),
    "as_of": (_STR, False),
    "postal_codes": (_LIST, False),
    "schema_migration": (_STR, True),
    "notes": (_STR, True),
}

STORE_FIELDS: dict[str, tuple[tuple, bool]] = {
    "store_id": (_INT, False),
    "name": (_STR, False),
    "chain": (_STR, True),
    "postal_code": (_STR, False),
    "city": (_STR, True),
    "province": (_STR, True),
}

DEAL_FIELDS: dict[str, tuple[tuple, bool]] = {
    "deal_id": (_INT, False),
    "store_id": (_INT, False),
    "product_name": (_STR, False),
    "brand": (_STR, True),
    "sale_price": (_NUM, False),
    "regular_price": (_NUM, True),
    "discount_percentage": (_NUM, True),
    "unit": (_STR, True),
    "category": (_STR, True),
    "valid_from": (_STR, False),
    "valid_until": (_STR, False),
    "source": (_STR, False),
    "product_id": (_INT, True),
    "sale_story": (_STR, True),
    "pre_price_text": (_STR, True),
}

DEAL_PRODUCT_FIELDS: dict[str, tuple[tuple, bool]] = {
    "deal_id": (_INT, False),
    "product_id": (_INT, False),
    "tile_position": (_INT, True),
    "price_is_per_item": (_BOOL, True),
}

PRODUCT_FIELDS: dict[str, tuple[tuple, bool]] = {
    "product_id": (_INT, False),
    "canonical_name": (_STR, False),
    "category": (_STR, False),
    "status": (_STR, False),
    "assumed_on_hand": (_BOOL, False),
    "merged_into": (_INT, True),
    # Every attribute is accepted as null here, whatever the column's
    # constraint: the snapshot must carry what the database said, and the
    # consumers (product_allows / the SQL gate's IS NOT FALSE) fail closed on
    # None. Defaulting a null to False is how "unknown" becomes "safe".
    **{attr: (_BOOL, True) for attr in ATTRIBUTES},
}

#: The keys ``DatabaseService.fetch_current_deals`` returns, in its order.
SERVED_DEAL_KEYS = ("deal_id", "product_name", "sale_price", "regular_price",
                    "discount_percentage", "source", "product_id", "store_name",
                    "store_id", "chain")

#: The product keys ``recipe_catalog`` selects.
CATALOG_PRODUCT_KEYS = ("product_id", "canonical_name", "category", "status", *ATTRIBUTES)

_SECTIONS = {
    "stores": (STORE_FIELDS, ("store_id",)),
    "deals": (DEAL_FIELDS, ("deal_id",)),
    "deal_products": (DEAL_PRODUCT_FIELDS, ("deal_id", "product_id")),
    "products": (PRODUCT_FIELDS, ("product_id",)),
}


class SnapshotError(ValueError):
    """A snapshot file is malformed, of an unknown version, or inconsistent."""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _check_record(where: str, record: Any, spec: Mapping[str, tuple[tuple, bool]]) -> None:
    if not isinstance(record, dict):
        raise SnapshotError(f"{where}: expected an object, got {type(record).__name__}")
    missing = sorted(set(spec) - set(record))
    if missing:
        raise SnapshotError(f"{where}: missing field(s) {missing}")
    unexpected = sorted(set(record) - set(spec))
    if unexpected:
        raise SnapshotError(f"{where}: unexpected field(s) {unexpected}")
    for key, (types, nullable) in spec.items():
        value = record[key]
        if value is None:
            if not nullable:
                raise SnapshotError(f"{where}.{key}: must not be null")
            continue
        # bool is an int subclass; never accept it where a number is meant.
        if isinstance(value, bool) and bool not in types:
            raise SnapshotError(f"{where}.{key}: expected {types[0].__name__}, got bool")
        if not isinstance(value, types):
            raise SnapshotError(
                f"{where}.{key}: expected {types[0].__name__}, got {type(value).__name__}")


def _parse_date(where: str, value: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(value)
    except ValueError as e:
        raise SnapshotError(f"{where}: not an ISO date: {value!r}") from e


def _sort_key(fields: Sequence[str]):
    return lambda r: tuple(r[f] for f in fields)


def validate(data: Any) -> dict:
    """Check ``data`` (a parsed snapshot) and return it in canonical order.

    Raises :class:`SnapshotError` on anything wrong; never repairs.
    """
    if not isinstance(data, dict):
        raise SnapshotError("snapshot: expected a JSON object")
    expected = {"meta", *_SECTIONS}
    missing = sorted(expected - set(data))
    if missing:
        raise SnapshotError(f"snapshot: missing section(s) {missing}")
    unexpected = sorted(set(data) - expected)
    if unexpected:
        raise SnapshotError(f"snapshot: unexpected section(s) {unexpected}")

    meta = data["meta"]
    # Version first, so a future format fails on the version, not on a field.
    if not isinstance(meta, dict):
        raise SnapshotError("meta: expected an object")
    version = meta.get("format_version")
    if version != FORMAT_VERSION or isinstance(version, bool):
        hint = ""
        if version == 1 and not isinstance(version, bool):
            hint = (" -- v1 predates products.not_kosher (migration 021): add "
                    "\"not_kosher\": null to every product and set format_version "
                    "2, or re-capture")
        raise SnapshotError(
            f"meta.format_version: unsupported {version!r} (this loader reads "
            f"{FORMAT_VERSION}){hint}")
    _check_record("meta", meta, META_FIELDS)
    as_of = _parse_date("meta.as_of", meta["as_of"])
    try:
        _dt.datetime.fromisoformat(meta["captured_at"])
    except ValueError as e:
        raise SnapshotError(f"meta.captured_at: not an ISO timestamp: {meta['captured_at']!r}") from e
    postal_codes = meta["postal_codes"]
    if not postal_codes or not all(isinstance(p, str) and p for p in postal_codes):
        raise SnapshotError("meta.postal_codes: expected a non-empty list of strings")
    if len(set(postal_codes)) != len(postal_codes):
        raise SnapshotError("meta.postal_codes: duplicates")

    out: dict = {"meta": {**meta, "postal_codes": sorted(postal_codes)}}
    for section, (spec, key_fields) in _SECTIONS.items():
        rows = data[section]
        if not isinstance(rows, list):
            raise SnapshotError(f"{section}: expected a list")
        seen = set()
        for i, row in enumerate(rows):
            _check_record(f"{section}[{i}]", row, spec)
            key = tuple(row[f] for f in key_fields)
            if key in seen:
                raise SnapshotError(f"{section}[{i}]: duplicate {dict(zip(key_fields, key))}")
            seen.add(key)
        out[section] = sorted((dict(r) for r in rows), key=_sort_key(key_fields))

    stores = {s["store_id"]: s for s in out["stores"]}
    for s in out["stores"]:
        if s["postal_code"] not in postal_codes:
            raise SnapshotError(
                f"store {s['store_id']}: postal code {s['postal_code']!r} not in meta.postal_codes")
    products = {p["product_id"] for p in out["products"]}
    for d in out["deals"]:
        where = f"deal {d['deal_id']}"
        if d["store_id"] not in stores:
            raise SnapshotError(f"{where}: store {d['store_id']} not in stores")
        start = _parse_date(f"{where}.valid_from", d["valid_from"])
        end = _parse_date(f"{where}.valid_until", d["valid_until"])
        if not start <= as_of <= end:
            raise SnapshotError(
                f"{where}: valid {start}..{end} is not live at as_of {as_of}")
    deal_ids = {d["deal_id"] for d in out["deals"]}
    for link in out["deal_products"]:
        where = f"deal_products ({link['deal_id']}, {link['product_id']})"
        if link["deal_id"] not in deal_ids:
            raise SnapshotError(f"{where}: deal not in deals")
        if link["product_id"] not in products:
            raise SnapshotError(f"{where}: product not in products")
    return out


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def dump_snapshot(data: Mapping) -> str:
    """The canonical text of a snapshot (validated first)."""
    return json.dumps(validate(copy.deepcopy(dict(data))), indent=1, sort_keys=True,
                      ensure_ascii=False) + "\n"


def write_snapshot(data: Mapping, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_snapshot(data), encoding="utf-8", newline="\n")
    return path


def snapshot_path(name: str) -> Path:
    """Path of a committed snapshot by name, e.g. ``snapshot_path("seed27")``."""
    return SNAPSHOT_DIR / f"{name}.json"


def load_snapshot(source: Path | str) -> "Snapshot":
    """Load and validate a snapshot from a path, or by committed name."""
    path = Path(source)
    if not path.suffix and not path.exists():
        path = snapshot_path(str(source))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SnapshotError(f"{path}: not valid JSON: {e}") from e
    return Snapshot.from_dict(data)


# --------------------------------------------------------------------------
# The snapshot and its GraphDeps fakes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Snapshot:
    data: dict

    @classmethod
    def from_dict(cls, data: Any) -> "Snapshot":
        return cls(validate(copy.deepcopy(data)))

    @property
    def meta(self) -> dict:
        return copy.deepcopy(self.data["meta"])

    @property
    def name(self) -> str:
        return self.data["meta"]["name"]

    @property
    def as_of(self) -> _dt.date:
        return _dt.date.fromisoformat(self.data["meta"]["as_of"])

    @property
    def postal_codes(self) -> list[str]:
        return list(self.data["meta"]["postal_codes"])

    def rows(self, section: str) -> list[dict]:
        return copy.deepcopy(self.data[section])

    def linked_products(self) -> dict[int, list[dict]]:
        """``deal_id`` -> its linked product rows (full rows), in
        ``tile_position, product_id`` order, as ``recipe_catalog`` orders them."""
        products = {p["product_id"]: p for p in self.data["products"]}
        links = sorted(self.data["deal_products"],
                       key=lambda l: (l["deal_id"],
                                      l["tile_position"] is None,
                                      l["tile_position"] or 0,
                                      l["product_id"]))
        out: dict[int, list[dict]] = {}
        for link in links:
            out.setdefault(link["deal_id"], []).append(copy.deepcopy(products[link["product_id"]]))
        return out

    def deal_source(self) -> "SnapshotDeals":
        return SnapshotDeals(self)

    def catalog(self) -> "SnapshotCatalog":
        return SnapshotCatalog(self)

    def graph_deps(self, **overrides):
        """``GraphDeps`` reading this snapshot instead of the database.

        ``deals`` and ``catalog`` come from the snapshot and ``recipe_store``
        defaults to an :class:`InMemoryRecipeStore` (an evaluation run must not
        write); everything else is ``GraphDeps``' default unless overridden.
        """
        from app.agents.deps import GraphDeps, InMemoryRecipeStore

        fields = {"deals": self.deal_source(), "catalog": self.catalog(),
                  "recipe_store": InMemoryRecipeStore(), **overrides}
        return GraphDeps(**fields)


def _served_order(deal: dict):
    # ORDER BY d.discount_percentage DESC NULLS LAST, d.sale_price ASC, d.deal_id
    disc = deal["discount_percentage"]
    return (disc is None, -(disc or 0), deal["sale_price"], deal["deal_id"])


class SnapshotDeals:
    """``GraphDeps.deals``: ``DatabaseService.fetch_current_deals`` over a snapshot.

    Same filter, gate, order, cap and row shape as the SQL, with the snapshot's
    ``as_of`` standing in for ``CURRENT_DATE``. A postal code the snapshot does
    not cover raises rather than returning ``[]``: production would answer
    "no deals", but in an evaluation that is a mis-wired scenario, not a
    finding.
    """

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.calls: list[tuple[str, Optional[list]]] = []

    def fetch_current_deals(self, postal_code: str,
                            dietary_restrictions: Optional[Iterable[str]] = None) -> list[dict]:
        restrictions = list(dietary_restrictions) if dietary_restrictions is not None else None
        self.calls.append((postal_code, restrictions))
        snap = self.snapshot
        if postal_code not in snap.data["meta"]["postal_codes"]:
            raise SnapshotError(
                f"snapshot {snap.name!r} covers postal codes {snap.postal_codes}, "
                f"not {postal_code!r}")
        as_of = snap.as_of
        stores = {s["store_id"]: s for s in snap.data["stores"]}
        linked = snap.linked_products()
        served = []
        for deal in snap.data["deals"]:
            store = stores[deal["store_id"]]
            if store["postal_code"] != postal_code:
                continue
            if not (_dt.date.fromisoformat(deal["valid_from"]) <= as_of
                    <= _dt.date.fromisoformat(deal["valid_until"])):
                continue
            if not deal_allows(linked.get(deal["deal_id"], []), restrictions):
                continue
            served.append(deal)
        served.sort(key=_served_order)
        return [
            {
                "deal_id": d["deal_id"],
                "product_name": d["product_name"],
                "sale_price": d["sale_price"],
                "regular_price": d["regular_price"],
                "discount_percentage": d["discount_percentage"],
                "source": d["source"],
                "product_id": d["product_id"],
                "store_name": stores[d["store_id"]]["name"],
                "store_id": d["store_id"],
                "chain": stores[d["store_id"]]["chain"],
            }
            for d in served[:DEAL_LIMIT]
        ]


def _catalog_row(product: dict) -> dict:
    return {k: product[k] for k in CATALOG_PRODUCT_KEYS}


class SnapshotCatalog:
    """``GraphDeps.catalog``: the ``recipe_catalog`` module over a snapshot."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot

    def fetch_pantry_staples(self) -> list[dict]:
        """Confirmed ``assumed_on_hand`` products, by name (then id)."""
        rows = [p for p in self.snapshot.data["products"]
                if p["status"] == "confirmed" and p["assumed_on_hand"]]
        rows.sort(key=lambda p: (p["canonical_name"], p["product_id"]))
        return [_catalog_row(p) for p in rows]

    def fetch_deal_products(self, deal_ids: Iterable) -> dict[str, list[dict]]:
        """``str(deal_id)`` -> linked products (any status); unlinked deals absent."""
        ids = sorted({int(i) for i in deal_ids
                      if i is not None and str(i).strip().isdigit()})
        linked = self.snapshot.linked_products()
        return {str(i): [_catalog_row(p) for p in linked[i]] for i in ids if i in linked}
