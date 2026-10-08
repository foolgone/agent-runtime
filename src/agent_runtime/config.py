"""运行配置。全部来自环境变量，前缀 ``AGENT_``。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}

DEFAULT_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


class ConfigError(RuntimeError):
    """配置缺失或非法。启动时快速失败，不要留到第一次请求才报错。"""


@dataclass(frozen=True, slots=True)
class Settings:
    provider: str
    model: str
    api_key: str
    base_url: str
    data_dir: Path
    max_tool_rounds: int = 8
    request_timeout_s: float = 120.0
    tool_timeout_s: float = 30.0
    # 前端构建产物目录。``None`` 表示不托管前端（只当 API 用，比如跑测试）。
    web_dir: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source: Mapping[str, str] = os.environ if env is None else env

        provider = source.get("AGENT_PROVIDER", "openai").strip().lower()
        if provider not in DEFAULT_BASE_URLS:
            raise ConfigError(
                f"AGENT_PROVIDER must be one of {sorted(DEFAULT_BASE_URLS)}, got {provider!r}"
            )

        model = source.get("AGENT_MODEL", "").strip()
        if not model:
            raise ConfigError("AGENT_MODEL is required")

        api_key = source.get("AGENT_API_KEY", "").strip()
        if not api_key:
            # 允许走 provider 的约定环境变量，方便本地已有 key 的情况
            api_key = source.get(DEFAULT_KEY_ENVS[provider], "").strip()
        if not api_key:
            raise ConfigError(
                f"AGENT_API_KEY is required (or set {DEFAULT_KEY_ENVS[provider]})"
            )

        base_url = source.get("AGENT_BASE_URL", "").strip() or DEFAULT_BASE_URLS[provider]

        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            data_dir=Path(source.get("AGENT_DATA_DIR", ".data")),
            max_tool_rounds=_positive_int(source, "AGENT_MAX_TOOL_ROUNDS", 8),
            request_timeout_s=_positive_float(source, "AGENT_REQUEST_TIMEOUT_S", 120.0),
            tool_timeout_s=_positive_float(source, "AGENT_TOOL_TIMEOUT_S", 30.0),
            web_dir=_web_dir(source),
        )


def _web_dir(env: Mapping[str, str]) -> Path | None:
    """前端构建产物在哪。

    默认取仓库里的 ``web/dist``，路径由本文件的位置推出来 ——
    这样从任意工作目录启动都能找到，不必先 ``cd`` 到仓库根。

    ``AGENT_WEB_DIR`` 显式指定时以它为准；指定成空串表示不托管前端。
    目录不存在不算错误：后端本来就要能单独跑（只当 API、或者前端还没 build）。
    """

    raw = env.get("AGENT_WEB_DIR")
    if raw is not None:
        stripped = raw.strip()
        return Path(stripped) if stripped else None
    # config.py -> agent_runtime -> src -> 仓库根
    return Path(__file__).resolve().parents[2] / "web" / "dist"


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:

    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be > 0, got {value}")
    return value


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be > 0, got {value}")
    return value
