"""Deterministic dietary-compliance verdict for recipes: a lookup, not a match.

Dietary compliance is decided in **code**, not by the Nutritionist LLM. Every
model tried (SmolLM2-360M SFT, qwen2.5:1.5b, llama3.1:8b, mistral:7b)
false-rejected compliant recipes and confabulated violations (ROADMAP item 5,
2026-07-15), so the LLM is advisory-only and this module owns approve/reject.

How the verdict is reached
--------------------------
Every SousChef ingredient **cites** what it is made from: either the
``deal_id`` of a deal assigned to that SousChef, or the ``product_id`` of an
always-on-hand pantry staple (``products.assumed_on_hand``, migration 014).
The citation is resolved to curated ``products`` rows and the verdict is
:func:`app.services.products.product_allows` on those rows -- the same
authority the deal gate uses. Nothing here reads an ingredient's display name;
it is carried into violation messages for humans and never decides anything.

* cited deal  -> every product linked to that deal through ``deal_products``
  (a multi-product flyer tile has several) must be ``confirmed`` and clear the
  restrictions. A deal with no confirmed linked product fails closed.
* cited staple -> the staple product must be known, ``confirmed`` and clear
  the restrictions.
* no citation -> fails closed. Output validation already rejects uncited
  ingredients (``llm_output``), so reaching here means something bypassed it.

A user with no restriction this app understands is approved without lookups:
there is nothing for a product attribute to decide.

An attribute must be explicitly ``False`` to comply. ``None`` -- an undecided
``contains_animal_byproduct`` / ``contains_gluten`` on a product confirmed
before migration 015 / 017 --
fails closed, so a vegan recipe citing it is rejected until a reviewer decides.

The substring oracle this module used to be -- ``eggplant`` read as egg,
``coconut milk`` as dairy, while ``anchovies``, ``prosciutto`` and ``gelatin``
slipped through -- has been deleted. No product name is matched anywhere.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence

from app.services import products as _products

#: Attributes whose failure is an allergen problem rather than a diet choice.
#: Drives the ``allergen_free`` / ``meets_restrictions`` split.
ALLERGEN_ATTRIBUTES = frozenset({"contains_nuts"})


# --------------------------------------------------------------------------
# Citation-based recipe verdict
# --------------------------------------------------------------------------

def _key(value: Any) -> Optional[str]:
    """Normalise an id to the string key the catalog maps are keyed by."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    return str(value).strip() or None


@dataclass(frozen=True)
class CitationCatalog:
    """The curated products a recipe's citations can resolve to.

    ``deal_products`` maps ``str(deal_id)`` to the products linked to that deal
    (``deal_products`` junction, with ``status`` and the attribute columns).
    ``staples`` maps ``str(product_id)`` to an always-on-hand staple product.
    Built once per run by the graph's ``load_recipe_catalog`` node.
    """

    deal_products: Mapping[str, Sequence[dict]] = field(default_factory=dict)
    staples: Mapping[str, dict] = field(default_factory=dict)

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "CitationCatalog":
        staples = {
            _key(p.get("product_id")): p
            for p in (state.get("pantry_staples") or [])
            if _key(p.get("product_id")) is not None
        }
        return cls(deal_products=dict(state.get("deal_products") or {}), staples=staples)


@dataclass(frozen=True)
class Violation:
    """One ingredient that failed, and why. ``attributes`` are the product
    attributes that failed (or every required one when the product could not
    be resolved, since nothing is known about it)."""

    ingredient: str
    citation: str
    attributes: tuple[str, ...]
    reason: str

    def describe(self) -> str:
        return f"{self.ingredient} ({self.citation}): {self.reason}"


def _failed_attributes(product: dict, required: tuple[str, ...]) -> tuple[str, ...]:
    """Attributes that do not clear -- None (undecided) counts as failing,
    exactly as in ``product_allows``."""
    return tuple(a for a in required if product.get(a) is None or product.get(a))


