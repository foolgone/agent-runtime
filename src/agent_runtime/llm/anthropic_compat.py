"""Anthropic Messages API provider。

和 OpenAI 的差异都收敛在这个文件里：
- system 是顶层参数，不是 messages 里的第一条
- 工具声明是扁平结构（``input_schema``），不是 ``function`` 包一层
- 工具结果必须作为 ``user`` 消息里的 ``tool_result`` block 回传，且连续的多个结果要合进同一条消息
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from agent_runtime.errors import ProviderError
from agent_runtime.llm.base import (
    ChatMessage,
    Completed,
    StreamEvent,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStarted,
    ToolSchema,
    Usage,
)

_SSE_DATA_PREFIX = "data:"
_MAX_TOKENS = 4096


class AnthropicCompatProvider:
    """``provider.name == "anthropic"``。"""

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout_s: float = 120.0,
        max_tokens: int = _MAX_TOKENS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._timeout = timeout_s
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] = (),
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": encode_messages(messages),
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = encode_tools(tools)

        client = self._ensure_client()
        url = f"{self._base_url}/v1/messages"

        try:
            async with client.stream("POST", url, json=payload, headers=self._headers) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise ProviderError(
                        f"anthropic upstream returned {response.status_code}: {body[:500]}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith(_SSE_DATA_PREFIX):
                        continue
                    data = line[len(_SSE_DATA_PREFIX):].strip()
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("type") == "error":
                        raise ProviderError(f"anthropic stream error: {chunk.get('error')}")
                    for event in _to_stream_events(chunk):
                        yield event
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc


def encode_tools(tools: Sequence[ToolSchema]) -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": dict(spec.parameters),
        }
        for spec in tools
    ]


def encode_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """把统一消息结构翻译成 Anthropic 的 messages 数组。

    关键点：连续的 ``tool`` 消息必须合并进同一条 ``user`` 消息的多个 ``tool_result`` block。
    一条 user 消息里混着 tool_result 和普通文本，部分版本会直接拒收，所以这里严格分开。
    """

    encoded: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            encoded.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        if message.role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
            )
            continue

        flush_results()

        if message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": _parse_arguments(call.arguments),
                    }
                )
            encoded.append({"role": "assistant", "content": blocks})
            continue

        # system 角色在 Anthropic 里属于顶层参数，这里跳过以免请求被拒
        if message.role == "system":
            continue

        encoded.append({"role": message.role, "content": message.content})

    flush_results()
    return encoded


def _parse_arguments(raw: str) -> dict[str, Any]:
    """Anthropic 要求 tool_use.input 是对象，不是字符串。解析失败就退回空对象。"""

    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_stream_events(chunk: dict[str, Any]) -> list[StreamEvent]:
    kind = chunk.get("type")
    events: list[StreamEvent] = []

    if kind == "message_start":
        usage = (chunk.get("message") or {}).get("usage") or {}
        events.append(
            Usage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            )
        )
        return events

    if kind == "content_block_start":
        block = chunk.get("content_block") or {}
        if block.get("type") == "tool_use":
            events.append(
                ToolCallStarted(
                    index=int(chunk.get("index") or 0),
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                )
            )
        return events

    if kind == "content_block_delta":
        delta = chunk.get("delta") or {}
        delta_type = delta.get("type")
        index = int(chunk.get("index") or 0)
        if delta_type == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                events.append(TextDelta(text))
        elif delta_type == "input_json_delta":
            fragment = delta.get("partial_json")
            if isinstance(fragment, str) and fragment:
                events.append(ToolCallArgumentsDelta(index=index, fragment=fragment))
        return events

    if kind == "message_delta":
        delta = chunk.get("delta") or {}
        usage = chunk.get("usage") or {}
        if usage.get("output_tokens") is not None:
            events.append(Usage(input_tokens=0, output_tokens=int(usage["output_tokens"])))
        stop_reason = delta.get("stop_reason")
        if stop_reason:
            events.append(Completed(stop_reason=str(stop_reason)))
        return events

    if kind == "message_stop":
        events.append(Completed(stop_reason=None))
        return events

    return events
