"""The one place a chat model is constructed.

Every agent gets its LLM from :func:`make_llm`, by *role*. The role fixes what
is intrinsic to the agent (sampling temperature, JSON output); the provider
and model name come from config (``settings.llm_provider`` and the per-role
model setting). Nothing else in ``app/`` may import a provider's chat class --
``test_llm_factory`` pins that.

Adding a provider means one builder function registered in ``_PROVIDERS``
(plus whatever model-name settings it needs); the agents do not change. Every
builder must set an explicit HTTP timeout (``LLM_CONNECT_TIMEOUT_SECONDS`` /
``LLM_READ_TIMEOUT_SECONDS``) so a hung model fails fast. Only
Ollama exists today, and its import is deferred to the builder so a test that
injects a fake model never needs the provider package at all.

Whatever is returned must support ``.invoke(messages)`` returning an object
with ``.content`` -- the contract ``llm_output.invoke_validated`` relies on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..config import Settings, settings as _default_settings


@dataclass(frozen=True)
class RoleSpec:
    """What a role asks of its model, independent of the provider."""

    model_setting: str   # Settings attribute holding this role's model name
    temperature: float
    json_output: bool = True
    #: Default output-token cap (``OLLAMA_NUM_PREDICT`` overrides it for all roles).
    #: Observed outputs are 160-610 tokens; this leaves headroom but stops a
    #: runaway generation (one hit 81,920 tokens / 21 minutes).
    num_predict: int = 2048


#: The agent roles. Values reproduce the ChatOllama arguments each agent
#: hard-coded before this seam existed.
ROLES: Dict[str, RoleSpec] = {
    # High-level planning
    "chef": RoleSpec("ollama_chef_model", temperature=0.7),
    # Higher creativity for recipes
    "sous_chef": RoleSpec("ollama_sous_chef_model", temperature=0.8),
    # Lower temperature for consistent (advisory) nutrition facts
    "nutritionist": RoleSpec("ollama_nutritionist_model", temperature=0.3, num_predict=1024),
}


def _spec(role: str) -> RoleSpec:
    try:
        return ROLES[role]
    except KeyError:
        raise ValueError(f"unknown LLM role {role!r}; known roles: {sorted(ROLES)}") from None


def model_name(role: str, settings: Optional[Settings] = None) -> str:
    """The configured model name for ``role`` (also what MLflow logs)."""
    settings = settings or _default_settings
    return getattr(settings, _spec(role).model_setting)


def _ollama(spec: RoleSpec, model: str, settings: Settings) -> Any:
    import httpx
    from langchain_ollama import ChatOllama

    kwargs: Dict[str, Any] = {
        "model": model,
        "base_url": settings.ollama_base_url,
        "temperature": spec.temperature,
        # Without it Ollama applies its server default window and silently
        # truncates a longer prompt (output tokens share the window too).
        "num_ctx": settings.ollama_num_ctx,
        # Caps output length: a runaway generation otherwise streams until the
        # window is full. Truncated JSON fails validation and is retried as a
        # model-output failure. Deliberately not part of model provenance.
        "num_predict": settings.ollama_num_predict or spec.num_predict,
    }
    if spec.json_output:
        kwargs["format"] = "json"
    # ChatOllama has no default timeout. client_kwargs reaches ollama.Client ->
    # httpx.Client; expiry raises httpx.ConnectTimeout / ReadTimeout, which
    # llm_errors classifies as infrastructure. write/pool use the read value.
    read = settings.llm_read_timeout_seconds
    kwargs["client_kwargs"] = {"timeout": httpx.Timeout(
        read, connect=settings.llm_connect_timeout_seconds)}
    return ChatOllama(**kwargs)


_PROVIDERS: Dict[str, Callable[[RoleSpec, str, Settings], Any]] = {
    "ollama": _ollama,
}


def make_llm(role: str, settings: Optional[Settings] = None) -> Any:
    """Build the chat model for an agent role from config."""
    settings = settings or _default_settings
    spec = _spec(role)
    provider = (settings.llm_provider or "").strip().lower()
    try:
        build = _PROVIDERS[provider]
    except KeyError:
        raise ValueError(
            f"unsupported LLM provider {settings.llm_provider!r}; "
            f"supported: {sorted(_PROVIDERS)}"
        ) from None
    return build(spec, model_name(role, settings), settings)


def provider_name(settings: Optional[Settings] = None) -> str:
    """The configured provider name, normalised as ``make_llm`` reads it."""
    settings = settings or _default_settings
    return (settings.llm_provider or "").strip().lower()


def configured_models(settings: Optional[Settings] = None) -> Dict[str, Dict[str, str]]:
    """``role -> {provider, model}`` for every role, as production builds them.

    This is the provenance a persisted generation records
    (``recipe_generations.models``) and what MLflow logs per role. A caller
    that builds its models another way (the eval harness) supplies its own map.
    """
    return {role: {"provider": provider_name(settings), "model": model_name(role, settings)}
            for role in ROLES}
