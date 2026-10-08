"""「模型 -> 工具 -> 模型」循环。

三条不变式，整个文件都在维护它们：

1. **先记账，后执行。** 每个工具调用在真正执行之前就先写 ``tool/call``。
   进程在工具执行中途死掉时，日志里会留下一条没有结果的调用记录，
   恢复时能识别出「已受理、结果未知」，而不是当它没发生过。
2. **状态只来自日志。** 每一轮都从事件流重放消息列表，而不是在内存里累积。
   于是「正常跑」和「崩溃后恢复」走的是同一条代码路径，恢复逻辑不会被测试遗忘。
   代价是单轮内重放 n 次，n 是一轮里的事件数——几十条量级，先不为它做缓存。
3. **工具有上限。** 一轮最多 ``max_tool_rounds`` 次往返，用光了就记 ``turn/error`` 收尾，
   避免模型陷入「调用 -> 报错 -> 再调用」的死循环。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.errors import (
    AgentRuntimeError,
    ToolArgumentError,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from agent_runtime.llm.base import ChatMessage, Provider, StreamCollector, TextDelta, ToolCall
from agent_runtime.session.projection import project
from agent_runtime.session.store import EventStore
from agent_runtime.tools.registry import ToolRegistry

DEFAULT_SYSTEM_PROMPT = (
    "你是一个可以调用工具的助手。需要外部信息或需要产生副作用时调用工具，"
    "不要凭空猜测工具的执行结果。工具返回错误时，先判断能否通过修正参数解决，"
    "不能解决就把情况说明给用户。"
)


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolFinished:
    call_id: str
    name: str
    is_error: bool
    content: str
    duration_ms: int


@dataclass(slots=True)
class TurnResult:
    """一轮的最终结果。``unfinished`` 非空表示这一轮没能干净收尾。"""

    text: str = ""
    rounds: int = 0
    tool_calls: int = 0
    stop_reason: str | None = None
    unfinished: list[ToolCall] = field(default_factory=list)


LoopEvent = TextChunk | ToolStarted | ToolFinished | TurnResult


class AgentRunner:
    def __init__(
        self,
        *,
        provider: Provider,
        store: EventStore,
        registry: ToolRegistry,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tool_rounds: int = 8,
        tool_timeout_s: float = 30.0,
    ) -> None:
        self._provider = provider
        self._store = store
        self._registry = registry
        self._system = system_prompt
        self._max_tool_rounds = max_tool_rounds
        self._tool_timeout_s = tool_timeout_s

    @property
    def tools(self) -> list[str]:
        """本次装配实际可用的工具名。写操作工具不在其中。"""

        return self._registry.names()

    # ------------------------------------------------------------------ 会话

    async def ensure_session(self, session_id: str) -> bool:
        """会话不存在就建一个，返回是否新建。

        判定依据是「这个会话此前没有任何事件」，而不是「文件是否存在」——
        空文件（建了但还没写东西）和不存在，对调用方是同一件事。
        """

        if await self._store.read(session_id):
            return False
        await self._store.append(
            session_id, "session/created", {"tools": self._registry.names()}
        )
        return True

    async def history(self, session_id: str) -> list[ChatMessage]:
        return project(await self._store.read(session_id)).messages

    async def recover(self, session_id: str) -> TurnResult:
        """读取会话现状，报告是否有「已受理、结果未知」的工具调用。

        这个方法不修复任何东西，只如实汇报。要不要重试由调用方决定——
        重试一个可能有副作用的写操作，不该由运行时替用户拿主意。
        """

        projection = project(await self._store.read(session_id))
        last_assistant = next(
            (m for m in reversed(projection.messages) if m.role == "assistant"), None
        )
        return TurnResult(
            text=last_assistant.content if last_assistant else "",
            rounds=projection.turns,
            tool_calls=sum(1 for m in projection.messages if m.role == "tool"),
            stop_reason="interrupted" if projection.unfinished_calls else "clean",
            unfinished=list(projection.unfinished_calls),
        )

    # ------------------------------------------------------------------ 主循环

    async def run_turn(self, session_id: str, user_input: str) -> AsyncIterator[LoopEvent]:
        await self._store.append(session_id, "user/message", {"content": user_input})

        result = TurnResult()
        spoken: list[str] = []

        for round_index in range(1, self._max_tool_rounds + 1):
            result.rounds = round_index

            # 每轮都从日志重放，不在内存里攒消息。这样恢复路径和正常路径是同一段代码。
            projection = project(await self._store.read(session_id))

            collector = StreamCollector()
            async for event in self._provider.stream(
                messages=projection.messages,
                tools=self._registry.schemas(),
                system=self._system,
            ):
                collector.apply(event)
                if isinstance(event, TextDelta):
                    yield TextChunk(event.text)

            completion = collector.result()
            usage = completion.usage
            usage_record: dict[str, int] = (
                {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
                if usage
                else {}
            )

            if not completion.has_tool_calls:
                await self._store.append(
                    session_id,
                    "assistant/message",
                    {"content": completion.text, "usage": usage_record},
                )
                stop_reason = completion.stop_reason or "stop"
                await self._store.append(session_id, "turn/end", {"stop_reason": stop_reason})

                # 收尾那一轮的文本才是回答。它可能为空（模型只发工具调用不发话），
                # 这时才退回前面几轮攒下的开场白。
                result.text = completion.text or "".join(spoken)
                result.stop_reason = stop_reason
                yield result
                return

            await self._store.append(
                session_id,
                "assistant/message",
                {
                    "content": completion.text,
                    "usage": usage_record,
                    "tool_calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in completion.tool_calls
                    ],
                },
            )
            if completion.text:
                spoken.append(completion.text)

            for call in completion.tool_calls:
                async for call_event in self._execute(session_id, call):
                    if isinstance(call_event, ToolFinished):
                        result.tool_calls += 1
                    yield call_event

        # 往返次数用光还没收敛
        await self._store.append(
            session_id,
            "turn/error",
            {"reason": "max_tool_rounds_exceeded", "max_tool_rounds": self._max_tool_rounds},
        )
        projection = project(await self._store.read(session_id))
        result.text = "".join(spoken)
        result.stop_reason = "max_tool_rounds_exceeded"
        result.unfinished = list(projection.unfinished_calls)
        yield result

    # ------------------------------------------------------------------ 单次工具调用

    async def _execute(self, session_id: str, call: ToolCall) -> AsyncIterator[LoopEvent]:
        # 先记账再执行。执行中途崩溃时，日志里会留下这条没有结果的调用。
        await self._store.append(
            session_id,
            "tool/call",
            {"call_id": call.id, "name": call.name, "arguments": call.arguments},
        )
        yield ToolStarted(call_id=call.id, name=call.name, arguments=call.arguments)

        started = time.perf_counter()
        content, is_error = await self._invoke(call)
        duration_ms = int((time.perf_counter() - started) * 1000)

        await self._store.append(
            session_id,
            "tool/result",
            {
                "call_id": call.id,
                "name": call.name,
                "content": content,
                "is_error": is_error,
                "duration_ms": duration_ms,
            },
        )
        yield ToolFinished(
            call_id=call.id,
            name=call.name,
            is_error=is_error,
            content=content,
            duration_ms=duration_ms,
        )

    async def _invoke(self, call: ToolCall) -> tuple[str, bool]:
        """执行工具。工具层的失败一律变成 ``(内容, True)`` 回传给模型，不往上抛。

        工具报错对模型是有用信息——参数写错了它自己能改。真正无法继续的失败
        （比如上游模型服务挂了）发生在调用模型那一层，不在这里吞。
        """

        try:
            arguments = _parse_arguments(call.arguments)
        except ValueError as exc:
            return f"arguments are not valid JSON: {exc}", True

        try:
            result = await self._registry.invoke(
                call.name, arguments, default_timeout_s=self._tool_timeout_s
            )
        except ToolNotFoundError:
            return f"unknown tool: {call.name}", True
        except ToolNotAllowedError as exc:
            return f"tool not allowed in this session: {exc.name}", True
        except ToolArgumentError as exc:
            return exc.detail, True
        except ToolTimeoutError as exc:
            return f"tool timed out after {exc.timeout_s}s", True
        except AgentRuntimeError as exc:
            return str(exc), True
        except Exception as exc:  # noqa: BLE001 - 工具是外部代码，不能让它的异常掀翻整轮对话
            return f"tool raised {type(exc).__name__}: {exc}", True

        return result.content, result.is_error


def _parse_arguments(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ValueError("arguments must be a JSON object")
    return parsed


__all__ = [
    "AgentRunner",
    "LoopEvent",
    "TextChunk",
    "ToolFinished",
    "ToolStarted",
    "TurnResult",
]
