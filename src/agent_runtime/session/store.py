"""事件存储：每会话一个 append-only JSONL 文件。

三个必须做对的地方：

1. **序号串行分配**。并发 append 下两个协程同时读到最后一条 seq 就会撞号，所以按会话加锁。
2. **写完要 fsync**。只 flush 到用户态缓冲的话，进程被 kill 掉那几行就没了；
   而「工具已经执行但调用记录丢了」正是恢复时最怕的状态。
3. **读的时候容忍坏尾行**。崩溃可能正好落在写一半，最后一行是残的，跳过它继续，
   不要因为一行读不动就判定整个会话损坏。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_runtime.errors import SessionNotFoundError
from agent_runtime.session.events import (
    EventType,
    SessionEvent,
    utc_now,
    validate_session_id,
)


class EventStore:
    """``<root>/<session_id>.jsonl``，一行一条事件。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._locks: dict[str, asyncio.Lock] = {}
        self._seq_cache: dict[str, int] = {}

    # ------------------------------------------------------------------ 路径

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{validate_session_id(session_id)}.jsonl"

    def list_sessions(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.jsonl") if p.is_file())

    # ------------------------------------------------------------------ 写

    async def append(
        self,
        session_id: str,
        event_type: EventType,
        data: Mapping[str, Any] | None = None,
    ) -> SessionEvent:
        session_id = validate_session_id(session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())

        async with lock:
            seq = await self._next_seq(session_id)
            event = SessionEvent(seq=seq, type=event_type, data=dict(data or {}), ts=utc_now())
            line = json.dumps(event.to_record(), ensure_ascii=False, separators=(",", ":"))
            await asyncio.to_thread(self._append_line, self.path_for(session_id), line)
            self._seq_cache[session_id] = seq
            return event

    def _append_line(self, path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    async def _next_seq(self, session_id: str) -> int:
        cached = self._seq_cache.get(session_id)
        if cached is not None:
            return cached + 1
        events = await self.read(session_id)
        return (events[-1].seq if events else 0) + 1

    # ------------------------------------------------------------------ 读

    async def read(self, session_id: str) -> list[SessionEvent]:
        session_id = validate_session_id(session_id)
        path = self.path_for(session_id)
        if not path.exists():
            return []
        return await asyncio.to_thread(self._read_lines, path)

    def _read_lines(self, path: Path) -> list[SessionEvent]:
        events: list[SessionEvent] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(SessionEvent.from_record(json.loads(line)))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    # 只可能是崩溃留下的残行。丢掉它，后面的都是完整的。
                    continue
        return events

    async def exists(self, session_id: str) -> bool:
        return self.path_for(session_id).exists()

    async def require(self, session_id: str) -> list[SessionEvent]:
        events = await self.read(session_id)
        if not events:
            raise SessionNotFoundError(session_id)
        return events
