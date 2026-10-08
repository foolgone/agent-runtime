"""OpenAI ``/chat/completions`` 兼容 provider。

凡是 OpenAI 兼容协议的网关（官方、Azure 风格代理、各家自建网关）都能用这一个。
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
    build_schemas,
)

_SSE_DATA_PREFIX = "data:"
_DONE_SENTINEL = "[DONE]"


class OpenAICompatProvider:
    """``provider.name == "openai"``。"""

    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._headers = {
            "Authorization": f"Bearer {api_key}",
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
            "messages": encode_messages(messages, system),
            "stream": True,
        }
        if tools:
            payload["tools"] = build_schemas(tools)

        client = self._ensure_client()
        url = f"{self._base_url}/chat/completions"

        try:
            async with client.stream("POST", url, json=payload, headers=self._headers) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise ProviderError(
                        f"openai-compatible upstream returned {response.status_code}: {body[:500]}"
                    )
                async for line in response.aiter_lines():
                    raw = _sse_payload(line)
                    if raw is None:
                        continue
                    if raw == _DONE_SENTINEL:
                        break
                    chunk = _loads(raw)
                    if chunk is None:
                        continue
                    for stream_event in _to_stream_events(chunk):
                        yield stream_event
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai-compatible request failed: {exc}") from exc


def encode_messages(messages: Sequence[ChatMessage], system: str | None) -> list[dict[str, Any]]:
    """把统一消息结构翻译成 OpenAI 的 messages 数组。"""

    encoded: list[dict[str, Any]] = []
    if system:
        encoded.append({"role": "system", "content": system})

    for message in messages:
        if message.role == "tool":
            encoded.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": message.content,
                }
            )
            continue

        if message.role == "assistant" and message.tool_calls:
            encoded.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                # 空字符串会让部分网关拒绝请求，统一兜一个合法 JSON
                                "arguments": call.arguments or "{}",
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )
            continue

        encoded.append({"role": message.role, "content": message.content})

    return encoded


def _sse_payload(line: str) -> str | None:
    """取出 ``data:`` 后面的字符串。不是数据行就返回 None。"""

    if not line or not line.startswith(_SSE_DATA_PREFIX):
        return None
    payload = line[len(_SSE_DATA_PREFIX):].strip()
    return payload or None


def _loads(payload: str) -> dict[str, Any] | None:
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError:
        # 网关偶尔会插入心跳或非 JSON 行，跳过即可，不要中断整条流
        return None
    return chunk if isinstance(chunk, dict) else None


def _to_stream_events(chunk: dict[str, Any]) -> list[StreamEvent]:
    events: list[StreamEvent] = []

    usage = chunk.get("usage")
    if isinstance(usage, dict):
        events.append(
            Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            )
        )

    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return events

    choice = choices[0]
    delta = choice.get("delta") or {}

    content = delta.get("content")
    if isinstance(content, str) and content:
        events.append(TextDelta(content))

    for raw in delta.get("tool_calls") or []:
        index = int(raw.get("index") or 0)
        function = raw.get("function") or {}
        if raw.get("id") or function.get("name"):
            events.append(
                ToolCallStarted(
                    index=index,
                    id=str(raw.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                )
            )
        fragment = function.get("arguments")
        if isinstance(fragment, str) and fragment:
            events.append(ToolCallArgumentsDelta(index=index, fragment=fragment))

    finish_reason = choice.get("finish_reason")
    if finish_reason:
        events.append(Completed(stop_reason=str(finish_reason)))

    return events
