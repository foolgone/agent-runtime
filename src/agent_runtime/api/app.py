"""HTTP 接口层。

一轮对话用 SSE 推出去，因为模型是流式返回的，攒完再发等于把首字延迟拉到最长。
事件名和 ``loop.runner`` 的 ``LoopEvent`` 一一对应，前端不需要猜。

接口本身很薄：解析请求 -> 调 runner -> 把事件翻译成 SSE。业务逻辑一律不在这里。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_runtime.config import Settings
from agent_runtime.errors import (
    AgentRuntimeError,
    ProviderError,
    SessionNotFoundError,
)
from agent_runtime.llm.registry import LazyProvider, build_provider
from agent_runtime.loop.runner import (
    AgentRunner,
    LoopEvent,
    TextChunk,
    ToolFinished,
    ToolStarted,
    TurnResult,
)
from agent_runtime.session.store import EventStore
from agent_runtime.tools.registry import ToolRegistry, build_default_registry

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关掉 nginx 一类反代的缓冲，否则 SSE 会被攒成一坨再吐出来
    "X-Accel-Buffering": "no",
}


class CreateSessionResponse(BaseModel):
    session_id: str
    tools: list[str]


class TurnRequest(BaseModel):
    input: str = Field(min_length=1, max_length=32_000)


class MessageOut(BaseModel):
    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class HistoryResponse(BaseModel):
    session_id: str
    messages: list[MessageOut]


class SessionListResponse(BaseModel):
    sessions: list[str]


class RecoveryResponse(BaseModel):
    session_id: str
    stop_reason: str
    rounds: int
    tool_calls: int
    unfinished_calls: list[dict[str, str]]


def create_app(
    settings: Settings,
    *,
    provider: Any | None = None,
    registry: ToolRegistry | None = None,
    notes_dir: Path | None = None,
) -> FastAPI:
    """构造应用。

    ``provider`` 可以注入，测试用假的 provider 就能跑完整条链路，不需要真实模型。
    """

    @asynccontextmanager
    async def lifespan(active: FastAPI) -> AsyncIterator[None]:
        yield
        # provider 是整个进程共用的那一个，谁建谁关。关在这里而不是每个请求里，
        # 否则每轮对话都会把连接池拆掉重建，而连接池的意义就是别重建。
        #
        # ``aclose`` 不在 Provider 协议里（实现者只需提供 stream），所以是可选的：
        # 假 provider 通常没有连接可关。有就关，没有就算了。
        close = getattr(active.state.provider, "aclose", None)
        if close is not None:
            await close()

    app = FastAPI(title="agent-runtime", version="0.1.0", lifespan=lifespan)

    store = EventStore(settings.data_dir / "sessions")
    tools = registry if registry is not None else build_default_registry(notes_dir=notes_dir)

    # 全应用一个 provider，不是每个请求一个。
    # 每个请求建一个 —— 这里是之前的样子 —— 意味着每轮对话都要重新握手一次，
    # 且建出来的 httpx.AsyncClient 没人关。
    # 包一层 LazyProvider：只读接口不该为了查一条历史去建连接池，
    # 也不该因为模型配置写错而跟着失败。
    app.state.settings = settings
    app.state.store = store
    app.state.registry = tools
    app.state.provider = (
        provider if provider is not None else LazyProvider(lambda: build_provider(settings))
    )

    def make_runner() -> AgentRunner:
        return AgentRunner(
            provider=app.state.provider,
            store=store,
            registry=tools,
            max_tool_rounds=settings.max_tool_rounds,
            tool_timeout_s=settings.tool_timeout_s,
        )

    app.state.runner_factory = make_runner

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "provider": settings.provider,
            "model": settings.model,
            "tools": tools.names(),
        }

    @app.post("/sessions", response_model=CreateSessionResponse)
    async def create_session() -> CreateSessionResponse:
        session_id = uuid.uuid4().hex[:16]
        await make_runner().ensure_session(session_id)
        return CreateSessionResponse(session_id=session_id, tools=tools.names())

    @app.get("/sessions", response_model=SessionListResponse)
    async def list_sessions() -> SessionListResponse:
        """列出磁盘上已有的会话。

        直接来自目录扫描，不走内存索引——内存里那份重启就没了，
        而这个接口的意义正是「进程重启后还能找回之前的会话」。
        """

        session_ids = await asyncio.to_thread(store.list_sessions)
        return SessionListResponse(sessions=session_ids)

    @app.get("/sessions/{session_id}/messages", response_model=HistoryResponse)
    async def get_messages(session_id: str) -> HistoryResponse:
        try:
            messages = await make_runner().history(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return HistoryResponse(
            session_id=session_id,
            messages=[
                MessageOut(
                    role=m.role,
                    content=m.content,
                    tool_call_id=m.tool_call_id,
                    tool_calls=[
                        {"id": c.id, "name": c.name, "arguments": c.arguments}
                        for c in m.tool_calls
                    ],
                )
                for m in messages
            ],
        )

    @app.get("/sessions/{session_id}/recovery", response_model=RecoveryResponse)
    async def get_recovery(session_id: str) -> RecoveryResponse:
        try:
            result = await make_runner().recover(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return RecoveryResponse(
            session_id=session_id,
            stop_reason=result.stop_reason or "unknown",
            rounds=result.rounds,
            tool_calls=result.tool_calls,
            unfinished_calls=[
                {"call_id": c.id, "name": c.name, "arguments": c.arguments}
                for c in result.unfinished
            ],
        )

    @app.post("/sessions/{session_id}/messages")
    async def post_message(session_id: str, body: TurnRequest, request: Request) -> StreamingResponse:
        runner = make_runner()
        try:
            if not await store.exists(session_id):
                raise SessionNotFoundError(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        async def event_stream() -> AsyncIterator[str]:
            try:
                async for event in runner.run_turn(session_id, body.input):
                    if await request.is_disconnected():
                        return
                    yield _encode(event)
            except ProviderError as exc:
                yield _sse("error", {"kind": "provider", "message": str(exc)})
            except AgentRuntimeError as exc:
                yield _sse("error", {"kind": type(exc).__name__, "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 - 流已经开始，只能把错误当事件发出去
                yield _sse("error", {"kind": "internal", "message": str(exc)})

        return StreamingResponse(
            event_stream(), media_type="text/event-stream", headers=SSE_HEADERS
        )

    return app


def _encode(event: LoopEvent) -> str:
    if isinstance(event, TextChunk):
        return _sse("text", {"text": event.text})
    if isinstance(event, ToolStarted):
        return _sse(
            "tool_started",
            {"call_id": event.call_id, "name": event.name, "arguments": event.arguments},
        )
    if isinstance(event, ToolFinished):
        return _sse(
            "tool_finished",
            {
                "call_id": event.call_id,
                "name": event.name,
                "is_error": event.is_error,
                "duration_ms": event.duration_ms,
                "content": event.content,
            },
        )
    if isinstance(event, TurnResult):
        return _sse(
            "done",
            {
                "text": event.text,
                "rounds": event.rounds,
                "tool_calls": event.tool_calls,
                "stop_reason": event.stop_reason,
                "unfinished": [c.id for c in event.unfinished],
            },
        )
    return _sse("unknown", {})


def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def app_from_env() -> FastAPI:
    """给 ``uvicorn --factory`` 用：配置全部来自环境变量。"""

    return create_app(Settings.from_env())
