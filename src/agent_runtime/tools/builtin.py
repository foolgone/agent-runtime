"""内置工具。

这些工具不依赖任何外部服务，作用是把「模型 -> 工具 -> 模型」这条链路跑通，
也顺便演示只读 / 写两类工具在注册表里的区别：``write_note`` 是写操作，
默认装配（``only_reads()``）会把它摘掉，想用得显式放进允许集合。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_runtime.tools.spec import ToolResult, ToolSpec

DEFAULT_NOTES_DIR = Path(".data/notes")


def builtin_specs(notes_dir: Path | None = None) -> list[ToolSpec]:
    root = (notes_dir or DEFAULT_NOTES_DIR).resolve()

    async def echo(arguments: Mapping[str, Any]) -> ToolResult:
        text = str(arguments.get("text", ""))
        return ToolResult.ok(text)

    async def now(arguments: Mapping[str, Any]) -> ToolResult:
        offset = arguments.get("utc_offset_hours", 0)
        moment = datetime.now(UTC) + timedelta(hours=float(offset))
        return ToolResult.ok(moment.isoformat(timespec="seconds"), utc_offset_hours=float(offset))

    async def list_notes(_: Mapping[str, Any]) -> ToolResult:
        if not root.exists():
            return ToolResult.ok("(no notes)")
        names = sorted(p.name for p in root.glob("*.md") if p.is_file())
        return ToolResult.ok("\n".join(names) if names else "(no notes)", count=len(names))

    async def read_note(arguments: Mapping[str, Any]) -> ToolResult:
        target = _resolve_within(root, str(arguments.get("name", "")))
        if target is None:
            return ToolResult.error("note name is invalid or escapes the notes directory")
        if not target.is_file():
            return ToolResult.error(f"note not found: {target.name}")
        return ToolResult.ok(target.read_text(encoding="utf-8"), name=target.name)

    async def write_note(arguments: Mapping[str, Any]) -> ToolResult:
        target = _resolve_within(root, str(arguments.get("name", "")))
        if target is None:
            return ToolResult.error("note name is invalid or escapes the notes directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(arguments.get("content", "")), encoding="utf-8")
        return ToolResult.ok(f"wrote {target.name}", name=target.name)

    return [
        ToolSpec(
            name="echo",
            description="原样返回输入文本。用于验证工具调用链路是否通畅。",
            handler=echo,
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要回显的文本"}},
                "required": ["text"],
            },
        ),
        ToolSpec(
            name="now",
            description="返回当前 UTC 时间，可指定时区偏移。",
            handler=now,
            parameters={
                "type": "object",
                "properties": {
                    "utc_offset_hours": {
                        "type": "number",
                        "description": "相对 UTC 的小时偏移，例如东八区为 8",
                        "default": 0,
                    }
                },
            },
        ),
        ToolSpec(
            name="list_notes",
            description="列出笔记目录下的所有笔记文件名。",
            handler=list_notes,
            parameters={"type": "object", "properties": {}},
            timeout_s=5.0,
        ),
        ToolSpec(
            name="read_note",
            description="读取指定笔记的全文。",
            handler=read_note,
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "笔记文件名"}},
                "required": ["name"],
            },
            timeout_s=5.0,
        ),
        ToolSpec(
            name="write_note",
            description="写入或覆盖一篇笔记。属于写操作，默认不在允许集合内。",
            handler=write_note,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "笔记文件名"},
                    "content": {"type": "string", "description": "笔记内容"},
                },
                "required": ["name", "content"],
            },
            side_effect="write",
            timeout_s=5.0,
        ),
    ]


def _resolve_within(root: Path, name: str) -> Path | None:
    """把用户给的名字解析到 root 之内。任何越界（``../``、绝对路径）一律拒绝。"""

    if not name or name != Path(name).name:
        return None
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        return None
    return target
