"""One-off probe: inspect raw Chef and SousChef model output shapes."""
import json
import os

from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("OLLAMA_BASE_URL", "http://172.18.128.1:11434")

from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage

from app.agents.prompts import PromptTemplates
from app.services.database import DatabaseService

deals = DatabaseService().fetch_current_deals("M5V3A8")
print(f"deals: {len(deals)}, sample: {deals[0]}")

chef_llm = ChatOllama(model="qwen2.5:7b", base_url=os.environ["OLLAMA_BASE_URL"],
                      temperature=0.7, format="json")
prompt = PromptTemplates.CHEF_INGREDIENT_PLANNING.format(
    deals_json=json.dumps(deals[:100], indent=2),
    budget=75.0, household_size=2, num_meals=7,
    dietary_restrictions="vegetarian, no_nuts", preferences="{}",
)
resp = chef_llm.invoke([HumanMessage(content=prompt)])
print("\n=== CHEF RAW (first 2000 chars) ===")
print(resp.content[:2000])

parsed = json.loads(resp.content)
groups = parsed.get("ingredient_groups", [])
print("\ngroups:", len(groups), "sizes:", [len(g) if isinstance(g, list) else type(g).__name__ for g in groups])

if groups and isinstance(groups[0], list) and groups[0]:
    sous_llm = ChatOllama(model="phi4-mini", base_url=os.environ["OLLAMA_BASE_URL"],
                          temperature=0.8, format="json")
    sprompt = PromptTemplates.SOUS_CHEF_RECIPE_GENERATION.format(
        ingredients_json=json.dumps(groups[0], indent=2),
        target_recipe_count=2, servings=2, dietary_restrictions="vegetarian, no_nuts",
    )
    sresp = sous_llm.invoke([HumanMessage(content=sprompt)])
    print("\n=== SOUS CHEF RAW (first 2000 chars) ===")
    print(sresp.content[:2000])
