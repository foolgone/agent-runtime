"""工具注册表：装配期隔离 + 参数校验。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.errors import (
    DuplicateToolError,
    ToolArgumentError,
    ToolNotAllowedError,
    ToolNotFoundError,
)
from agent_runtime.tools.builtin import builtin_specs
from agent_runtime.tools.registry import ToolRegistry, build_default_registry
from agent_runtime.tools.spec import ToolResult, ToolSpec


async def _ok(_: Mapping[str, Any]) -> ToolResult:
    return ToolResult.ok("ok")


def make_spec(name: str, **kwargs: Any) -> ToolSpec:
    return ToolSpec(name=name, description=f"{name} tool", handler=_ok, **kwargs)


def test_duplicate_registration_rejected() -> None:
    registry = ToolRegistry()
    registry.register(make_spec("alpha"))
    with pytest.raises(DuplicateToolError):
        registry.register(make_spec("alpha"))


@pytest.mark.parametrize("name", ["Alpha", "1alpha", "a", "has space", "with-dash", ""])
def test_bad_names_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="invalid tool name"):
        make_spec(name)


def test_invalid_parameter_schema_rejected() -> None:
    bad = ToolSpec(
        name="bad",
        description="bad",
        handler=_ok,
        parameters={"type": "object", "properties": {"x": {"type": "not-a-type"}}},
    )
    with pytest.raises(ValueError, match="invalid parameter schema"):
        ToolRegistry([bad])


def test_write_tools_excluded_by_default() -> None:
    registry = build_default_registry()
    assert "write_note" not in registry.names()
    assert "read_note" in registry.names()


def test_include_writes_is_explicit() -> None:
    registry = build_default_registry(include_writes=True)
    assert "write_note" in registry.names()


def test_scoped_registry_distinguishes_not_allowed_from_not_found() -> None:
    registry = ToolRegistry([make_spec("alpha"), make_spec("beta")])
    narrow = registry.scoped(["alpha"])

    assert narrow.names() == ["alpha"]
    assert narrow.allow == frozenset({"alpha"})
    assert registry.allow is None

    # 存在但不允许 -> ToolNotAllowedError，可据此上报而不是当成模型幻觉
    with pytest.raises(ToolNotAllowedError):
        narrow.get("beta")
    # 压根不存在 -> ToolNotFoundError
    with pytest.raises(ToolNotFoundError):
        narrow.get("gamma")


def test_scoped_registry_rejects_unknown_names_at_assembly_time() -> None:
    registry = ToolRegistry([make_spec("alpha")])
    with pytest.raises(ToolNotFoundError):
        registry.scoped(["alpha", "ghost"])


def test_scoping_does_not_mutate_original() -> None:
    registry = ToolRegistry([make_spec("alpha"), make_spec("beta")])
    registry.scoped(["alpha"])
    assert registry.names() == ["alpha", "beta"]


def test_schemas_are_sorted_and_normalized() -> None:
    registry = ToolRegistry(
        [
            ToolSpec(
                name="zulu",
                description="z",
                handler=_ok,
                parameters={"properties": {"a": {"type": "string"}}},
            ),
            make_spec("alpha"),
        ]
    )
    schemas = registry.schemas()
    assert [s.name for s in schemas] == ["alpha", "zulu"]
    # 省略 type 的 schema 会被补成 object，否则部分模型侧会拒收
    assert schemas[1].parameters["type"] == "object"


async def test_arguments_validated_before_handler_runs() -> None:
    calls: list[Mapping[str, Any]] = []

    async def handler(arguments: Mapping[str, Any]) -> ToolResult:
        calls.append(arguments)
        return ToolResult.ok("done")

    registry = ToolRegistry(
        [
            ToolSpec(
                name="greet",
                description="greet",
                handler=handler,
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            )
        ]
    )

    with pytest.raises(ToolArgumentError):
        await registry.invoke("greet", {"name": 42})
    assert calls == []

    result = await registry.invoke("greet", {"name": "ada"})
    assert result.content == "done"
    assert calls == [{"name": "ada"}]


async def test_builtin_notes_cannot_escape_directory(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    specs = {spec.name: spec for spec in builtin_specs(notes)}

    escaped = await specs["write_note"].handler({"name": "../escape.md", "content": "x"})
    assert escaped.is_error
    assert not (tmp_path / "escape.md").exists()

    absolute = await specs["read_note"].handler({"name": "C:/Windows/win.ini"})
    assert absolute.is_error
