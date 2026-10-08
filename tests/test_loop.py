"""编排循环：工具往返、错误回传、上限保护、崩溃恢复。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from conftest import ScriptedProvider, text_script, tool_call_script

from agent_runtime.loop.runner import AgentRunner, TextChunk, ToolFinished, TurnResult
from agent_runtime.session.store import EventStore
from agent_runtime.tools.registry import ToolRegistry, build_default_registry
from agent_runtime.tools.spec import ToolResult, ToolSpec


def build_runner(
    provider: ScriptedProvider,
    store: EventStore,
    registry: ToolRegistry | None = None,
    **kwargs: Any,
) -> AgentRunner:
    return AgentRunner(
        provider=provider,
        store=store,
        registry=registry if registry is not None else build_default_registry(),
        **kwargs,
    )


async def drain(runner: AgentRunner, session_id: str, text: str) -> list[Any]:
    return [event async for event in runner.run_turn(session_id, text)]


async def test_plain_answer_needs_no_tool(store: EventStore) -> None:
    provider = ScriptedProvider([text_script("你好")])
    runner = build_runner(provider, store)

    events = await drain(runner, "s", "hi")
    result = events[-1]

    assert isinstance(result, TurnResult)
    assert result.text == "你好"
    assert result.rounds == 1
    assert result.tool_calls == 0
    assert result.stop_reason == "stop"

    recorded = [e.type for e in await store.read("s")]
    assert recorded == ["user/message", "assistant/message", "turn/end"]


async def test_tool_round_trip(store: EventStore) -> None:
    provider = ScriptedProvider(
        [
            tool_call_script("c1", "echo", json.dumps({"text": "ping"})),
            text_script("工具说 ping"),
        ]
    )
    runner = build_runner(provider, store)

    events = await drain(runner, "s", "帮我回显 ping")
    kinds = [type(e).__name__ for e in events]

    assert "ToolStarted" in kinds
    assert "ToolFinished" in kinds
    assert events[-1].rounds == 2  # 第一次要工具，第二次收尾

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.content == "ping"
    assert not finished.is_error

    # 第二次请求模型时，消息里必须带上工具结果
    second_call_messages = provider.calls[1]["messages"]
    assert [m.role for m in second_call_messages] == ["user", "assistant", "tool"]
    assert second_call_messages[2].tool_call_id == "c1"
    assert second_call_messages[2].content == "ping"

    assert [e.type for e in await store.read("s")] == [
        "user/message",
        "assistant/message",
        "tool/call",
        "tool/result",
        "assistant/message",
        "turn/end",
    ]


async def test_tool_call_is_logged_before_it_runs(store: EventStore) -> None:
    """不变式 1：执行之前先记 tool/call。崩在工具里也得留下痕迹。"""

    observed: list[list[str]] = []

    async def spy(_: Mapping[str, Any]) -> ToolResult:
        observed.append([e.type for e in await store.read("s")])
        return ToolResult.ok("seen")

    registry = ToolRegistry(
        [ToolSpec(name="spy", description="spy", handler=spy, parameters={"type": "object"})]
    )
    provider = ScriptedProvider(
        [tool_call_script("c1", "spy", "{}"), text_script("done")]
    )
    runner = build_runner(provider, store, registry)

    await drain(runner, "s", "go")

    assert observed, "工具没有被执行"
    at_execution_time = observed[0]
    assert "tool/call" in at_execution_time
    assert "tool/result" not in at_execution_time


async def test_final_text_is_the_answer_not_the_preamble(store: EventStore) -> None:
    """模型先说一句开场白再调工具，最后给答案。返回的必须是答案。"""

    provider = ScriptedProvider(
        [
            tool_call_script("c1", "echo", '{"text":"x"}', preamble="让我查一下。"),
            text_script("答案在这里。"),
        ]
    )
    runner = build_runner(provider, store)

    result = (await drain(runner, "s", "go"))[-1]

    assert result.text == "答案在这里。"
    assert "让我查一下。" not in result.text


async def test_falls_back_to_preamble_when_final_round_is_silent(store: EventStore) -> None:
    """收尾那一轮模型没说话时，不能返回空字符串把之前说过的内容丢掉。"""

    provider = ScriptedProvider(
        [
            tool_call_script("c1", "echo", '{"text":"x"}', preamble="我先说一句。"),
            text_script(""),
        ]
    )
    runner = build_runner(provider, store)

    result = (await drain(runner, "s", "go"))[-1]

    assert result.text == "我先说一句。"


async def test_tool_error_is_fed_back_to_the_model(store: EventStore) -> None:
    provider = ScriptedProvider(
        [
            tool_call_script("c1", "echo", "not-json"),
            text_script("我改一下参数"),
        ]
    )
    runner = build_runner(provider, store)

    events = await drain(runner, "s", "go")
    finished = next(e for e in events if isinstance(e, ToolFinished))

    assert finished.is_error
    assert "not valid JSON" in finished.content

    # 错误不是终点：模型拿到了错误内容，才有机会自我纠正
    tool_message = provider.calls[1]["messages"][-1]
    assert tool_message.role == "tool"
    assert "not valid JSON" in tool_message.content


async def test_unknown_tool_reported_back(store: EventStore) -> None:
    provider = ScriptedProvider(
        [tool_call_script("c1", "ghost_tool", "{}"), text_script("换个工具")]
    )
    runner = build_runner(provider, store)

    events = await drain(runner, "s", "go")
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.is_error
    assert "unknown tool" in finished.content


async def test_write_tool_blocked_by_registry(store: EventStore) -> None:
    """写操作工具留在注册表外，模型要它也只能拿到拒绝。"""

    provider = ScriptedProvider(
        [tool_call_script("c1", "write_note", '{"name":"a.md","content":"x"}'), text_script("好")]
    )
    runner = build_runner(provider, store)

    events = await drain(runner, "s", "写一篇")
    finished = next(e for e in events if isinstance(e, ToolFinished))

    assert finished.is_error
    assert "not allowed" in finished.content
    # 也不该出现在给模型的工具声明里
    assert "write_note" not in [t.name for t in provider.calls[0]["tools"]]


async def test_max_tool_rounds_stops_runaway_loop(store: EventStore) -> None:
    provider = ScriptedProvider(
        [tool_call_script(f"c{i}", "echo", '{"text":"x"}') for i in range(10)]
    )
    runner = build_runner(provider, store, max_tool_rounds=3)

    events = await drain(runner, "s", "go")
    result = events[-1]

    assert result.stop_reason == "max_tool_rounds_exceeded"
    assert result.rounds == 3
    assert len(provider.calls) == 3

    types = [e.type for e in await store.read("s")]
    assert types[-1] == "turn/error"
    assert types.count("tool/call") == 3


async def test_state_comes_from_the_log_not_memory(store: EventStore) -> None:
    """空会话起步，第二轮请求里必须能看到第一轮的全部消息。"""

    provider = ScriptedProvider([text_script("一"), text_script("二")])
    runner = build_runner(provider, store)

    await drain(runner, "s", "第一句")
    await drain(runner, "s", "第二句")

    second_call = provider.calls[1]["messages"]
    assert [m.content for m in second_call] == ["第一句", "一", "第二句"]


async def test_recover_reports_clean_session(store: EventStore) -> None:
    provider = ScriptedProvider([text_script("好")])
    runner = build_runner(provider, store)
    await drain(runner, "s", "hi")

    result = await runner.recover("s")
    assert result.stop_reason == "clean"
    assert result.unfinished == []


async def test_recover_detects_interrupted_tool_call(store: EventStore) -> None:
    """模拟进程死在工具执行中途：日志停在 tool/call，没有 tool/result。"""

    provider = ScriptedProvider()
    runner = build_runner(provider, store)

    await store.append("s", "session/created", {})
    await store.append("s", "user/message", {"content": "写一篇笔记"})
    await store.append(
        "s",
        "assistant/message",
        {
            "content": "",
            "tool_calls": [{"id": "c1", "name": "write_note", "arguments": '{"name":"a.md"}'}],
        },
    )
    await store.append(
        "s", "tool/call", {"call_id": "c1", "name": "write_note", "arguments": '{"name":"a.md"}'}
    )

    result = await runner.recover("s")

    assert result.stop_reason == "interrupted"
    assert [c.id for c in result.unfinished] == ["c1"]
    assert result.unfinished[0].name == "write_note"
    # 恢复只汇报，不擅自重试——写操作重试与否该由调用方决定
    assert [e.type for e in await store.read("s")][-1] == "tool/call"


async def test_ensure_session_is_idempotent(store: EventStore) -> None:
    runner = build_runner(ScriptedProvider(), store)

    assert await runner.ensure_session("s") is True
    assert await runner.ensure_session("s") is False
    assert [e.type for e in await store.read("s")] == ["session/created"]


async def test_text_deltas_stream_in_order(store: EventStore) -> None:
    from agent_runtime.llm.base import Completed, TextDelta

    provider = ScriptedProvider(
        [[TextDelta("你"), TextDelta("好"), TextDelta("呀"), Completed(stop_reason="stop")]]
    )
    runner = build_runner(provider, store)

    events = await drain(runner, "s", "hi")
    assert "".join(e.text for e in events if isinstance(e, TextChunk)) == "你好呀"


async def test_session_not_found_on_unknown_id_is_not_an_error_for_history(store: EventStore) -> None:
    runner = build_runner(ScriptedProvider(), store)
    assert await runner.history("nope") == []


@pytest.mark.parametrize("bad_session", ["../x", "a b"])
async def test_run_turn_rejects_bad_session_id(store: EventStore, bad_session: str) -> None:
    runner = build_runner(ScriptedProvider(), store)
    with pytest.raises(ValueError, match="invalid session id"):
        await drain(runner, bad_session, "hi")
