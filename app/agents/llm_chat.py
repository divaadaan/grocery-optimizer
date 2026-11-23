"""Plain chat interface for the approach-C agents: ``chat(spec, messages) -> Reply``.

The labeller, the dish planner and the per-dish SousChef call models through
this module instead of a LangChain chat model (design doc
``experiments/chef/DESIGN_APPROACH_C.md`` §10). A model is named by a *spec*:

    ollama:qwen2.5:7b
    hf:openai/gpt-oss-120b                 (the HF router picks a provider)
    hf:meta-llama/Llama-3.3-70B-Instruct:groq   (provider pinned by suffix)

Both backends are plain ``httpx`` calls, so there is no new dependency. Every
call has an explicit timeout, and transport / HTTP-status failures surface as
``httpx`` exceptions, which :mod:`app.agents.llm_errors` classifies as
infrastructure (fail fast). A reply whose text is unusable is the caller's to
validate (and retry once); this module never parses JSON for it.

Ported from ``experiments/chef/llm.py``, where both backends were exercised over
the 2026-11-23 prototype matrix. Two lessons kept:

* Ollama silently keeps only the *tail* of an over-long prompt (about
  ``num_ctx/2`` tokens), so ``num_ctx`` is sized from the prompt here, and the
  reply flags a likely truncation.
* Hybrid-thinking Qwen3 models on HF need ``enable_thinking: false`` or hidden
  reasoning eats the whole output budget.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from ..config import Settings, settings as _default_settings

HF_URL = "https://router.huggingface.co/v1/chat/completions"
PROVIDERS = ("ollama", "hf")

#: Smallest and largest Ollama context window this module will ask for.
MIN_NUM_CTX = 8192
MAX_NUM_CTX = 32768


@dataclass
class Reply:
    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    seconds: float = 0.0
    finish_reason: Optional[str] = None
    #: Ollama evaluated far fewer prompt tokens than were sent: the head of the
    #: prompt was probably dropped. Always False for HF.
    truncated_prompt: bool = False
    num_ctx: Optional[int] = None
    model: str = ""
    raw: Any = field(default=None, repr=False)

    def usage(self) -> Dict[str, Any]:
        """Token usage in the shape MLflow / provenance records."""
        return {"model": self.model, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "seconds": round(self.seconds, 2),
                "finish_reason": self.finish_reason,
                "truncated_prompt": self.truncated_prompt}


#: ``chat``'s signature, for agents that take an injectable chat function.
ChatFn = Callable[..., Reply]


def parse_spec(spec: str) -> Tuple[str, str]:
    """``"hf:openai/gpt-oss-120b"`` -> ``("hf", "openai/gpt-oss-120b")``."""
    provider, sep, name = (spec or "").strip().partition(":")
    provider = provider.lower()
    if not sep or not name or provider not in PROVIDERS:
        raise ValueError(
            f"bad model spec {spec!r}: expected 'ollama:<model>' or 'hf:<org/model>'")
    return provider, name


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Conservative token estimate (~3 chars/token for JSON-ish text)."""
    chars = sum(len(m.get("content") or "") for m in messages)
    return chars // 3 + 64


def ollama_num_ctx(est_prompt_tokens: int, max_tokens: int) -> int:
    """A window that holds the whole prompt *and* the output with room to spare.

    Ollama keeps roughly ``num_ctx/2`` of an over-long prompt, hence the 2x.
    Doubling from 8192 keeps the set of window sizes small: changing
    ``num_ctx`` between calls reloads the model.
    """
    need = 2 * est_prompt_tokens + max_tokens
    ctx = MIN_NUM_CTX
    while ctx < need and ctx < MAX_NUM_CTX:
        ctx *= 2
    return ctx


def _timeout(settings: Settings) -> httpx.Timeout:
    return httpx.Timeout(settings.llm_read_timeout_seconds,
                         connect=settings.llm_connect_timeout_seconds)


def chat(spec: str, messages: List[Dict[str, Any]], *, temperature: float = 0.2,
         max_tokens: int = 2048, json_mode: bool = True, seed: Optional[int] = None,
         settings: Optional[Settings] = None,
         client: Optional[httpx.Client] = None) -> Reply:
    """One chat completion. Raises ``httpx`` errors on transport/HTTP failure.

    ``client`` is injectable so tests can use ``httpx.MockTransport``.
    """
    settings = settings or _default_settings
    provider, name = parse_spec(spec)
    own_client = client is None
    client = client or httpx.Client(timeout=_timeout(settings))
    try:
        if provider == "ollama":
            return _ollama(client, name, messages, temperature, max_tokens, json_mode,
                           seed, settings, spec)
        return _hf(client, name, messages, temperature, max_tokens, json_mode, seed,
                   settings, spec)
    finally:
        if own_client:
            client.close()