def _describe_failed(product: dict, failed: tuple[str, ...]) -> str:
    """'has contains_meat' / 'has contains_animal_byproduct undecided'.

    Kept apart because they mean different things to whoever reads the
    feedback: one is a fact, the other is a review that has not happened."""
    has = [a for a in failed if product.get(a)]
    undecided = [a for a in failed if product.get(a) is None]
    parts = []
    if has:
        parts.append(f"has {', '.join(has)}")
    if undecided:
        parts.append(f"has {', '.join(undecided)} undecided (not yet reviewed)")
    return "; ".join(parts) or f"has {', '.join(failed)}"


def _product_label(product: dict) -> str:
    return f"product {product.get('product_id')} '{product.get('canonical_name', '?')}'"


def _check_products(
    display: str, citation: str, linked: Sequence[dict], restrictions, required
) -> list[Violation]:
    if not linked:
        return [Violation(display, citation, required,
                          "no confirmed product is linked to it, so it cannot be "
                          "shown to clear the restriction")]
    out = []
    for product in linked:
        if product.get("status") != "confirmed":
            out.append(Violation(display, citation, required,
                                 f"{_product_label(product)} is not a confirmed product"))
        elif not _products.product_allows(product, restrictions):
            failed = _failed_attributes(product, required) or required
            out.append(Violation(
                display, citation, failed,
                f"{_product_label(product)} {_describe_failed(product, failed)}"))
    return out


def ingredient_violations(
    ingredient: dict, restrictions: Sequence[str], catalog: CitationCatalog
) -> list[Violation]:
    """Why one cited ingredient fails ``restrictions`` (empty == compliant)."""
    required = _products.restrictions_to_attributes(restrictions)
    if not required:
        return []
    display = str(ingredient.get("name") or "unnamed ingredient")
    deal_key = _key(ingredient.get("deal_id"))
    product_key = _key(ingredient.get("product_id"))

    if deal_key is not None:
        linked = catalog.deal_products.get(deal_key) or []
        return _check_products(display, f"deal {deal_key}", linked, restrictions, required)
    if product_key is not None:
        staple = catalog.staples.get(product_key)
        if staple is None:
            return [Violation(display, f"pantry product {product_key}", required,
                              "not a known pantry staple")]
        return _check_products(display, f"pantry product {product_key}", [staple],
                               restrictions, required)
    return [Violation(display, "no citation", required,
                      "cites neither a deal nor a pantry product, so it cannot "
                      "be checked")]


def recipe_violations(
    recipe: dict, restrictions: Sequence[str], catalog: CitationCatalog
) -> list[Violation]:
    """Every violation in ``recipe`` (empty == compliant). Reads
    ``ingredients[*].deal_id`` / ``product_id``; never ``name``."""
    out: list[Violation] = []
    for ingredient in recipe.get("ingredients", []):
        if isinstance(ingredient, dict):
            out.extend(ingredient_violations(ingredient, restrictions, catalog))
        else:
            required = _products.restrictions_to_attributes(restrictions)
            if required:
                out.append(Violation(str(ingredient), "no citation", required,
                                     "is not a structured, cited ingredient"))
    return out


def compliance_report(
    recipe: dict, restrictions: Sequence[str], catalog: CitationCatalog
) -> dict:
    """Full deterministic verdict for a recipe.

    Returns ``approved`` (no violations at all), the human-readable
    ``violations`` list, the ``allergen_free`` / ``meets_restrictions`` split
    the ValidationResult carries, and ``details`` (the structured
    :class:`Violation` records, as dicts).
    """
    violations = recipe_violations(recipe, restrictions, catalog)
    allergen = [v for v in violations if ALLERGEN_ATTRIBUTES & set(v.attributes)]
    diet = [v for v in violations if set(v.attributes) - ALLERGEN_ATTRIBUTES]
    return {
        "approved": not violations,
        "violations": [v.describe() for v in violations],
        "allergen_free": not allergen,
        "meets_restrictions": not diet,
        "details": [{**asdict(v), "attributes": list(v.attributes)} for v in violations],
    }
