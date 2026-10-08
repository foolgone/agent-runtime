"""运行时异常。

约定：所有对外可见的失败都必须能落到一个具体类型上，便于在 API 层映射成稳定的错误码，
而不是把底层 traceback 直接抛给调用方。
"""

from __future__ import annotations


class AgentRuntimeError(Exception):
    """本运行时的所有异常基类。"""


class ToolNotFoundError(AgentRuntimeError):
    """模型请求了一个未注册的工具。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"tool is not registered: {name}")
        self.name = name


class ToolNotAllowedError(AgentRuntimeError):
    """工具已注册，但不在本次调用的允许集合内。"""

    def __init__(self, name: str, allow: frozenset[str]) -> None:
        super().__init__(f"tool {name!r} is not in the allowlist {sorted(allow)}")
        self.name = name
        self.allow = allow


class ToolArgumentError(AgentRuntimeError):
    """工具参数不符合 JSON Schema。"""

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"invalid arguments for tool {name!r}: {detail}")
        self.name = name
        self.detail = detail


class DuplicateToolError(AgentRuntimeError):
    """同名工具被重复注册。允许覆盖会让权限边界变得不可推理。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"tool already registered: {name}")
        self.name = name


class ToolTimeoutError(AgentRuntimeError):
    """工具执行超时。"""

    def __init__(self, name: str, timeout_s: float) -> None:
        super().__init__(f"tool {name!r} timed out after {timeout_s}s")
        self.name = name
        self.timeout_s = timeout_s


class SessionNotFoundError(AgentRuntimeError):
    """会话不存在。"""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


class ProviderError(AgentRuntimeError):
    """上游模型服务返回错误，或流式响应无法解析。"""
