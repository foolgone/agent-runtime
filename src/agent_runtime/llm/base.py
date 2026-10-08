"""模型层的公共类型与 provider 协议。

设计要点：**provider 只负责把统一的输入翻译成某家 API 的请求，再把流式响应翻译回统一事件。**
编排、工具执行、持久化都不在这里。这样换一家模型不改一行编排代码。

流式响应里 tool call 是增量到达的（名字先来、参数字符串一片一片来），
所以这里提供一个 ``ToolCallAssembler``，把碎片拼成完整调用。所有 provider 共用它。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    """模型发起的一次工具调用。``arguments`` 保留原始 JSON 字符串，不在这里解析。"""

    id: str
    name: str
    arguments: str = ""


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """暴露给模型的工具描述。parameters 是 JSON Schema 的 object 片段。"""

    name: str
    description: str
    parameters: Mapping[str, Any]


# --------------------------------------------------------------------------- 流式事件

@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    index: int
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolCallArgumentsDelta:
    index: int
    fragment: str


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Completed:
    stop_reason: str | None = None


StreamEvent = TextDelta | ToolCallStarted | ToolCallArgumentsDelta | Usage | Completed


@runtime_checkable
class Provider(Protocol):
    """模型服务适配层。实现者只需要提供 ``stream``。"""

    name: str

    def stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] = (),
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        ...


@dataclass(slots=True)
class AssembledCompletion:
    """一次模型请求的完整结果，由流式事件归并而来。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    usage: Usage | None = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class ToolCallAssembler:
    """把 ``ToolCallStarted`` / ``ToolCallArgumentsDelta`` 拼成完整 ``ToolCall``。

    按 ``index`` 聚合，因为并行工具调用会交错到达。乱序片段不会报错，
    但缺少 ``ToolCallStarted`` 的片段会被丢弃——宁可少一个调用，也不要凭空造一个。
    """

    def __init__(self) -> None:
        self._pending: dict[int, ToolCall] = {}
        self._order: list[int] = []

    def apply(self, event: StreamEvent) -> None:
        if isinstance(event, ToolCallStarted):
            if event.index not in self._pending:
                self._order.append(event.index)
            self._pending[event.index] = ToolCall(id=event.id, name=event.name)
        elif isinstance(event, ToolCallArgumentsDelta):
            call = self._pending.get(event.index)
            if call is not None:
                call.arguments += event.fragment

    def finish(self) -> list[ToolCall]:
        return [self._pending[i] for i in self._order if i in self._pending]


class StreamCollector:
    """边收边归并。

    需要把增量实时转发出去（SSE、CLI 打字机效果）时用它：拿到事件先 ``apply``，
    再自己决定怎么往外发；流结束后 ``result()`` 拿完整结果。
    只在最后要结果、中间不需要转发时，用下面的 ``collect()``。
    """

    __slots__ = ("_assembler", "_parts", "_usage", "_stop_reason")

    def __init__(self) -> None:
        self._assembler = ToolCallAssembler()
        self._parts: list[str] = []
        self._usage: Usage | None = None
        self._stop_reason: str | None = None

    def apply(self, event: StreamEvent) -> None:
        self._assembler.apply(event)
        if isinstance(event, TextDelta):
            self._parts.append(event.text)
        elif isinstance(event, Usage):
            # Anthropic 先给 input、后给 output，OpenAI 只给一次，所以取各字段最大值而不是覆盖
            previous = self._usage
            self._usage = Usage(
                input_tokens=max(previous.input_tokens if previous else 0, event.input_tokens),
                output_tokens=max(previous.output_tokens if previous else 0, event.output_tokens),
            )
        elif isinstance(event, Completed) and event.stop_reason is not None:
            # 有的实现会在流末尾补一个不带 stop_reason 的收束事件，别让它把真实原因冲掉
            self._stop_reason = event.stop_reason

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def result(self) -> AssembledCompletion:
        return AssembledCompletion(
            text=self.text,
            tool_calls=self._assembler.finish(),
            stop_reason=self._stop_reason,
            usage=self._usage,
        )


async def collect(events: AsyncIterator[StreamEvent]) -> AssembledCompletion:
    """把流式事件收拢成一次完整结果。非流式调用方用这个。"""

    collector = StreamCollector()
    async for event in events:
        collector.apply(event)
    return collector.result()


def build_schemas(specs: Iterable[ToolSchema]) -> list[dict[str, Any]]:
    """OpenAI 风格的工具声明。Anthropic provider 会自行转换。"""

    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": dict(spec.parameters),
            },
        }
        for spec in specs
    ]
