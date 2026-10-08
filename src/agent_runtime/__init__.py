"""agent-runtime：可恢复、幂等的 Agent 任务运行时。

分层：

- ``llm``      模型适配。统一消息结构 + 流式事件，换一家模型不改编排。
- ``tools``    工具定义与注册表。权限边界在装配期定死，运行期只兜底。
- ``session``  append-only 事件日志与重放。会话状态完全由事件流推出。
- ``loop``     「模型 -> 工具 -> 模型」循环。
- ``api``      HTTP / SSE 接口。

设计取舍见 ``docs/adr/``。
"""

from agent_runtime.config import ConfigError, Settings
from agent_runtime.errors import AgentRuntimeError

__version__ = "0.1.0"

__all__ = ["AgentRuntimeError", "ConfigError", "Settings", "__version__"]
