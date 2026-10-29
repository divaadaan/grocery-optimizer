"""Pydantic validation of agent LLM output, with retry-on-invalid.

Roadmap: "Pydantic-validate agent JSON output; retry on parse failure."
Ollama's format="json" guarantees syntactically valid JSON but not the
requested shape. These schemas coerce the shape variants observed in the
2026-06-12 verification runs (bare list vs wrapper object, string vs list
instructions, "$5.99" prices, "45 minutes" prep times) and reject the rest;
invoke_validated() feeds the validation error back to the model and retries.
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .instruction_refs import step_problems, unreferenced

# NB: langchain_core is imported lazily inside invoke_validated (the only place
# it's used) so the schemas below can be imported with just pydantic installed —
# the training/ dataset builder reuses them as the build-time validation gate
# and its venv deliberately doesn't carry the langchain stack.


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


# --- Chef ---------------------------------------------------------------

class IngredientSelection(BaseModel):
    """One product the Chef assigns to a SousChef group."""
    model_config = ConfigDict(extra="allow")

    product_name: str
    quantity_estimate: str = ""
    deal_id: Union[int, str, None] = None

    @field_validator("quantity_estimate", mode="before")
    @classmethod
    def _stringify(cls, v):
        return "" if v is None else str(v)


class ChefPlan(BaseModel):
    """Output schema for CHEF_INGREDIENT_PLANNING."""
    model_config = ConfigDict(extra="ignore")

    ingredient_groups: List[List[IngredientSelection]] = Field(min_length=3, max_length=3)
    ingredient_reuse_map: Dict[str, int] = Field(default_factory=dict)
    rationale: str = ""

    @field_validator("ingredient_groups")
    @classmethod
    def _groups_nonempty(cls, groups):
        if any(not group for group in groups):
            raise ValueError("every ingredient group must contain at least one product")
        return groups


# --- SousChef -----------------------------------------------------------

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
    ``invoke_validated``'s retry. An ingredient no step references is only
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
        :func:`citation_context`), so this runs inside ``invoke_validated``'s
        retry loop and a bad citation goes back to the model with the list of
        valid ids. Without a context (e.g. the training/ dataset gate) only
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


# --- Nutritionist -------------------------------------------------------

class NutritionistVerdict(BaseModel):
    """Output schema for NUTRITIONIST_VALIDATION. `approved` is required —
    a verdict without it is meaningless and triggers a retry."""
    model_config = ConfigDict(extra="ignore")

    approved: bool
    feedback: str = ""
    nutrition_facts: Dict[str, Any] = Field(default_factory=dict)
    dietary_compliance: Dict[str, Any] = Field(default_factory=dict)
    health_score: float = 0.0

    @field_validator("feedback", mode="before")
    @classmethod
    def _stringify(cls, v):
        return "" if v is None else str(v)

    @field_validator("health_score", mode="before")
    @classmethod
    def _score(cls, v):
        return _lenient_float(v)


# --- Invoke with validation + retry --------------------------------------

class LLMOutputError(RuntimeError):
    """Model failed to produce schema-valid JSON within max_attempts.

    Carries the raw model responses (`raw_attempts`) and the final parse/
    validation error so callers can report exactly what the model emitted for an
    output that never validated. Without this the bad output was discarded and
    only a truncated error string survived, making failures hard to diagnose.
    """

    def __init__(self, message: str, raw_attempts: Optional[List[str]] = None,
                 last_error: str = ""):
        super().__init__(message)
        self.raw_attempts: List[str] = raw_attempts or []
        self.last_error = last_error

    @property
    def last_raw(self) -> Optional[str]:
        """The most recent raw model response, or None if none was captured."""
        return self.raw_attempts[-1] if self.raw_attempts else None


def raw_output_report(err: "LLMOutputError", max_chars: int = 2000) -> str:
    """Readable dump of what the model actually emitted across attempts, for
    logging when an output never validated. Long responses are truncated."""
    if not err.raw_attempts:
        return "(no raw model output captured)"
    n = len(err.raw_attempts)
    lines = []
    for i, raw in enumerate(err.raw_attempts, 1):
        text = raw if len(raw) <= max_chars else (
            raw[:max_chars] + f"… [+{len(raw) - max_chars} chars]"
        )
        lines.append(f"  attempt {i}/{n}: {text}")
    return "\n".join(lines)


_RETRY_PROMPT = """Your previous response was invalid: {error}

Respond again with ONLY a valid JSON document matching the output format \
requested above. No prose, no markdown fences."""

T = TypeVar("T", bound=BaseModel)


def invoke_validated(
    llm,
    prompt: str,
    schema: Type[T],
    max_attempts: int = 3,
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[T, str]:
    """Invoke the LLM and validate its JSON output against `schema`.

    On parse/validation failure, the model's bad response and the error are
    appended to the conversation and it is asked again. Returns
    (validated model, raw content of the accepted response); raises
    LLMOutputError once attempts are exhausted.

    ``context`` is passed to pydantic as the validation context, for checks
    that need per-call data (e.g. :func:`citation_context`). Its failures are
    ordinary validation errors and get the same feedback-and-retry.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    messages = [HumanMessage(content=prompt)]
    last_error = "no attempts made"
    raw_attempts: List[str] = []

    for attempt in range(1, max_attempts + 1):
        response = llm.invoke(messages)
        raw = response.content
        raw_attempts.append(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            last_error = f"not valid JSON ({e})"
        else:
            try:
                return schema.model_validate(data, context=context), raw
            except ValidationError as e:
                last_error = f"JSON did not match the required schema:\n{str(e)[:1500]}"

        if attempt < max_attempts:
            print(f"[llm_output] {schema.__name__} attempt {attempt}/{max_attempts} "
                  "invalid — retrying")
            messages = messages + [
                AIMessage(content=raw),
                HumanMessage(content=_RETRY_PROMPT.format(error=last_error)),
            ]

    raise LLMOutputError(
        f"{schema.__name__}: no valid output after {max_attempts} attempts — {last_error}",
        raw_attempts=raw_attempts,
        last_error=last_error,
    )
