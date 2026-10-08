"""HTTP 层：SSE 事件名、状态码、恢复接口。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from conftest import ScriptedProvider, text_script, tool_call_script

from agent_runtime.api.app import create_app
from agent_runtime.config import Settings
from agent_runtime.tools.registry import build_default_registry


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    provider = ScriptedProvider(
        [
            tool_call_script("c1", "echo", '{"text":"pong"}', preamble="让我查一下。"),
            text_script("回显完成"),
        ]
    )
    app = create_app(settings, provider=provider, registry=build_default_registry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as active:
        yield active


async def parse_sse(response: httpx.Response) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = ""
    async for line in response.aiter_lines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            events.append((name, json.loads(line[len("data: ") :])))
    return events


async def start_session(client: httpx.AsyncClient) -> str:
    response = await client.post("/sessions")
    assert response.status_code == 200
    return response.json()["session_id"]


async def test_healthz_lists_tools(client: httpx.AsyncClient) -> None:
    body = (await client.get("/healthz")).json()
    assert body["status"] == "ok"
    assert "echo" in body["tools"]
    assert "write_note" not in body["tools"]


async def test_create_session_returns_id_and_tools(client: httpx.AsyncClient) -> None:
    body = (await client.post("/sessions")).json()
    assert len(body["session_id"]) == 16
    assert "echo" in body["tools"]


async def test_turn_streams_sse_events(client: httpx.AsyncClient) -> None:
    session_id = await start_session(client)

    response = await client.post(f"/sessions/{session_id}/messages", json={"input": "回声"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = await parse_sse(response)
    names = [name for name, _ in events]

    assert names[0] == "text"
    assert "tool_started" in names
    assert "tool_finished" in names
    assert names[-1] == "done"

    done = dict(events)["done"]
    assert done["stop_reason"] == "stop"
    assert done["tool_calls"] == 1
    assert done["unfinished"] == []


async def test_history_after_turn(client: httpx.AsyncClient) -> None:
    session_id = await start_session(client)
    await parse_sse(await client.post(f"/sessions/{session_id}/messages", json={"input": "hi"}))

    body = (await client.get(f"/sessions/{session_id}/messages")).json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert body["messages"][2]["tool_call_id"] == "c1"


async def test_recovery_reports_clean(client: httpx.AsyncClient) -> None:
    session_id = await start_session(client)
    await parse_sse(await client.post(f"/sessions/{session_id}/messages", json={"input": "hi"}))

    body = (await client.get(f"/sessions/{session_id}/recovery")).json()
    assert body["stop_reason"] == "clean"
    assert body["unfinished_calls"] == []


async def test_unknown_session_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/sessions/doesnotexist/messages", json={"input": "hi"})
    assert response.status_code == 404


async def test_invalid_session_id_returns_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/sessions/bad..id/messages", json={"input": "hi"})
    assert response.status_code == 400


async def test_empty_input_rejected(client: httpx.AsyncClient) -> None:
    session_id = await start_session(client)
    response = await client.post(f"/sessions/{session_id}/messages", json={"input": ""})
    assert response.status_code == 422


async def test_sse_frames_are_well_formed(client: httpx.AsyncClient) -> None:
    session_id = await start_session(client)
    response = await client.post(f"/sessions/{session_id}/messages", json={"input": "回声"})

    raw = "".join([chunk async for chunk in response.aiter_text()])
    frames = [frame for frame in raw.split("\n\n") if frame.strip()]

    assert raw.endswith("\n\n")
    for frame in frames:
        lines = frame.splitlines()
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: ")
        json.loads(lines[1][len("data: ") :])

    assert "text/event-stream" in response.headers["content-type"]
    assert response.headers["x-accel-buffering"] == "no"
