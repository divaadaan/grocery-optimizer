"""Recipe prompt format from arXiv 2502.02028.

Input:  <|startoftext|>[Recipe Name]\nIngredients: [list]\nInstructions:
Target: numbered cooking steps.

Divergence from the paper: <|startoftext|> is kept as a literal string rather
than registered as a special token. SmolLM's vocab doesn't contain it, and
adding a new token under QLoRA would leave its embedding untrained unless
embed_tokens/lm_head are added to modules_to_save (which defeats the memory
savings on tied-weight models). A multi-token literal is functionally
equivalent for SFT and keeps full-FT and QLoRA runs comparable.
"""

START_TOKEN = "<|startoftext|>"


def format_ingredients(ingredients: list[str]) -> str:
    return "; ".join(ingredients)


def format_steps(steps: list[str]) -> str:
    return " ".join(f"{i}. {step.strip().rstrip('.')}." for i, step in enumerate(steps, 1))


def build_prompt(name: str, ingredients: list[str]) -> str:
    """Generation-time prefix: everything up to (and including) 'Instructions:'."""
    return f"{START_TOKEN}{name}\nIngredients: {format_ingredients(ingredients)}\nInstructions:"


def build_full_text(name: str, ingredients: list[str], steps: list[str], eos_token: str = "") -> str:
    """Full training example: prompt + reference steps + EOS."""
    return f"{build_prompt(name, ingredients)} {format_steps(steps)}{eos_token}"
