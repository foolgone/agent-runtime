"""provider 工厂。

新增一家模型服务只需要在这里登记一个构造函数，编排层完全不用改。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence

from agent_runtime.config import ConfigError, Settings
from agent_runtime.llm.anthropic_compat import AnthropicCompatProvider
from agent_runtime.llm.base import ChatMessage, Provider, StreamEvent, ToolSchema
from agent_runtime.llm.openai_compat import OpenAICompatProvider

ProviderFactory = Callable[[Settings], Provider]

_FACTORIES: dict[str, ProviderFactory] = {
    "openai": lambda s: OpenAICompatProvider(
        model=s.model,
        api_key=s.api_key,
        base_url=s.base_url,
        timeout_s=s.request_timeout_s,
    ),
    "anthropic": lambda s: AnthropicCompatProvider(
        model=s.model,
        api_key=s.api_key,
        base_url=s.base_url,
        timeout_s=s.request_timeout_s,
    ),
}


def available_providers() -> list[str]:
    return sorted(_FACTORIES)


def register_provider(name: str, factory: ProviderFactory) -> None:
    """供测试与第三方扩展注入自定义 provider。"""

    _FACTORIES[name] = factory


def build_provider(settings: Settings) -> Provider:
    try:
        factory = _FACTORIES[settings.provider]
    except KeyError as exc:
        raise ConfigError(
            f"unknown provider {settings.provider!r}, available: {available_providers()}"
        ) from exc
    return factory(settings)


class LazyProvider:
    """延迟到第一次真正调用模型时才构造底下的 provider。

    存在的理由是服务端有两种请求：要模型的（跑一轮对话）和不要的
    （查历史、查恢复状态）。不要模型的那种如果顺带构造了 provider，
    就会跟着建一个 ``httpx.AsyncClient``——一个连接池，连带一次 DNS 和 TLS——
    然后因为没人关它而留在那儿。只读请求本来跟模型没关系，
    也不该因为模型配置写错就跟着失败。

    构造过程不加锁：``_factory()`` 是同步调用，检查与赋值之间没有 await，
    在事件循环里是原子的，跟 ``_ensure_client`` 同理。
    """

    def __init__(self, factory: Callable[[], Provider]) -> None:
        self._factory = factory
        self._inner: Provider | None = None

    @property
    def built(self) -> bool:
        return self._inner is not None

    @property
    def name(self) -> str:
        return self._inner.name if self._inner is not None else "lazy"

    async def stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] = (),
        system: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if self._inner is None:
            self._inner = self._factory()
        async for event in self._inner.stream(messages=messages, tools=tools, system=system):
            yield event

    async def aclose(self) -> None:
        # 没构造过就没有连接可关。``aclose`` 不在 Provider 协议里（见 ``base.Provider``），
        # 所以底下的实现可能有也可能没有。
        if self._inner is None:
            return
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()
