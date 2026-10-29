"""The single LLM construction seam (app/agents/llm_factory.py).

Default config must build exactly the ChatOllama each agent used to build
itself, and no other module may construct a provider chat model. No network:
constructing a ChatOllama does not contact the server.
"""
from pathlib import Path

import pytest

from app.agents import llm_factory
from app.agents.llm_factory import make_llm, model_name
from app.config import settings

APP_DIR = Path(__file__).resolve().parents[1]

# role -> (model setting, temperature) as hard-coded in the agents before the seam
EXPECTED = {
    "chef": (settings.ollama_chef_model, 0.7),
    "sous_chef": (settings.ollama_sous_chef_model, 0.8),
    "nutritionist": (settings.ollama_nutritionist_model, 0.3),
}


@pytest.mark.parametrize("role", sorted(EXPECTED))
def test_default_config_builds_the_same_chat_ollama_as_before(role):
    from langchain_ollama import ChatOllama

    llm = make_llm(role)
    model, temperature = EXPECTED[role]
    assert isinstance(llm, ChatOllama)
    assert llm.model == model
    assert llm.temperature == temperature
    assert llm.format == "json"
    assert llm.base_url == settings.ollama_base_url
    assert model_name(role) == model


def test_agents_get_their_model_from_the_factory():
    from app.agents.chef_orchestrator import ChefOrchestrator
    from app.agents.nutritionist import Nutritionist
    from app.agents.sous_chef import SousChef

    assert ChefOrchestrator(db=object()).llm.model == settings.ollama_chef_model
    assert SousChef().llm.temperature == 0.8
    assert Nutritionist().llm.temperature == 0.3


def test_injected_llm_is_used_as_is():
    from app.agents.sous_chef import SousChef

    fake = object()
    assert SousChef(llm=fake).llm is fake


def test_unknown_role_and_provider_are_errors():
    with pytest.raises(ValueError, match="unknown LLM role"):
        make_llm("sommelier")
    other = settings.model_copy(update={"llm_provider": "not-a-provider"})
    with pytest.raises(ValueError, match="unsupported LLM provider"):
        make_llm("chef", settings=other)


def test_provider_is_read_from_config():
    built = {}

    def fake_provider(spec, model, cfg):
        built.update(spec=spec, model=model)
        return "a model"

    llm_factory._PROVIDERS["fake"] = fake_provider
    try:
        other = settings.model_copy(update={"llm_provider": "Fake"})
        assert make_llm("nutritionist", settings=other) == "a model"
        assert built["model"] == settings.ollama_nutritionist_model
        assert built["spec"].temperature == 0.3 and built["spec"].json_output
    finally:
        del llm_factory._PROVIDERS["fake"]


def test_no_module_but_the_factory_constructs_a_chat_model():
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if "tests" in path.parts or path.name == "llm_factory.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "ChatOllama" in text or "langchain_ollama" in text:
            offenders.append(str(path.relative_to(APP_DIR)))
    assert offenders == []
