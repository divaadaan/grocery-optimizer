"""App-targeted SFT dataset builder (ROADMAP item 5, step 2).

Unlike ``reproduce_paper/`` (which teaches recipe *form* on Food.com and was
shown not to move the dietary/allergen failure mode), this package renders
training examples through the app's *real* prompts
(``app.agents.prompts.PromptTemplates``) and validates every completion through
the app's *real* Pydantic schemas (``app.agents.llm_output``) at build time, so
train-time inputs match inference byte-for-byte and no malformed completion can
leak into the corpus.

Hybrid labelling:
- ``chef_plan`` and ``nutritionist_verdict`` completions are generated
  PROGRAMMATICALLY from the seed catalog + ``forbidden_terms`` — zero label
  noise on exactly the two failure modes the SFT must fix (chef puts meat in a
  vegetarian's groups; nutritionist false-rejects compliant minimal recipes).
- ``sous_chef_recipe`` completions are distilled from a strong teacher
  (``qwen2.5:7b`` via Ollama) and filtered through the RecipeBatch validator +
  forbidden-term rule checks.

The import direction (training -> app) is the one allowed by the repo's split
rule; ``app/`` never imports ``training/``.
"""
