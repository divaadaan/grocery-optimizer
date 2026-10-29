"""Instruction steps that reference ingredients by index (ROADMAP Track C).

A SousChef instruction step is a *template*: free text in which every
ingredient is written as a placeholder ``{N}``, ``N`` being the 0-based index
of that ingredient in the recipe's own ``ingredients`` list::

    "Dice {0} and fry it in {2} until golden."

The displayed text is produced by :func:`render_step`, which substitutes each
placeholder with the *cited* item's name (the deal's ``product_name`` or the
staple product's ``canonical_name``), not the model's free-text ingredient
name. So every ingredient a step references is one that cites a deal or
staple, and that citation is what the dietary authority
(``app/agents/dietary.py``) checks.

Residual risk, stated plainly: this is a *structural* guarantee about the
references, not about the words. Nothing stops a model from typing a food word
literally in the step text ("add butter") instead of using a placeholder.
Detecting that would mean matching ingredient names or food words in free
text, which CLAUDE.md forbids downstream of product review (no word lists, no
food-noun detector, no LLM judge). The prompt requires placeholders for every
ingredient; the scorer reports how often steps reference ingredients at all.

Pure functions, standard library only, so the pydantic schemas
(``llm_output``) and the offline eval scorers can share one parser.
"""
import re
from typing import Callable, List, NamedTuple, Sequence

#: A well-formed placeholder: a non-negative integer in single braces.
PLACEHOLDER = re.compile(r"\{(\d+)\}")
#: Anything brace-shaped left after removing well-formed placeholders:
#: ``{}``, ``{a}``, ``{ 1 }``, ``{-1}``, ``{{0}}``'s outer braces, a lone brace.
_MALFORMED = re.compile(r"\{[^{}]*\}|[{}]")


class ParsedStep(NamedTuple):
    """One template step: the ingredient indices it references, in order of
    appearance (repeats kept), and any malformed brace tokens."""
    refs: List[int]
    malformed: List[str]


def parse_step(template: str) -> ParsedStep:
    refs = [int(m) for m in PLACEHOLDER.findall(template)]
    malformed = _MALFORMED.findall(PLACEHOLDER.sub("", template))
    return ParsedStep(refs, malformed)


def step_problems(templates: Sequence[str], n_ingredients: int) -> List[str]:
    """Human-readable problems that make a recipe's steps unrenderable:
    malformed placeholders, indices outside ``0..n_ingredients-1``, and a
    recipe whose steps reference no ingredient at all. Empty list = valid.

    Unreferenced ingredients are *not* a problem here; see
    :func:`unreferenced`."""
    problems: List[str] = []
    any_ref = False
    valid = (f"{{0}}..{{{n_ingredients - 1}}}" if n_ingredients > 0
             else "none (the recipe has no ingredients)")
    for i, template in enumerate(templates, 1):
        parsed = parse_step(template)
        if parsed.malformed:
            problems.append(
                f"step {i} has malformed placeholder(s) {parsed.malformed}: write "
                "an ingredient only as {N}, N its 0-based index in the "
                "ingredients list, and use no other braces")
        bad = sorted({r for r in parsed.refs if r >= n_ingredients})
        if bad:
            problems.append(
                f"step {i} references ingredient index(es) {bad} but the recipe "
                f"has {n_ingredients} ingredient(s); valid placeholders: {valid}")
        any_ref = any_ref or bool(parsed.refs)
    if templates and not any_ref:
        problems.append(
            "no instruction step references an ingredient: write every "
            f"ingredient in the steps as a placeholder ({valid}), e.g. "
            "\"Slice {0} and toss it with {1}.\"")
    return problems


def referenced(templates: Sequence[str]) -> List[int]:
    """Sorted distinct ingredient indices referenced by any step."""
    return sorted({r for t in templates for r in parse_step(t).refs})


def unreferenced(templates: Sequence[str], n_ingredients: int) -> List[int]:
    """Indices of ingredients no step references. Reported, never rejected:
    an unused ingredient is cited and compliance-checked like any other, so
    it is a recipe-quality gap, not a compliance hole."""
    used = set(referenced(templates))
    return [i for i in range(n_ingredients) if i not in used]


def render_step(template: str, name_of: Callable[[int], str]) -> str:
    """Substitute every ``{N}`` with ``name_of(N)``. Call only on steps that
    passed :func:`step_problems`."""
    return PLACEHOLDER.sub(lambda m: name_of(int(m.group(1))), template)


def structured_steps(templates: Sequence[str]) -> List[dict]:
    """The structured form kept on a Recipe beside the rendered text:
    ``[{"template": str, "ingredient_refs": [int, ...]}]``."""
    return [{"template": t, "ingredient_refs": parse_step(t).refs} for t in templates]


__all__ = ["PLACEHOLDER", "ParsedStep", "parse_step", "referenced", "render_step",
           "step_problems", "structured_steps", "unreferenced"]
