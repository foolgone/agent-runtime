"""延迟可控的假 provider。

**这个文件是整份压测可信度的关键。**

真实模型调用要走网络，耗时由上游决定，比运行时自身的开销高一到两个数量级。
拿真模型压测，量出来的是别人的网络延迟，跟自己写的代码没关系。

所以这里把模型延迟变成可控参数：固定成某个值后，
端到端延迟减去模型延迟，剩下的才是运行时开销。
报告里必须写清这一点——不然「P95 轮次延迟 40ms」会被误读成「这运行时很慢」，
实际上是 stub 的延迟。

工具往返次数也可控，用来观察「一轮里事件数翻倍」对写入成本的影响。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from agent_runtime.llm.base import (
    ChatMessage,
    Completed,
    StreamEvent,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStarted,
    ToolSchema,
)

TOOL_NAME = "echo"
TOOL_ARGUMENTS = '{"text":"bench"}'


class StubProvider:
    """按固定延迟回固定剧本。不联网、不花钱、结果可复现。"""

    name = "stub"

    def __init__(self, *, latency_ms: float = 0.0, tool_rounds: int = 1) -> None:
        self.latency_ms = latency_ms
        self.tool_rounds = tool_rounds
        self.calls = 0

    async def stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] = (),
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000.0)

        if self._rounds_done(messages) < self.tool_rounds:
            index = self._rounds_done(messages)
            yield ToolCallStarted(index=0, id=f"call_{index}", name=TOOL_NAME)
            # 参数分两片发，顺带把组装器也一起压到
            yield ToolCallArgumentsDelta(index=0, fragment=TOOL_ARGUMENTS[:8])
            yield ToolCallArgumentsDelta(index=0, fragment=TOOL_ARGUMENTS[8:])
            yield Completed(stop_reason="tool_calls")
            return

        yield TextDelta("完成。")
        yield Completed(stop_reason="stop")

    @staticmethod
    def _rounds_done(messages: Sequence[ChatMessage]) -> int:
        """数本轮已经发生过几次工具调用。往前扫到最近一条 user 消息为止。"""

        done = 0
        for message in reversed(messages):
            if message.role == "user":
                break
            if message.role == "assistant" and message.tool_calls:
                done += 1
        return done
