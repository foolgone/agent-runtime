"""provider 工厂。

新增一家模型服务只需要在这里登记一个构造函数，编排层完全不用改。
"""

from __future__ import annotations

from collections.abc import Callable

from agent_runtime.config import ConfigError, Settings
from agent_runtime.llm.anthropic_compat import AnthropicCompatProvider
from agent_runtime.llm.base import Provider
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
