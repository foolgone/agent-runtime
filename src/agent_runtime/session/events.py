"""会话事件。

会话状态**只有一种写入方式**：往事件日志末尾追加一条事件。没有可写状态表，
当前状态永远由事件流重放得出。这样做的直接收益是崩溃恢复不需要额外机制——
进程没了，日志还在，重放一遍就回到崩溃前的位置。

事件类型：

===================  ==========================================================
``session/created``  会话建立
``user/message``     用户输入
``assistant/message`` 模型输出（可能带 tool_calls）
``tool/call``         **工具调用发起**。在真正执行之前写入，因此崩溃必然留下痕迹
``tool/result``      工具执行结果
``turn/end``         一轮结束
``turn/error``       一轮异常结束
===================  ==========================================================
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

EventType = Literal[
    "session/created",
    "user/message",
    "assistant/message",
    "tool/call",
    "tool/result",
    "turn/end",
    "turn/error",
]

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """一条已落盘的事件。``seq`` 在单个会话内严格递增，从 1 开始。"""

    seq: int
    type: EventType
    data: Mapping[str, Any] = field(default_factory=dict)
    ts: str = ""

    def to_record(self) -> dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "data": dict(self.data)}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> SessionEvent:
        return cls(
            seq=int(record["seq"]),
            type=record["type"],
            data=record.get("data") or {},
            ts=str(record.get("ts") or ""),
        )


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def validate_session_id(session_id: str) -> str:
    """会话 id 会直接变成文件名，必须先卡死字符集，否则是路径穿越。"""

    if not SESSION_ID_PATTERN.match(session_id):
        raise ValueError(
            f"invalid session id {session_id!r}: must match {SESSION_ID_PATTERN.pattern}"
        )
    return session_id
