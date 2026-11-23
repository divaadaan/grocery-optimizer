"""``app/agents/llm_chat.py``: request shapes, sizing and error classification.

No network: every call goes through ``httpx.MockTransport``.
"""
import json

import httpx
import pytest

from app.agents import llm_chat
from app.agents.llm_errors import INFRASTRUCTURE, MODEL_OUTPUT, classify
from app.config import Settings


def _settings(**kw):
    base = dict(huggingface_api_key="hf_test", ollama_base_url="http://ollama.test:11434")
    base.update(kw)
    return Settings(**base)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("spec,expected", [
    ("ollama:qwen2.5:7b", ("ollama", "qwen2.5:7b")),
    ("hf:openai/gpt-oss-120b", ("hf", "openai/gpt-oss-120b")),
    ("HF:meta-llama/Llama-3.3-70B-Instruct:groq", ("hf", "meta-llama/Llama-3.3-70B-Instruct:groq")),
])
def test_parse_spec(spec, expected):
    assert llm_chat.parse_spec(spec) == expected


@pytest.mark.parametrize("bad", ["", "qwen2.5:7b", "openai:gpt-4o", "hf:", "ollama"])
def test_parse_spec_rejects_bad_specs(bad):
    with pytest.raises(ValueError):
        llm_chat.parse_spec(bad)


def test_num_ctx_fits_twice_the_prompt_plus_output():
    assert llm_chat.ollama_num_ctx(1000, 2048) == 8192
    assert llm_chat.ollama_num_ctx(4000, 2048) == 16384
    assert llm_chat.ollama_num_ctx(10**6, 2048) == llm_chat.MAX_NUM_CTX


def test_ollama_request_shape_and_reply():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "message": {"content": '{"ok": true}'}, "prompt_eval_count": 150,
            "eval_count": 7, "done_reason": "stop"})

    msgs = [{"role": "user", "content": "x" * 300}]
    reply = llm_chat.chat("ollama:qwen2.5:7b", msgs, temperature=0.1, max_tokens=500,
                          seed=3, settings=_settings(), client=_client(handler))
    assert seen["url"] == "http://ollama.test:11434/api/chat"
    body = seen["body"]
    assert body["model"] == "qwen2.5:7b" and body["stream"] is False
    assert body["format"] == "json"
    assert body["options"] == {"num_ctx": 8192, "num_predict": 500, "temperature": 0.1, "seed": 3}
    assert reply.text == '{"ok": true}'
    assert (reply.input_tokens, reply.output_tokens, reply.finish_reason) == (150, 7, "stop")
    assert reply.truncated_prompt is False
    assert reply.model == "ollama:qwen2.5:7b"


def test_ollama_flags_a_probably_truncated_prompt():
    def handler(request):
        return httpx.Response(200, json={"message": {"content": "{}"}, "prompt_eval_count": 10})

    reply = llm_chat.chat("ollama:m", [{"role": "user", "content": "y" * 3000}],
                          settings=_settings(), client=_client(handler))
    assert reply.truncated_prompt is True


def test_hf_request_shape_and_reply():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2}})

    reply = llm_chat.chat("hf:openai/gpt-oss-120b", [{"role": "user", "content": "hi"}],
                          max_tokens=100, settings=_settings(), client=_client(handler))
    assert seen["url"] == llm_chat.HF_URL
    assert seen["auth"] == "Bearer hf_test"
    assert seen["body"]["model"] == "openai/gpt-oss-120b"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert "chat_template_kwargs" not in seen["body"]
    assert (reply.input_tokens, reply.output_tokens) == (11, 2)


def test_hf_qwen3_disables_thinking():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    llm_chat.chat("hf:Qwen/Qwen3.6-35B-A3B", [{"role": "user", "content": "hi"}],
                  settings=_settings(), client=_client(handler))
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_hf_retries_without_response_format_on_400():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "unsupported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    reply = llm_chat.chat("hf:x/y", [{"role": "user", "content": "hi"}],
                          settings=_settings(), client=_client(handler))
    assert len(bodies) == 2 and "response_format" not in bodies[1]
    assert reply.text == "{}"


def test_hf_without_a_key_is_an_error():
    with pytest.raises(RuntimeError, match="HUGGINGFACE_API_KEY"):
        llm_chat.chat("hf:x/y", [{"role": "user", "content": "hi"}],
                      settings=_settings(huggingface_api_key=None),
                      client=_client(lambda r: httpx.Response(200)))


@pytest.mark.parametrize("code,kind", [
    (401, INFRASTRUCTURE), (402, INFRASTRUCTURE), (403, INFRASTRUCTURE),
    (404, INFRASTRUCTURE), (429, INFRASTRUCTURE), (500, INFRASTRUCTURE),
    (503, INFRASTRUCTURE), (422, MODEL_OUTPUT),
])
def test_http_errors_are_classified(code, kind):
    def handler(request):
        return httpx.Response(code, json={"error": "nope"})

    with pytest.raises(httpx.HTTPStatusError) as err:
        llm_chat.chat("hf:x/y", [{"role": "user", "content": "hi"}], json_mode=False,
                      settings=_settings(), client=_client(handler))
    assert classify(err.value) == kind


def test_connection_refused_is_infrastructure():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(httpx.ConnectError) as err:
        llm_chat.chat("ollama:m", [{"role": "user", "content": "hi"}],
                      settings=_settings(), client=_client(handler))
    assert classify(err.value) == INFRASTRUCTURE


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": {"b": "}"}}\n```', {"a": {"b": "}"}}),
    ('prose {not json} then {"a": 2}', {"a": 2}),
    ('[1, 2]', None),
    ('', None),
    ('{"a": ', None),
])
def test_parse_json_object(text, expected):
    assert llm_chat.parse_json_object(text) == expected


def test_no_agent_module_imports_a_langchain_chat_class_here():
    src = open(llm_chat.__file__, encoding="utf-8").read()
    assert "langchain" not in src
