"""HTTP 层：SSE 事件名、状态码、恢复接口。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

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


# --------------------------------------------------------------------------- provider 生命周期


@pytest.fixture
async def real_provider_client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, list[int]]]:
    """走「自己造 provider」的路径，并数它被造了几次。

    别的用例都注入假 provider，绕过了这条路径——而这里恰恰是要测的：
    provider 是每请求一个还是全局一个。
    """

    import agent_runtime.api.app as app_module

    built: list[int] = []

    def counting_build(active_settings: Settings):
        built.append(1)
        return ScriptedProvider(
            [
                tool_call_script("c1", "echo", '{"text":"pong"}', preamble="让我查一下。"),
                text_script("回显完成"),
            ]
        )

    monkeypatch.setattr(app_module, "build_provider", counting_build)
    app = create_app(settings, registry=build_default_registry())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as active:
        yield active, built


async def test_provider_is_built_once_not_per_request(
    real_provider_client: tuple[httpx.AsyncClient, list[int]],
) -> None:
    """每个请求造一个 provider，就会一起造一个 httpx.AsyncClient。

    连接池的意义是不重建；每请求一个 provider 等于每轮对话重新握手，
    且建出来的客户端没人关。
    """

    client, built = real_provider_client
    session_id = await start_session(client)
    await parse_sse(await client.post(f"/sessions/{session_id}/messages", json={"input": "回声"}))
    await client.get(f"/sessions/{session_id}/messages")
    await client.get(f"/sessions/{session_id}/recovery")

    assert len(built) == 1, f"provider 被构造了 {len(built)} 次，应当是 1 次"


async def test_read_only_endpoints_never_build_a_provider(
    real_provider_client: tuple[httpx.AsyncClient, list[int]],
) -> None:
    """查历史、查恢复不碰模型，就不该建 provider，也不该因为模型配置坏了而失败。"""

    client, built = real_provider_client
    session_id = await start_session(client)

    assert (await client.get(f"/sessions/{session_id}/messages")).status_code == 200
    assert (await client.get(f"/sessions/{session_id}/recovery")).status_code == 200
    assert built == []


async def test_read_only_endpoints_survive_broken_model_config(
    settings: Settings, tmp_path: Path
) -> None:
    """模型配置写错，只读接口照样能用。

    这是上面那条的实际后果，单独测一遍是因为它才是用户能感知到的差别：
    查询历史不该因为 AGENT_PROVIDER 拼错而 500。
    """

    broken = replace(settings, provider="no-such-provider")
    app = create_app(broken, registry=build_default_registry(), notes_dir=tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = await start_session(client)
        history = await client.get(f"/sessions/{session_id}/messages")
        recovery = await client.get(f"/sessions/{session_id}/recovery")

    assert history.status_code == 200
    assert recovery.status_code == 200
