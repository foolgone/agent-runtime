"""模型层：统一消息结构、流式事件、provider 适配。"""

from agent_runtime.llm.base import (
    AssembledCompletion,
    ChatMessage,
    Completed,
    Provider,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallArgumentsDelta,
    ToolCallAssembler,
    ToolCallStarted,
    ToolSchema,
    Usage,
    build_schemas,
    collect,
)
from agent_runtime.llm.registry import available_providers, build_provider, register_provider

__all__ = [
    "AssembledCompletion",
    "ChatMessage",
    "Completed",
    "Provider",
    "StreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolCallArgumentsDelta",
    "ToolCallAssembler",
    "ToolCallStarted",
    "ToolSchema",
    "Usage",
    "available_providers",
    "build_provider",
    "build_schemas",
    "collect",
    "register_provider",
]
