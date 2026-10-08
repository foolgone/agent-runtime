"""工具定义。

一个工具 = 名字 + 给模型看的描述 + 参数的 JSON Schema + 实际执行函数。
另外带两个运行时关心的元信息：

- ``side_effect``：只读还是写。写操作默认不进主路径，需要显式加入允许集合才可用。
- ``timeout_s``：单个工具的执行上限，慢工具不该拖垮整轮对话。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

# 和模型侧工具命名约定保持一致：小写字母开头，只含小写字母/数字/下划线
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

SideEffect = Literal["read", "write"]

EMPTY_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行结果。``is_error`` 为真时内容仍会回传给模型，让模型有机会自我纠正。"""

    content: str
    is_error: bool = False
    data: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, content: str, **data: Any) -> ToolResult:
        return cls(content=content, data=data)

    @classmethod
    def error(cls, content: str, **data: Any) -> ToolResult:
        return cls(content=content, is_error=True, data=data)


ToolHandler = Callable[[Mapping[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    parameters: Mapping[str, Any] = field(default_factory=lambda: EMPTY_PARAMETERS)
    side_effect: SideEffect = "read"
    timeout_s: float | None = None

    def __post_init__(self) -> None:
        if not TOOL_NAME_PATTERN.match(self.name):
            raise ValueError(
                f"invalid tool name {self.name!r}: must match {TOOL_NAME_PATTERN.pattern}"
            )
        if self.side_effect not in ("read", "write"):
            raise ValueError(f"invalid side_effect {self.side_effect!r}")
        if self.timeout_s is not None and self.timeout_s <= 0:
            raise ValueError(f"timeout_s must be > 0, got {self.timeout_s}")

    @property
    def is_write(self) -> bool:
        return self.side_effect == "write"
