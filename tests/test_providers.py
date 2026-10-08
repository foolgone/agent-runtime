"""provider 适配层：请求编码与 SSE 解析。

SSE 解析是整个仓库最容易悄悄坏掉的地方——上游换个字段名、插一行心跳，
不该让整条流崩掉。这里用 httpx 的 MockTransport 喂真实的响应字节。
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent_runtime.errors import ProviderError
from agent_runtime.llm.anthropic_compat import AnthropicCompatProvider, encode_messages
from agent_runtime.llm.base import (
    ChatMessage,
    Completed,
    TextDelta,
    ToolCall,
    ToolSchema,
    collect,
)
from agent_runtime.llm.openai_compat import OpenAICompatProvider
from agent_runtime.llm.registry import LazyProvider

SSE_HEADERS = {"content-type": "text/event-stream"}


def make_client(body: str, captured: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(200, headers=SSE_HEADERS, content=body.encode("utf-8"))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def openai_chunk(delta: dict, finish_reason: str | None = None) -> str:
    payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return f"data: {json.dumps(payload)}\n\n"


def tool_call_delta(
    index: int, call_id: str | None, name: str | None, arguments: str
) -> dict:
    """一个 tool_call 增量。后续分片只有 index 和 arguments。"""

    raw: dict = {"index": index, "function": {"arguments": arguments}}
    if call_id is not None and name is not None:
        raw.update({"id": call_id, "type": "function"})
        raw["function"]["name"] = name
    return raw


# --------------------------------------------------------------------------- OpenAI

async def test_openai_assembles_text_and_tool_calls() -> None:
    body = (
        ": keep-alive\n\n"
        + openai_chunk({"content": "让我"})
        + openai_chunk({"content": "查一下。"})
        + openai_chunk({"tool_calls": [tool_call_delta(0, "call_1", "echo", '{"te')]})
        + openai_chunk({"tool_calls": [tool_call_delta(0, None, None, 'xt":"hi"}')]})
        + openai_chunk({"tool_calls": [tool_call_delta(1, "call_2", "now", "{}")]})
        + openai_chunk({}, finish_reason="tool_calls")
        + "data: [DONE]\n\n"
        + openai_chunk({"content": "不该出现"})  # [DONE] 之后的内容必须被丢弃
    )
    captured: list[httpx.Request] = []
    provider = OpenAICompatProvider(
        model="m", api_key="k", base_url="http://x/v1", client=make_client(body, captured)
    )

    completion = await collect(provider.stream(messages=[ChatMessage("user", "hi")]))

    assert completion.text == "让我查一下。"
    assert [c.id for c in completion.tool_calls] == ["call_1", "call_2"]
    assert completion.tool_calls[0].arguments == '{"text":"hi"}'
    assert completion.stop_reason == "tool_calls"

    sent = json.loads(captured[0].content)
    assert sent["stream"] is True
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


async def test_openai_sends_system_and_tool_schemas() -> None:
    captured: list[httpx.Request] = []
    provider = OpenAICompatProvider(
        model="m", api_key="k", base_url="http://x/v1", client=make_client("data: [DONE]\n\n", captured)
    )
    tools = [
        ToolSchema(
            name="echo",
            description="回显",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        )
    ]

    await collect(provider.stream(messages=[ChatMessage("user", "hi")], tools=tools, system="你是助手"))

    sent = json.loads(captured[0].content)
    assert sent["messages"][0] == {"role": "system", "content": "你是助手"}
    assert sent["tools"][0]["function"]["name"] == "echo"


async def test_openai_reports_upstream_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatProvider(model="m", api_key="k", base_url="http://x/v1", client=client)

    with pytest.raises(ProviderError, match="401"):
        await collect(provider.stream(messages=[ChatMessage("user", "hi")]))


async def test_openai_tolerates_garbage_lines() -> None:
    body = (
        "event: ping\ndata: not-json\n\n"
        'data: {"choices":[]}\n\n'
        + openai_chunk({"content": "ok"}, finish_reason="stop")
        + "data: [DONE]\n\n"
    )
    provider = OpenAICompatProvider(
        model="m", api_key="k", base_url="http://x/v1", client=make_client(body)
    )

    completion = await collect(provider.stream(messages=[ChatMessage("user", "hi")]))
    assert completion.text == "ok"


async def test_openai_encodes_tool_history() -> None:
    """把上一轮的工具调用和结果发回去时，格式必须对，否则上游会拒收。"""

    captured: list[httpx.Request] = []
    provider = OpenAICompatProvider(
        model="m", api_key="k", base_url="http://x/v1", client=make_client("data: [DONE]\n\n", captured)
    )
    messages = [
        ChatMessage("user", "hi"),
        ChatMessage("assistant", "", tool_calls=[ToolCall(id="call_1", name="echo", arguments='{"text":"hi"}')]),
        ChatMessage("tool", "hi", tool_call_id="call_1"),
    ]

    await collect(provider.stream(messages=messages))

    sent = json.loads(captured[0].content)["messages"]
    assert sent[1]["tool_calls"][0]["function"]["arguments"] == '{"text":"hi"}'
    assert sent[2] == {"role": "tool", "tool_call_id": "call_1", "content": "hi"}


async def test_openai_fills_empty_arguments() -> None:
    """空 arguments 会让部分网关直接 400，统一兜成 {}。"""

    captured: list[httpx.Request] = []
    provider = OpenAICompatProvider(
        model="m", api_key="k", base_url="http://x/v1", client=make_client("data: [DONE]\n\n", captured)
    )
    messages = [
        ChatMessage("assistant", "", tool_calls=[ToolCall(id="c", name="now", arguments="")]),
    ]
    await collect(provider.stream(messages=messages))
    sent = json.loads(captured[0].content)["messages"]
    assert sent[0]["tool_calls"][0]["function"]["arguments"] == "{}"


# --------------------------------------------------------------------------- Anthropic

async def test_anthropic_assembles_text_and_tool_calls() -> None:
    def event(kind: str, data: dict) -> str:
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"

    body = (
        event("message_start", {"message": {"usage": {"input_tokens": 12, "output_tokens": 0}}})
        + event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
        + event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "好"}})
        + event(
            "content_block_start",
            {"index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "echo"}},
        )
        + event("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"te'}})
        + event("content_block_delta", {"index": 1, "delta": {"type": "input_json_delta", "partial_json": 'xt":"hi"}'}})
        + event("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}})
        + event("message_stop", {})
    )
    captured: list[httpx.Request] = []
    provider = AnthropicCompatProvider(
        model="m", api_key="k", base_url="http://x", client=make_client(body, captured)
    )

    completion = await collect(provider.stream(messages=[ChatMessage("user", "hi")], system="sys"))

    assert completion.text == "好"
    assert completion.tool_calls[0].id == "toolu_1"
    assert completion.tool_calls[0].arguments == '{"text":"hi"}'
    # message_stop 不带 stop_reason，不能把 message_delta 给的真实原因冲掉
    assert completion.stop_reason == "tool_use"
    assert completion.usage is not None
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (12, 30)

    sent = json.loads(captured[0].content)
    assert sent["system"] == "sys"
    assert sent["max_tokens"] > 0


async def test_anthropic_groups_consecutive_tool_results() -> None:
    """一条 user 消息里混着 tool_result 和普通文本会被拒收，所以连续结果必须合并成一条。"""

    messages = [
        ChatMessage("user", "hi"),
        ChatMessage(
            "assistant",
            "查一下",
            tool_calls=[
                ToolCall(id="t1", name="echo", arguments='{"text":"a"}'),
                ToolCall(id="t2", name="now", arguments="{}"),
            ],
        ),
        ChatMessage("tool", "a", tool_call_id="t1"),
        ChatMessage("tool", "b", tool_call_id="t2"),
        ChatMessage("user", "继续"),
    ]

    encoded = encode_messages(messages)

    assert [m["role"] for m in encoded] == ["user", "assistant", "user", "user"]
    results = encoded[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result"]
    assert [b["tool_use_id"] for b in results] == ["t1", "t2"]

    blocks = encoded[1]["content"]
    assert [b["type"] for b in blocks] == ["text", "tool_use", "tool_use"]
    # Anthropic 要求 input 是对象，不是字符串
    assert blocks[1]["input"] == {"text": "a"}
    assert blocks[2]["input"] == {}


async def test_anthropic_skips_system_role_in_messages() -> None:
    encoded = encode_messages([ChatMessage("system", "ignored"), ChatMessage("user", "hi")])
    assert encoded == [{"role": "user", "content": "hi"}]


async def test_anthropic_tolerates_unparseable_tool_input() -> None:
    messages = [
        ChatMessage("assistant", "", tool_calls=[ToolCall(id="t1", name="echo", arguments="{oops")]),
    ]
    encoded = encode_messages(messages)
    assert encoded[0]["content"][0]["input"] == {}


async def test_anthropic_reports_stream_error_event() -> None:
    body = 'event: error\ndata: {"type":"error","error":{"message":"overloaded"}}\n\n'
    provider = AnthropicCompatProvider(
        model="m", api_key="k", base_url="http://x", client=make_client(body)
    )

    with pytest.raises(ProviderError, match="overloaded"):
        await collect(provider.stream(messages=[ChatMessage("user", "hi")]))


def _broken_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openai_transport_failure_becomes_provider_error() -> None:
    provider = OpenAICompatProvider(
        model="m", api_key="k", base_url="http://x/v1", client=_broken_client()
    )
    with pytest.raises(ProviderError, match="request failed"):
        await collect(provider.stream(messages=[ChatMessage("user", "hi")]))


async def test_anthropic_transport_failure_becomes_provider_error() -> None:
    provider = AnthropicCompatProvider(
        model="m", api_key="k", base_url="http://x", client=_broken_client()
    )
    with pytest.raises(ProviderError, match="request failed"):
        await collect(provider.stream(messages=[ChatMessage("user", "hi")]))


# --------------------------------------------------------------------------- 懒构造


class _ClosableStub:
    """记账用的假 provider：记被构造、被流过、被关过。"""

    name = "stub"

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self._log.append("built")

    async def stream(self, *, messages, tools=(), system=None):
        self._log.append("streamed")
        yield TextDelta("ok")
        yield Completed(stop_reason="stop")

    async def aclose(self) -> None:
        self._log.append("closed")


async def test_lazy_provider_does_not_build_until_first_stream() -> None:
    log: list[str] = []
    lazy = LazyProvider(lambda: _ClosableStub(log))

    assert log == []
    assert lazy.name == "lazy"
    assert not lazy.built

    await collect(lazy.stream(messages=[ChatMessage("user", "hi")]))

    assert log == ["built", "streamed"]
    assert lazy.built
    assert lazy.name == "stub"


async def test_lazy_provider_builds_only_once_across_streams() -> None:
    """连接池靠复用才成立；构造两次就等于池子白建了。"""

    log: list[str] = []
    lazy = LazyProvider(lambda: _ClosableStub(log))

    for _ in range(3):
        await collect(lazy.stream(messages=[ChatMessage("user", "hi")]))

    assert log.count("built") == 1
    assert log.count("streamed") == 3


async def test_lazy_provider_aclose_is_noop_when_never_built() -> None:
    log: list[str] = []
    lazy = LazyProvider(lambda: _ClosableStub(log))

    await lazy.aclose()

    assert log == []


async def test_lazy_provider_aclose_forwards_to_inner() -> None:
    """关不关得动两次是底下那个 provider 的事，这层只负责转发。

    真实实现自己是幂等的（``OpenAICompatProvider.aclose`` 关完把 ``_client`` 置空），
    所以这里不额外加一层状态去挡。
    """

    log: list[str] = []
    lazy = LazyProvider(lambda: _ClosableStub(log))
    await collect(lazy.stream(messages=[ChatMessage("user", "hi")]))

    await lazy.aclose()

    assert log == ["built", "streamed", "closed"]


async def test_lazy_provider_tolerates_inner_without_aclose() -> None:
    """``aclose`` 不在 Provider 协议里，实现者可以没有。"""

    class NoClose:
        name = "stub"

        async def stream(self, *, messages, tools=(), system=None):
            yield Completed(stop_reason="stop")

    lazy = LazyProvider(NoClose)
    await collect(lazy.stream(messages=[ChatMessage("user", "hi")]))

    await lazy.aclose()  # 不该抛