def _ollama(client, name, messages, temperature, max_tokens, json_mode, seed,
            settings: Settings, spec: str) -> Reply:
    est = estimate_tokens(messages)
    num_ctx = ollama_num_ctx(est, max_tokens)
    options: Dict[str, Any] = {"num_ctx": num_ctx, "num_predict": max_tokens,
                               "temperature": temperature}
    if seed is not None:
        options["seed"] = seed
    body: Dict[str, Any] = {"model": name, "messages": messages, "stream": False,
                            "options": options}
    if json_mode:
        body["format"] = "json"
    t0 = time.monotonic()
    r = client.post(settings.ollama_base_url.rstrip("/") + "/api/chat", json=body)
    r.raise_for_status()
    d = r.json()
    prompt_eval = d.get("prompt_eval_count")
    return Reply(
        text=(d.get("message") or {}).get("content") or "",
        input_tokens=prompt_eval, output_tokens=d.get("eval_count"),
        seconds=time.monotonic() - t0, finish_reason=d.get("done_reason"),
        truncated_prompt=bool(prompt_eval is not None and prompt_eval < 0.6 * est),
        num_ctx=num_ctx, model=spec, raw=d,
    )


def _hf(client, name, messages, temperature, max_tokens, json_mode, seed,
        settings: Settings, spec: str) -> Reply:
    token = settings.huggingface_api_key
    if not token:
        raise RuntimeError("HUGGINGFACE_API_KEY is not set; cannot call model " + spec)
    headers = {"Authorization": f"Bearer {token}"}
    body: Dict[str, Any] = {"model": name, "messages": messages,
                            "temperature": temperature, "max_tokens": max_tokens,
                            "stream": False}
    if seed is not None:
        body["seed"] = seed
    if name.startswith("Qwen/Qwen3"):
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    t0 = time.monotonic()
    r = client.post(HF_URL, json=body, headers=headers)
    if r.status_code == 400 and "response_format" in body:
        # Not every router provider supports response_format; the prompt
        # still asks for JSON and the caller validates.
        body.pop("response_format")
        r = client.post(HF_URL, json=body, headers=headers)
    r.raise_for_status()
    d = r.json()
    choice = (d.get("choices") or [{}])[0]
    usage = d.get("usage") or {}
    return Reply(
        text=(choice.get("message") or {}).get("content") or "",
        input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
        seconds=time.monotonic() - t0, finish_reason=choice.get("finish_reason"),
        model=spec, raw=d,
    )


def parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """The first top-level JSON object in ``text``, tolerating code fences or
    prose around it; ``None`` when there is none."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    return obj if isinstance(obj, dict) else None
        start = text.find("{", start + 1)
    return None


#: Appended after an invalid reply: the error and a request for strict JSON.
RETRY_PROMPT = ("Your previous response was invalid: {error}\n\n"
                "Respond again with ONLY a valid JSON object matching the output format "
                "requested above. No prose, no comments, no markdown fences.")


class ChatOutputError(RuntimeError):
    """No schema-valid reply after every attempt (a model-output failure)."""

    def __init__(self, message: str, replies: List[Reply], last_error: str):
        super().__init__(message)
        self.replies = replies
        self.last_error = last_error


def chat_validated(chat_fn: ChatFn, spec: str, messages: List[Dict[str, Any]],
                   validate: Callable[[Dict[str, Any]], Any], *, max_attempts: int = 2,
                   **chat_kwargs) -> Tuple[Any, List[Reply]]:
    """Call the model and validate its JSON reply; on failure, show the model
    its reply and the error and ask once more (``max_attempts`` in all).

    ``validate`` takes the parsed JSON object and returns the validated value
    or raises (``ValueError`` / pydantic ``ValidationError``). Returns
    ``(value, every Reply)``. Raises :class:`ChatOutputError` when no attempt
    validates; transport/HTTP errors propagate untouched (infrastructure).
    """
    replies: List[Reply] = []
    convo = list(messages)
    last_error = "no attempts made"
    for attempt in range(1, max_attempts + 1):
        reply = chat_fn(spec, convo, **chat_kwargs)
        replies.append(reply)
        data = parse_json_object(reply.text)
        if data is None:
            last_error = ("no JSON object in the reply"
                          + (" (output was cut off at the token limit)"
                             if reply.finish_reason == "length" else ""))
        else:
            try:
                return validate(data), replies
            except (ValueError, TypeError) as e:  # pydantic ValidationError is a ValueError
                last_error = str(e)[:1500]
        if attempt < max_attempts:
            convo = convo + [{"role": "assistant", "content": reply.text},
                             {"role": "user", "content": RETRY_PROMPT.format(error=last_error)}]
    raise ChatOutputError(f"no valid output after {max_attempts} attempts: {last_error}",
                          replies, last_error)
