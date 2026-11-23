"""One chat interface over local Ollama and Hugging Face Inference Providers.

Model specs:
    ollama:qwen2.5:7b
    hf:Qwen/Qwen2.5-72B-Instruct          (router picks a provider)
    hf:meta-llama/Llama-3.3-70B-Instruct:groq   (provider pinned with a suffix)

Messages and tools use the OpenAI chat format for both backends. Every call
returns a ``Reply`` with the raw text, any tool calls, token usage, latency and
-- for Ollama -- whether the prompt was silently truncated (Ollama keeps only
the tail of an over-long prompt; see the 2026-11-23 trace report).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(".env")

HF_URL = "https://router.huggingface.co/v1/chat/completions"
TIMEOUT = httpx.Timeout(900.0, connect=10.0)


@dataclass
class Reply:
    text: str
    tool_calls: list[dict] = field(default_factory=list)  # [{"id","name","arguments"(dict)}]
    input_tokens: int | None = None
    output_tokens: int | None = None
    seconds: float = 0.0
    finish_reason: str | None = None
    truncated_prompt: bool = False
    num_ctx: int | None = None
    raw: Any = None


def _estimate_tokens(messages: list[dict], tools: list[dict] | None) -> int:
    chars = sum(len(m.get("content") or "") for m in messages)
    chars += len(json.dumps(tools)) if tools else 0
    return chars // 3 + 64  # conservative: ~3 chars/token for JSON-ish text


def _ollama_ctx(est_prompt: int, num_predict: int) -> int:
    # Ollama keeps roughly num_ctx/2 of an over-long prompt, so size the window
    # so the whole prompt AND the output fit with room to spare.
    need = 2 * est_prompt + num_predict
    ctx = 8192
    while ctx < need and ctx < 32768:
        ctx *= 2
    return ctx


def chat(model: str, messages: list[dict], *, tools: list[dict] | None = None,
         temperature: float = 0.2, max_tokens: int = 3000, json_mode: bool = False,
         seed: int | None = None, num_ctx: int | None = None) -> Reply:
    backend, _, name = model.partition(":")
    if backend == "ollama":
        return _ollama(name, messages, tools, temperature, max_tokens, json_mode, seed, num_ctx)
    if backend == "hf":
        return _hf(name, messages, tools, temperature, max_tokens, json_mode, seed)
    raise ValueError(f"unknown backend in model spec {model!r}")


def _ollama(name, messages, tools, temperature, max_tokens, json_mode, seed, num_ctx=None) -> Reply:
    from app.config import settings
    est = _estimate_tokens(messages, tools)
    # A fixed num_ctx for multi-turn loops: changing it between turns reloads the model.
    num_ctx = num_ctx or _ollama_ctx(est, max_tokens)
    options = {"num_ctx": num_ctx, "num_predict": max_tokens, "temperature": temperature}
    if seed is not None:
        options["seed"] = seed
    body: dict = {"model": name, "messages": _to_ollama_messages(messages),
                  "stream": False, "options": options}
    if tools:
        body["tools"] = tools
    if json_mode and not tools:
        body["format"] = "json"
    t0 = time.time()
    r = httpx.post(settings.ollama_base_url.rstrip("/") + "/api/chat", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    msg = d.get("message", {})
    calls = [{"id": f"call_{i}", "name": c["function"]["name"],
              "arguments": c["function"].get("arguments") or {}}
             for i, c in enumerate(msg.get("tool_calls") or [])]
    pin = d.get("prompt_eval_count")
    return Reply(
        text=msg.get("content") or "", tool_calls=calls,
        input_tokens=pin, output_tokens=d.get("eval_count"),
        seconds=time.time() - t0, finish_reason=d.get("done_reason"),
        # Truncation signature: Ollama evaluated well under what was sent.
        # (Only meaningful on the first turn; later turns hit the KV cache.)
        truncated_prompt=bool(pin is not None and len(messages) <= 2 and pin < 0.6 * est),
        num_ctx=num_ctx, raw=d,
    )


def _to_ollama_messages(messages):
    out = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):  # assistant turn we recorded in OpenAI shape
            m["tool_calls"] = [{"function": {"name": c["function"]["name"],
                                             "arguments": json.loads(c["function"]["arguments"])}}
                               for c in m["tool_calls"]]
        if m.get("role") != "tool":
            m.pop("tool_call_id", None)
        out.append(m)
    return out


def _hf(name, messages, tools, temperature, max_tokens, json_mode, seed) -> Reply:
    token = os.environ.get("HF_TOKEN") or os.environ["HUGGINGFACE_API_KEY"]
    body: dict = {"model": name, "messages": messages, "temperature": temperature,
                  "max_tokens": max_tokens, "stream": False}
    if tools:
        body["tools"] = tools
    if seed is not None:
        body["seed"] = seed
    if name.startswith("Qwen/Qwen3"):
        # Hybrid-thinking Qwen3.x: hidden reasoning otherwise eats the whole output budget.
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if json_mode and not tools:
        body["response_format"] = {"type": "json_object"}
    t0 = time.time()
    r = httpx.post(HF_URL, json=body, headers={"Authorization": f"Bearer {token}"},
                   timeout=TIMEOUT)
    if r.status_code == 400 and json_mode and "response_format" in body:
        body.pop("response_format")  # not every provider supports it
        r = httpx.post(HF_URL, json=body, headers={"Authorization": f"Bearer {token}"},
                       timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"HF {r.status_code}: {r.text[:500]}")
    d = r.json()
    ch = d["choices"][0]
    msg = ch["message"]
    calls = []
    for c in msg.get("tool_calls") or []:
        args = c["function"].get("arguments") or "{}"
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except json.JSONDecodeError:
            args = {"__unparseable__": args}
        calls.append({"id": c.get("id") or f"call_{len(calls)}",
                      "name": c["function"]["name"], "arguments": args})
    u = d.get("usage") or {}
    return Reply(text=msg.get("content") or "", tool_calls=calls,
                 input_tokens=u.get("prompt_tokens"), output_tokens=u.get("completion_tokens"),
                 seconds=time.time() - t0, finish_reason=ch.get("finish_reason"), raw=d)


def parse_json(text: str) -> dict | None:
    """First top-level JSON object in ``text`` (tolerates ```json fences / prose)."""
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
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None
