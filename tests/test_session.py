"""事件日志与重放。"""

from __future__ import annotations

import json

import pytest

from agent_runtime.session.projection import ERROR_PREFIX, project
from agent_runtime.session.store import EventStore


async def test_seq_is_monotonic_per_session(store: EventStore) -> None:
    first = await store.append("s1", "user/message", {"content": "a"})
    second = await store.append("s2", "user/message", {"content": "b"})
    third = await store.append("s1", "turn/end", {})

    assert (first.seq, second.seq, third.seq) == (1, 1, 2)


async def test_concurrent_appends_do_not_collide(store: EventStore) -> None:
    """并发 append 必须拿到互不相同的 seq，否则重放顺序会错乱。"""

    import asyncio

    await asyncio.gather(*(store.append("s", "user/message", {"content": str(i)}) for i in range(20)))
    events = await store.read("s")
    assert [e.seq for e in events] == list(range(1, 21))
    assert len({id(e) for e in events}) == 20


async def test_append_survives_new_store_instance(store: EventStore) -> None:
    await store.append("s", "user/message", {"content": "hello"})
    reopened = EventStore(store.root)
    events = await reopened.read("s")
    assert len(events) == 1
    assert events[0].data["content"] == "hello"


async def test_seq_continues_after_reopen(store: EventStore) -> None:
    await store.append("s", "user/message", {"content": "a"})
    reopened = EventStore(store.root)
    event = await reopened.append("s", "turn/end", {})
    assert event.seq == 2


async def test_invalid_session_id_rejected(store: EventStore) -> None:
    for bad in ["../etc/passwd", "a/b", "has space", "", "x" * 65, "dot.dot"]:
        with pytest.raises(ValueError, match="invalid session id"):
            await store.append(bad, "user/message", {})


async def test_truncated_trailing_line_is_tolerated(store: EventStore) -> None:
    """崩溃可能正好落在写一半，最后一行是残的。读的时候跳过，不要判定整个会话损坏。"""

    await store.append("s", "user/message", {"content": "a"})
    await store.append("s", "turn/end", {})

    path = store.path_for("s")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"seq":3,"ts":"2026-01-01T00:00:00.000+00:00","type":"user/mess')

    events = await store.read("s")
    assert [e.seq for e in events] == [1, 2]


async def test_projection_rebuilds_messages(store: EventStore) -> None:
    await store.append("s", "session/created", {})
    await store.append("s", "user/message", {"content": "hi"})
    await store.append(
        "s",
        "assistant/message",
        {
            "content": "",
            "tool_calls": [{"id": "c1", "name": "echo", "arguments": '{"text":"hi"}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )
    await store.append("s", "tool/call", {"call_id": "c1", "name": "echo", "arguments": "{}"})
    await store.append(
        "s", "tool/result", {"call_id": "c1", "name": "echo", "content": "hi", "is_error": False}
    )
    await store.append("s", "turn/end", {"stop_reason": "stop"})

    projection = project(await store.read("s"))

    assert [m.role for m in projection.messages] == ["user", "assistant", "tool"]
    assert projection.messages[1].tool_calls[0].name == "echo"
    assert projection.messages[2].tool_call_id == "c1"
    assert (projection.input_tokens, projection.output_tokens) == (10, 5)
    assert projection.turns == 1
    assert projection.is_clean


async def test_projection_flags_unfinished_tool_call(store: EventStore) -> None:
    """有 tool/call 没有 tool/result —— 已受理、结果未知。这正是崩溃恢复要认出来的状态。"""

    await store.append("s", "user/message", {"content": "hi"})
    await store.append("s", "tool/call", {"call_id": "c1", "name": "write_note", "arguments": "{}"})

    projection = project(await store.read("s"))

    assert [c.id for c in projection.unfinished_calls] == ["c1"]
    assert not projection.is_clean


async def test_projection_marks_error_results_for_the_model(store: EventStore) -> None:
    await store.append("s", "tool/call", {"call_id": "c1", "name": "echo", "arguments": "{}"})
    await store.append(
        "s", "tool/result", {"call_id": "c1", "name": "echo", "content": "bad", "is_error": True}
    )

    projection = project(await store.read("s"))
    assert projection.messages[0].content == ERROR_PREFIX + "bad"


async def test_projection_records_anomalies_without_crashing(store: EventStore) -> None:
    await store.append("s", "tool/result", {"call_id": "ghost", "name": "echo", "content": "x"})
    await store.append("s", "tool/call", {"call_id": "c1", "name": "echo", "arguments": "{}"})
    await store.append("s", "tool/call", {"call_id": "c1", "name": "echo", "arguments": "{}"})

    projection = project(await store.read("s"))

    assert len(projection.anomalies) == 2
    assert not projection.is_clean


async def test_event_record_is_valid_json_line(store: EventStore) -> None:
    await store.append("s", "user/message", {"content": "中文"})
    raw = store.path_for("s").read_text(encoding="utf-8").strip()
    record = json.loads(raw)
    assert record["seq"] == 1
    assert record["data"]["content"] == "中文"
    assert "\\u" not in raw  # ensure_ascii=False，日志可读
