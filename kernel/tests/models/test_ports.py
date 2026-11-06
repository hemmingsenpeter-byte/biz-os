import asyncio
import json

import anthropic
import httpx
import httpx2
import respx
from biz_kernel.contracts.suite import check_model_port
from biz_port_model_claude import ClaudeModel
from biz_port_model_ollama import OllamaModel

S = {
    "type": "object",
    "required": ["label", "score"],
    "properties": {"label": {"type": "string"}, "score": {"type": "number"}},
}


@respx.mock
def test_ollama_contract_and_request():
    route = respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {"content": '{"label": "a", "score": 1}'},
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        )
    )
    emb = respx.post("http://ollama.test/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})
    )
    m = OllamaModel("http://ollama.test", "m")
    asyncio.run(check_model_port(m))
    body = json.loads(route.calls[0].request.content)
    assert body["format"] == S and body["stream"] is False
    assert body["options"]["temperature"] <= 0.2
    assert m.last_usage.input_tokens == 7 and m.last_usage.cost_usd == 0
    assert asyncio.run(m.embed(["a", "b"])) == [[0.1, 0.2], [0.3, 0.4]]
    assert json.loads(emb.calls[0].request.content)["input"] == ["a", "b"]


def test_claude_contract_cache_and_cost():
    # The anthropic SDK 2.x runs on httpx2, which respx cannot patch: use its MockTransport.
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "emit_result",
                        "input": {"label": "a", "score": 2},
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 0,
                },
            },
        )

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    m = ClaudeModel(
        client=anthropic.AsyncAnthropic(api_key="test", max_retries=0, http_client=http)
    )
    asyncio.run(check_model_port(m))
    body = json.loads(seen[0].content)
    assert body["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert body["tools"][0]["input_schema"] == S
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    u = m.last_usage
    assert (u.input_tokens, u.cache_read_tokens) == (1000, 2000)
    assert float(u.cost_usd) > 0
