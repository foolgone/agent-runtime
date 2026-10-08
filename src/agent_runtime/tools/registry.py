"""工具注册表，以及「注册期隔离」的实现。

权限边界在**装配阶段**就定死：调用方通过 ``scoped()`` 派生一个收窄的注册表，
之后的注册、取用、生成 schema、执行全都只看这个收窄后的集合。
运行期不再临时判断权限——运行期判断意味着每个调用点都要记得判断，忘一处就是漏洞。

调用期只保留兜底：``get()`` 对不在集合内的名字抛错，``invoke()`` 会先做参数校验。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError
from jsonschema.exceptions import ValidationError

from agent_runtime.errors import (
    DuplicateToolError,
    ToolArgumentError,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolTimeoutError,
)
from agent_runtime.llm.base import ToolSchema
from agent_runtime.tools.spec import EMPTY_PARAMETERS, ToolResult, ToolSpec


class ToolRegistry:
    """不可变语义的注册表：任何收窄都产生新实例，不修改自身。"""

    __slots__ = ("_specs", "_origin")

    def __init__(
        self,
        specs: Iterable[ToolSpec] = (),
        *,
        _origin: Mapping[str, ToolSpec] | None = None,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {}
        # 收窄前的全集。只为区分「没这个工具」和「有这个工具但不允许」，不参与取用判断。
        self._origin = _origin

        for spec in specs:
            self.register(spec)

    # ------------------------------------------------------------------ 装配

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise DuplicateToolError(spec.name)
        _check_parameters(spec)
        self._specs[spec.name] = spec

    def scoped(self, allow: Iterable[str]) -> ToolRegistry:
        """派生出只含 ``allow`` 的新注册表。

        名字不在当前注册表中的话**立即报错**——装配错了要在启动时炸，不要等到某次线上调用才发现。
        """

        wanted = frozenset(allow)
        missing = sorted(wanted - set(self._specs))
        if missing:
            raise ToolNotFoundError(missing[0])
        return ToolRegistry(
            [self._specs[name] for name in sorted(wanted)],
            _origin=self._specs,
        )

    def only_reads(self) -> ToolRegistry:
        """剔除所有写操作工具。默认装配用的就是它。"""

        return ToolRegistry(
            [spec for spec in self._specs.values() if not spec.is_write],
            _origin=self._specs,
        )

    # ------------------------------------------------------------------ 取用

    @property
    def allow(self) -> frozenset[str] | None:
        """当前允许集合。``None`` 表示未收窄，即全部可用。"""

        return None if self._origin is None else frozenset(self._specs)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            if self._origin is not None and name in self._origin:
                raise ToolNotAllowedError(name, frozenset(self._specs)) from None
            raise ToolNotFoundError(name) from None

    def schemas(self) -> list[ToolSchema]:
        """给模型的工具声明，按名字排序保证同一份配置每次生成的顺序一致。"""

        return [
            ToolSchema(
                name=spec.name,
                description=spec.description,
                parameters=_normalized_parameters(spec),
            )
            for spec in (self._specs[name] for name in sorted(self._specs))
        ]

    # ------------------------------------------------------------------ 执行

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        default_timeout_s: float = 30.0,
    ) -> ToolResult:
        spec = self.get(name)
        validate_arguments(spec, arguments)

        timeout = spec.timeout_s if spec.timeout_s is not None else default_timeout_s
        try:
            return await asyncio.wait_for(spec.handler(arguments), timeout=timeout)
        except TimeoutError as exc:
            raise ToolTimeoutError(name, timeout) from exc


def validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> None:
    validator = Draft202012Validator(_normalized_parameters(spec))
    errors = sorted(validator.iter_errors(dict(arguments)), key=lambda e: list(e.path))
    if errors:
        raise ToolArgumentError(spec.name, _format_error(errors[0]))


def _normalized_parameters(spec: ToolSpec) -> Mapping[str, Any]:
    parameters = spec.parameters or EMPTY_PARAMETERS
    if "type" not in parameters:
        return {"type": "object", **parameters}
    return parameters


def _check_parameters(spec: ToolSpec) -> None:
    try:
        Draft202012Validator.check_schema(_normalized_parameters(spec))
    except SchemaError as exc:
        raise ValueError(f"tool {spec.name!r} has an invalid parameter schema: {exc.message}") from exc


def _format_error(error: ValidationError) -> str:
    location = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"


def build_default_registry(
    extra: Sequence[ToolSpec] = (),
    *,
    notes_dir: Path | None = None,
    include_writes: bool = False,
) -> ToolRegistry:
    """默认装配：内置工具 + 调用方补充。

    默认**剔除写操作**。要放开得显式传 ``include_writes=True``，
    让「谁能写」这件事在装配点就可见，而不是藏在某个运行期判断里。
    """

    from agent_runtime.tools.builtin import builtin_specs

    registry = ToolRegistry()
    for spec in (*builtin_specs(notes_dir), *extra):
        registry.register(spec)
    return registry if include_writes else registry.only_reads()
