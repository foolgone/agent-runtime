"""端到端冒烟：真实的 HTTP 进、真实的 HTTP 出、真实的磁盘日志。

跑法（先起 fake_openai_server.py）：

    python tools/smoke/fake_openai_server.py 8731 &
    python tools/smoke/smoke_e2e.py 8731

验证四件事：
1. OpenAI 兼容 provider 能解析真实 SSE
2. 工具调用被真正执行，结果回到模型，第二轮拿到正确历史
3. 事件日志按预期顺序落在磁盘上
4. 人为制造「崩溃」（删掉 tool/result 之后的事件）后，恢复接口能认出未收尾的调用
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

from agent_runtime.api.app import create_app
from agent_runtime.config import Settings


async def main(port: int) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="agent-runtime-smoke-"))
    settings = Settings(
        provider="openai",
        model="fake-model",
        api_key="fake-key",
        base_url=f"http://127.0.0.1:{port}/v1",
        data_dir=workdir,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        mark = "PASS" if condition else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
        if not condition:
            failures.append(label)

    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
        created = await client.post("/sessions")
        session_id = created.json()["session_id"]
        print(f"\nsession={session_id}\n")

        response = await client.post(
            f"/sessions/{session_id}/messages", json={"input": "现在几点？"}
        )
        frames = [f for f in response.text.split("\n\n") if f.strip()]
        events = _parse(frames)

        print("SSE:")
        for name, data in events:
            print(f"  {name:14} {json.dumps(data, ensure_ascii=False)[:90]}")

        names = [n for n, _ in events]
        done = dict(events).get("done", {})

        print("\nchecks:")
        check("HTTP 200", response.status_code == 200, str(response.status_code))
        check("流里有文本增量", names.count("text") >= 2, f"{names.count('text')} 段")
        check("工具被调用", "tool_started" in names and "tool_finished" in names)
        check("done 收到", names[-1] == "done")
        check("两轮往返", done.get("rounds") == 2, str(done.get("rounds")))
        check("一次工具调用", done.get("tool_calls") == 1, str(done.get("tool_calls")))
        check("干净收尾", done.get("stop_reason") == "stop", str(done.get("stop_reason")))

        tool_finished = dict(events).get("tool_finished", {})
        check("工具执行成功", tool_finished.get("is_error") is False)

        # 事件日志落盘顺序
        log = settings.data_dir / "sessions" / f"{session_id}.jsonl"
        record_types = [json.loads(line)["type"] for line in log.read_text("utf-8").splitlines()]
        expected = [
            "session/created",
            "user/message",
            "assistant/message",
            "tool/call",
            "tool/result",
            "assistant/message",
            "turn/end",
        ]
        check("日志顺序正确", record_types == expected, " -> ".join(record_types))

        history = (await client.get(f"/sessions/{session_id}/messages")).json()
        check(
            "历史可重放",
            [m["role"] for m in history["messages"]] == ["user", "assistant", "tool", "assistant"],
            str([m["role"] for m in history["messages"]]),
        )
        check("工具结果内容正确", "T" in history["messages"][2]["content"])

        # 制造崩溃：把 tool/result 之后的事件全部删掉，模拟工具执行中途进程被杀
        lines = log.read_text("utf-8").splitlines()
        cut = next(i for i, line in enumerate(lines) if json.loads(line)["type"] == "tool/result")
        log.write_text("\n".join(lines[:cut]) + "\n", encoding="utf-8")

        recovery = (await client.get(f"/sessions/{session_id}/recovery")).json()
        print(f"\n恢复报告: {json.dumps(recovery, ensure_ascii=False)}")
        check("识别出中断", recovery["stop_reason"] == "interrupted", recovery["stop_reason"])
        check(
            "未收尾调用可见",
            [c["name"] for c in recovery["unfinished_calls"]] == ["now"],
            str(recovery["unfinished_calls"]),
        )

        health = (await client.get("/healthz")).json()
        check("healthz 正常", health["status"] == "ok")
        check("写工具未暴露", "write_note" not in health["tools"])

    shutil.rmtree(workdir, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} 项 -> {failures}")
        return 1
    print("ALL PASS")
    return 0


def _parse(frames: list[str]) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in frames:
        name = ""
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[len("data: ") :])))
    return events


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8731)))
