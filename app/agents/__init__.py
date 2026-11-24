"""The generation agents (approach C, ``experiments/chef/DESIGN_APPROACH_C.md``).

* ``plan_pipeline``  -- confirmed trays -> dish plan -> one recipe per dish ->
  deterministic dietary verdict -> persist.
* ``dish_planner``   -- the Chef: one LLM call over the confirmed trays.
* ``dish_writer``    -- the per-dish SousChef.
* ``dietary``        -- compliance from curated product attributes (no LLM).
* ``llm_chat``       -- the plain ``chat(spec, messages)`` client (Ollama / HF).
"""
