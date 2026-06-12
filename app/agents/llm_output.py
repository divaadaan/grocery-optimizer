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

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


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

class RecipeIngredient(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    quantity: str = ""
    unit: str = ""
    price: float = 0.0

    @field_validator("quantity", "unit", mode="before")
    @classmethod
    def _stringify(cls, v):
        return "" if v is None else str(v)

    @field_validator("price", mode="before")
    @classmethod
    def _price(cls, v):
        return _lenient_float(v)


class GeneratedRecipe(BaseModel):
    """Output schema for one recipe from SOUS_CHEF_RECIPE_GENERATION / retry."""
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
                if isinstance(value, list) and any(cls._looks_like_recipe(r) for r in value):
                    return {"recipes": [r for r in value if isinstance(r, dict)]}
            recipe_like = [v for v in data.values() if cls._looks_like_recipe(v)]
            if recipe_like:
                return {"recipes": recipe_like}
        return data  # let validation fail with a useful error


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
    """Model failed to produce schema-valid JSON within max_attempts."""


_RETRY_PROMPT = """Your previous response was invalid: {error}

Respond again with ONLY a valid JSON document matching the output format \
requested above. No prose, no markdown fences."""

T = TypeVar("T", bound=BaseModel)


def invoke_validated(
    llm,
    prompt: str,
    schema: Type[T],
    max_attempts: int = 3,
) -> Tuple[T, str]:
    """Invoke the LLM and validate its JSON output against `schema`.

    On parse/validation failure, the model's bad response and the error are
    appended to the conversation and it is asked again. Returns
    (validated model, raw content of the accepted response); raises
    LLMOutputError once attempts are exhausted.
    """
    messages = [HumanMessage(content=prompt)]
    last_error = "no attempts made"

    for attempt in range(1, max_attempts + 1):
        response = llm.invoke(messages)
        raw = response.content
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            last_error = f"not valid JSON ({e})"
        else:
            try:
                return schema.model_validate(data), raw
            except ValidationError as e:
                last_error = f"JSON did not match the required schema:\n{str(e)[:1500]}"

        if attempt < max_attempts:
            print(f"[llm_output] {schema.__name__} attempt {attempt}/{max_attempts} invalid — retrying")
            messages = messages + [
                AIMessage(content=raw),
                HumanMessage(content=_RETRY_PROMPT.format(error=last_error)),
            ]

    raise LLMOutputError(
        f"{schema.__name__}: no valid output after {max_attempts} attempts — {last_error}"
    )
