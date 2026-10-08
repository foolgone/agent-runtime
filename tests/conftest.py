"""测试夹具。

核心是一个脚本化的假 provider：把「模型会怎么回」写成事件列表，
就能在不碰真实模型的前提下把整条链路跑完，包括工具往返和崩溃恢复。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.config import Settings
from agent_runtime.llm.base import (
    ChatMessage,
    Completed,
    StreamEvent,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStarted,
    ToolSchema,
)
from agent_runtime.session.store import EventStore


class ScriptedProvider:
    """按剧本返回流式事件的假 provider。

    剧本用完之后的每一次调用都返回一句固定文本，这样「模型不再要工具了」
    这个收敛条件不需要在每条用例里重复声明。
    """

    name = "scripted"

    def __init__(self, scripts: Sequence[Sequence[StreamEvent]] = ()) -> None:
        self._scripts = [list(script) for script in scripts]
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] = (),
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(
            {"messages": list(messages), "tools": list(tools), "system": system}
        )
        script = self._scripts.pop(0) if self._scripts else default_script()
        for event in script:
            yield event


def default_script(text: str = "收到") -> list[StreamEvent]:
    return [TextDelta(text), Completed(stop_reason="stop")]


def text_script(text: str) -> list[StreamEvent]:
    return [TextDelta(text), Completed(stop_reason="stop")]


def tool_call_script(
    call_id: str,
    name: str,
    arguments: str,
    *,
    preamble: str = "",
    index: int = 0,
) -> list[StreamEvent]:
    """一段「模型发起工具调用」的流。参数故意切成两片，验证拼接。"""

    events: list[StreamEvent] = []
    if preamble:
        events.append(TextDelta(preamble))
    events.append(ToolCallStarted(index=index, id=call_id, name=name))
    midpoint = max(1, len(arguments) // 2)
    events.append(ToolCallArgumentsDelta(index=index, fragment=arguments[:midpoint]))
    events.append(ToolCallArgumentsDelta(index=index, fragment=arguments[midpoint:]))
    events.append(Completed(stop_reason="tool_calls"))
    return events


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        provider="openai",
        model="test-model",
        api_key="test-key",
        base_url="http://localhost:9/v1",
        data_dir=tmp_path / "data",
        # 显式不托管前端：否则会挂上仓库里真实的 web/dist，
        # 测试结果就取决于本地有没有 build 过。
        web_dir=None,
    )


@pytest.fixture
def store(settings: Settings) -> EventStore:
    return EventStore(settings.data_dir / "sessions")
