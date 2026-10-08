"""重放：事件流 -> 当前会话状态。

恢复不需要额外的检查点文件，就把事件读一遍。这里同时也是**崩溃检测**的地方：
``tool/call`` 有记录、``tool/result`` 没记录，说明那次调用「已受理、结果未知」——
要么是执行到一半挂了，要么是执行完了但结果没落盘。两种情况都不能当作没发生过，
必须交给上层决定是重试还是报错。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from agent_runtime.llm.base import ChatMessage, ToolCall
from agent_runtime.session.events import SessionEvent

ERROR_PREFIX = "[tool error] "


@dataclass(slots=True)
class Projection:
    messages: list[ChatMessage] = field(default_factory=list)
    unfinished_calls: list[ToolCall] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_ts: str = ""
    anomalies: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """没有未收尾的工具调用，也没有结构异常。"""

        return not self.unfinished_calls and not self.anomalies


def project(events: Iterable[SessionEvent]) -> Projection:
    result = Projection()
    pending: dict[str, ToolCall] = {}
    order: list[str] = []

    for event in events:
        result.last_ts = event.ts or result.last_ts
        data = event.data

        if event.type == "user/message":
            result.messages.append(ChatMessage(role="user", content=str(data.get("content", ""))))

        elif event.type == "assistant/message":
            calls = [
                ToolCall(
                    id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    arguments=str(raw.get("arguments") or ""),
                )
                for raw in data.get("tool_calls") or []
            ]
            result.messages.append(
                ChatMessage(
                    role="assistant",
                    content=str(data.get("content", "")),
                    tool_calls=calls,
                )
            )
            usage = data.get("usage") or {}
            result.input_tokens += int(usage.get("input_tokens") or 0)
            result.output_tokens += int(usage.get("output_tokens") or 0)

        elif event.type == "tool/call":
            call = ToolCall(
                id=str(data.get("call_id") or ""),
                name=str(data.get("name") or ""),
                arguments=str(data.get("arguments") or ""),
            )
            if call.id in pending:
                result.anomalies.append(f"duplicate tool/call for {call.id}")
                continue
            pending[call.id] = call
            order.append(call.id)

        elif event.type == "tool/result":
            call_id = str(data.get("call_id") or "")
            if call_id not in pending:
                result.anomalies.append(f"tool/result without tool/call: {call_id}")
                continue
            pending.pop(call_id)
            order.remove(call_id)
            content = str(data.get("content", ""))
            if data.get("is_error"):
                # 让模型知道这次失败，它才有机会换个参数重试
                content = ERROR_PREFIX + content
            result.messages.append(
                ChatMessage(role="tool", content=content, tool_call_id=call_id, name=str(data.get("name") or ""))
            )

        elif event.type == "turn/end":
            result.turns += 1

        elif event.type == "turn/error":
            result.turns += 1
            result.anomalies.append(f"turn failed: {data.get('reason') or data.get('message') or 'unknown'}")

        elif event.type == "session/created":
            continue

        else:
            result.anomalies.append(f"unknown event type: {event.type}")

    result.unfinished_calls = [pending[call_id] for call_id in order]
    return result


def replay(events: Sequence[SessionEvent]) -> Projection:
    """``project`` 的别名，语义上更贴近调用方在读什么。"""

    return project(events)
