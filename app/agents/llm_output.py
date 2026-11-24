"""Pydantic schemas for the SousChef's recipe output.

A model's JSON can be syntactically valid but the wrong shape. These schemas
coerce the shape variants observed in practice (bare list vs wrapper object,
string vs list instructions, "$5.99" prices, "45 minutes" prep times) and
reject the rest; ``llm_chat.chat_validated`` feeds a validation error back to
the model once (``app/agents/dish_writer.py``).
"""
import re
from typing import Any, Dict, List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .instruction_refs import step_problems, unreferenced


def _lenient_int(value: Any, default: int) -> int:
    """'45 minutes' -> 45; None/'' -> default."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        return int(match.group()) if match else default
    return value


def _lenient_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, str):
        value = value.strip().lstrip("$") or 0.0
    return value


def _citation_id(value: Any) -> Any:
    """Coerce a citation to an int id: 12, "12" and " 12 " -> 12; None/"" ->
    None. Anything else is passed through so validation fails with the
    offending value in the error the model sees."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return value  # True is not an id; let int validation reject it
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


class RecipeIngredient(BaseModel):
    """One recipe ingredient. It must cite exactly one source:

    * ``deal_id``    -- a deal assigned to this SousChef, or
    * ``product_id`` -- an always-on-hand pantry staple product.

    ``name`` is for display only; dietary compliance and costing read the
    citation, never the name (``dietary.py``, ``costing.py``). Whether the
    cited id was actually *offered* to this SousChef is checked by
    :class:`RecipeBatch` against the validation context, because that set is
    per-call data the schema itself cannot know.
    """
    model_config = ConfigDict(extra="allow")

    name: str
    deal_id: Optional[int] = None
    product_id: Optional[int] = None
    quantity: str = ""
    unit: str = ""
    # Legacy, model-supplied. Never used for cost: costing.py prices from the
    # citation. Accepted so older outputs still parse.
    price: float = 0.0

    @field_validator("quantity", "unit", mode="before")
    @classmethod
    def _stringify(cls, v):
        return "" if v is None else str(v)

    @field_validator("price", mode="before")
    @classmethod
    def _price(cls, v):
        return _lenient_float(v)

    @field_validator("deal_id", "product_id", mode="before")
    @classmethod
    def _ids(cls, v):
        return _citation_id(v)

    @model_validator(mode="after")
    def _exactly_one_citation(self):
        cited = (self.deal_id is not None) + (self.product_id is not None)
        if cited != 1:
            what = "no citation" if cited == 0 else "both a deal_id and a product_id"
            raise ValueError(
                f"ingredient {self.name!r} has {what}: every ingredient must cite "
                "exactly one of deal_id (one of your assigned deals) or "
                "product_id (one of the listed pantry staples)"
            )
        return self


class GeneratedRecipe(BaseModel):
    """Output schema for one recipe from SOUS_CHEF_RECIPE_GENERATION / retry.

    ``instructions`` are *templates* that write every ingredient as a
    placeholder ``{N}``, its 0-based index in ``ingredients``
    (``app/agents/instruction_refs.py``). A malformed placeholder, an index
    out of range, or steps that reference no ingredient at all are
    validation errors, so they go back to the model through
    ``chat_validated``'s retry. An ingredient no step references is only
    reported (:meth:`unreferenced_ingredients`): it is cited and
    compliance-checked like the rest, and rejecting the whole batch over it
    would cost small models retries for no compliance gain.
    """
    model_config = ConfigDict(extra="ignore")

    name: str
    ingredients: List[RecipeIngredient] = Field(min_length=1)
    instructions: List[str] = Field(min_length=1)
    servings: int = 2
    total_cost: float = 0.0  # model arithmetic — informational only, see costing.py
    estimated_prep_time: int = 30
    meal_type: str = "dinner"
    cuisine_type: Optional[str] = None

    @field_validator("instructions", mode="before")
    @classmethod
    def _instructions(cls, v):
        if isinstance(v, str):
            return [v]
        return v

    @model_validator(mode="after")
    def _instruction_refs(self):
        problems = step_problems(self.instructions, len(self.ingredients))
        if problems:
            raise ValueError(
                f"recipe {self.name!r} instructions: " + "; ".join(problems))
        return self

    def unreferenced_ingredients(self) -> List[int]:
        """Indices of ingredients that no instruction step references."""
        return unreferenced(self.instructions, len(self.ingredients))

    @field_validator("servings", mode="before")
    @classmethod
    def _servings(cls, v):
        return _lenient_int(v, default=2)

    @field_validator("estimated_prep_time", mode="before")
    @classmethod
    def _prep_time(cls, v):
        return _lenient_int(v, default=30)

    @field_validator("total_cost", mode="before")
    @classmethod
    def _cost(cls, v):
        return _lenient_float(v)


