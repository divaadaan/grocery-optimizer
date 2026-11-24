"""The API's generation entry point (approach C).

``run_plan_generation`` is ``app/agents/plan_pipeline.run_plan_generation``:
confirmed trays -> dish plan -> one recipe per dish -> dietary verdict ->
persist. Kept as a module attribute so the route's job worker
(``app/routes/recipes.py``) resolves it at call time and tests can patch it.
"""
from app.agents.plan_pipeline import SelectionError, run_plan_generation  # noqa: F401
