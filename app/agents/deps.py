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
* ``recipe_store`` -- ``save_recipes(user_id, recipes) -> [recipe_id, ...]``
                      (``DatabaseService``) -- the graph's only write
* ``tracker``      -- ``MLflowLogger``'s static interface
* ``recipe_ids``   -- ``(chef_id, generated recipe) -> recipe_id``
                      (``sous_chef.RecipeIdFactory``); ``None`` is a fresh
                      uuid4 per recipe. Evaluation replay injects a
                      deterministic one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .llm_factory import make_llm
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


class InMemoryRecipeStore:
    """A ``recipe_store`` that persists nothing.

    For runs that must not write the database -- tests, including the
    ``-m llm`` end-to-end test, and dry runs. Ids count up from 1 so they look
    like what the route expects in ``saved_recipe_ids``.
    """

    def __init__(self) -> None:
        self.saved: List[Dict[str, Any]] = []

    def save_recipes(self, user_id: int, recipes: List[Dict]) -> List[int]:
        ids = []
        for recipe in recipes:
            recipe_id = len(self.saved) + 1
            self.saved.append({"id": recipe_id, "user_id": user_id, "recipe": recipe})
            ids.append(recipe_id)
        return ids