class RecipeBatch(BaseModel):
    """A list of recipes, coerced from the shape variants models actually emit:
    bare array, single recipe object, {"recipes": [...]}, or
    {"recipe_1": {...}, "recipe_2": {...}}."""

    recipes: List[GeneratedRecipe] = Field(min_length=1)

    @staticmethod
    def _looks_like_recipe(obj) -> bool:
        # distinguishes a recipe from a recipe *ingredient*, which also has "name"
        return isinstance(obj, dict) and "name" in obj and (
            "ingredients" in obj or "instructions" in obj
        )

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        if isinstance(data, list):
            return {"recipes": [r for r in data if isinstance(r, dict)]}
        if isinstance(data, dict):
            if isinstance(data.get("recipes"), list):
                return data
            if cls._looks_like_recipe(data):
                return {"recipes": [data]}
            for value in data.values():
                if isinstance(value, list) and any(
                    cls._looks_like_recipe(r) for r in value
                ):
                    return {"recipes": [r for r in value if isinstance(r, dict)]}
            recipe_like = [v for v in data.values() if cls._looks_like_recipe(v)]
            if recipe_like:
                return {"recipes": recipe_like}
        return data  # let validation fail with a useful error

    @model_validator(mode="after")
    def _citations_were_offered(self, info: ValidationInfo):
        """Every citation must name an id this SousChef was actually offered.

        The allowed sets arrive through pydantic's validation context (see
        :func:`citation_context`), so this runs inside ``chat_validated``'s
        retry loop and a bad citation goes back to the model with the list of
        valid ids. Without a context (e.g. a scorer re-checking a reply) only
        the structural one-citation rule above applies.
        """
        ctx = info.context or {}
        if CITATION_CONTEXT_KEY not in ctx:
            return self
        allowed_deals, allowed_products = ctx[CITATION_CONTEXT_KEY]
        bad = []
        for recipe in self.recipes:
            for ing in recipe.ingredients:
                if ing.deal_id is not None and ing.deal_id not in allowed_deals:
                    bad.append(f"{ing.name!r} cites deal_id {ing.deal_id}")
                if ing.product_id is not None and ing.product_id not in allowed_products:
                    bad.append(f"{ing.name!r} cites product_id {ing.product_id}")
        if bad:
            raise ValueError(
                "ingredients cite ids you were not given: " + "; ".join(bad)
                + f". Valid deal_ids: {sorted(allowed_deals)}. Valid pantry "
                f"product_ids: {sorted(allowed_products)}. Use only these ids, "
                "or leave the ingredient out."
            )
        return self


#: Validation-context key carrying (allowed_deal_ids, allowed_product_ids).
CITATION_CONTEXT_KEY = "citations"


def citation_context(deal_ids, product_ids) -> Dict[str, Any]:
    """Validation context for :class:`RecipeBatch`: the ids one SousChef call
    may cite. Ids are coerced to int, matching :class:`RecipeIngredient`."""
    def ints(ids):
        out = set()
        for i in ids:
            v = _citation_id(i)
            if isinstance(v, int) and not isinstance(v, bool):
                out.add(v)
        return frozenset(out)
    return {CITATION_CONTEXT_KEY: (ints(deal_ids), ints(product_ids))}
