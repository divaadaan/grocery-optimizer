"""Everything the recipe graph reaches outside itself, in one object.

``create_recipe_generation_graph(deps)`` wires these into the nodes instead of
the module-level singletons the graph used to build at import time. Every
field defaults to what production uses, so ``GraphDeps()`` is today's
behaviour and a caller overrides only what it needs, e.g.
``GraphDeps(recipe_store=InMemoryRecipeStore())``.

Interfaces (duck-typed, matching the production objects):

* ``*_llm``        -- ``.invoke(messages) -> obj with .content``
                      (``llm_factory.make_llm``)
* ``deals``        -- ``fetch_current_deals(postal_code, dietary_restrictions=...)``
                      (``DatabaseService``; it applies the dietary gate)
* ``catalog``      -- ``fetch_pantry_staples()`` / ``fetch_deal_products(deal_ids)``
                      (the ``recipe_catalog`` module)
* ``recipe_store`` -- ``save_recipes(user_id, recipes, generation=None)
                      -> [recipe_id, ...]`` (``DatabaseService``) -- the
                      graph's only write. ``generation`` is the provenance
                      dict ``finalize_meal_plan`` builds (``generation_id``,
                      models, restrictions, budget, snapshot, ...); a store
                      persists it with the recipes, atomically.
* ``tracker``      -- ``MLflowLogger``'s static interface
* ``recipe_ids``   -- ``(chef_id, generated recipe) -> recipe_id``
                      (``sous_chef.RecipeIdFactory``); ``None`` is a fresh
                      uuid4 per recipe. Evaluation replay injects a
                      deterministic one.
* ``models``       -- ``role -> {provider, model}`` of the three LLMs, recorded
                      as provenance (``recipe_generations.models``, MLflow
                      params). Defaults to what ``make_llm`` builds from config;
                      a caller injecting other models passes their identities.
* ``snapshot``     -- the eval snapshot identity the deals come from
                      (``app.eval.runner.snapshot_identity``); ``None`` in
                      production (live deals).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .llm_factory import configured_models, make_llm
from ..services import recipe_catalog
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger


@dataclass
class GraphDeps:
    chef_llm: Any = field(default_factory=lambda: make_llm("chef"))
    sous_chef_llm: Any = field(default_factory=lambda: make_llm("sous_chef"))
    nutritionist_llm: Any = field(default_factory=lambda: make_llm("nutritionist"))
    deals: Any = field(default_factory=DatabaseService)
    catalog: Any = field(default_factory=lambda: recipe_catalog)
    recipe_store: Any = field(default_factory=DatabaseService)
    tracker: Any = field(default_factory=lambda: MLflowLogger)
    recipe_ids: Optional[Any] = None
    models: Dict[str, Dict[str, str]] = field(default_factory=configured_models)
    snapshot: Optional[Dict[str, Any]] = None


class InMemoryRecipeStore:
    """A ``recipe_store`` that persists nothing.

    For runs that must not write the database -- tests, including the
    ``-m llm`` end-to-end test, and dry runs. Ids count up from 1 so they look
    like what the route expects in ``saved_recipe_ids``.

    Mirrors ``DatabaseService``: ``saved`` holds one entry per recipe (with
    its ``generation_id``), ``generations`` one provenance dict per generation
    keyed by ``generation_id`` -- the ``recipe_generations`` row.
    """

    def __init__(self) -> None:
        self.saved: List[Dict[str, Any]] = []
        self.generations: Dict[str, Dict[str, Any]] = {}

    def save_recipes(self, user_id: int, recipes: List[Dict],
                     generation: Optional[Dict[str, Any]] = None) -> List[int]:
        generation_id = (generation or {}).get("generation_id")
        if generation_id is not None:
            self.generations[generation_id] = {
                **generation, "user_id": user_id, "num_recipes_saved": len(recipes)}
        ids = []
        for recipe in recipes:
            recipe_id = len(self.saved) + 1
            self.saved.append({"id": recipe_id, "user_id": user_id,
                               "generation_id": generation_id, "recipe": recipe})
            ids.append(recipe_id)
        return ids

    def generation_recipes(self, generation_id: str) -> List[Dict[str, Any]]:
        """The recipes one generation saved, in save order."""
        return [e["recipe"] for e in self.saved if e["generation_id"] == generation_id]
