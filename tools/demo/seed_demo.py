"""造一份演示数据，用来给 README 截图。

截图必须是可复现的：手工点出来的界面，改动一次代码就再也对不上，
而且没人知道截图里那些状态是怎么来的。所以这个脚本把「截图上该有什么」
写成代码 —— 跑一遍就能重新造出同一份数据。

两段：

1. **正常的一轮**走 HTTP + SSE，跟界面走的是同一条路，所以那部分数据
   是真实跑出来的。
2. **崩溃的那一轮**直接往事件日志里追加事件，不经过 API。

第 2 步单独说明一下，因为它看起来像在伪造数据，其实不是：崩溃场景
**没法通过 API 造出来** —— 要制造崩溃，得让进程正好死在工具执行中途，
而 API 就是那个进程。所以这里直接写出那次崩溃会留下的日志：
``user/message`` -> ``assistant/message``(带 tool_calls) -> ``tool/call``，
然后停住。少了 ``tool/result`` 和后面的 ``turn/end``。

这正是「先记账、后执行」要留下的痕迹。界面因此能展示出别处看不到的东西：
一张停在「执行中」的工具卡片，和恢复面板里那条「已受理、结果未知」的记录。
两者都来自日志本身，没有一处是界面编的。

用法（先起好后端和假模型）：

    python tools/demo/seed_demo.py --base-url http://127.0.0.1:8000 \\
        --data-dir .data

跑完最好重启一次后端：``EventStore`` 按会话缓存了 seq，
绕过它直接写文件之后，继续在该会话上对话会撞号。截图只读日志，不受影响。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

CRASH_PROMPT = "再查一次时间，这次把时区一起说清楚。"
CRASH_PREAMBLE = "好，我再查一次。"
CRASH_TOOL = "now"
CRASH_ARGUMENTS = '{"utc_offset_hours":8}'


def _event(seq: int, event_type: str, data: dict[str, object]) -> str:
    record = {"seq": seq, "ts": datetime.now(UTC).isoformat(), "type": event_type, "data": data}
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def run_turn(client: httpx.Client, session_id: str, text: str) -> dict[str, object]:
    """跑一轮并返回 done 事件。SSE 逐帧读，跟前端一样。"""

    done: dict[str, object] = {}
    with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"input": text}
    ) as response:
        response.raise_for_status()
        name = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: ") and name == "done":
                done = json.loads(line[len("data: ") :])
    return done


def seed(base_url: str, session_count: int) -> list[str]:
    """建会话，每个会话跑一轮真实对话（含一次工具调用）。"""

    sessions: list[str] = []
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        for _ in range(session_count):
            session_id = client.post("/sessions").raise_for_status().json()["session_id"]
            result = run_turn(client, session_id, "现在几点？")
            print(
                f"  {session_id}  {result.get('rounds')} 轮往返 / "
                f"{result.get('tool_calls')} 次工具调用"
            )
            sessions.append(session_id)
    return sessions


def append_crashed_turn(data_dir: Path, session_id: str) -> str:
    """往日志尾部追加一次「执行到一半就没了」的工具调用。"""

    path = data_dir / "sessions" / f"{session_id}.jsonl"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    seq = json.loads(lines[-1])["seq"] + 1

    # 这次调用的 id 要跟前面那轮区分开，否则投影层会把它当成重复的 tool/call 记成异常
    call_id = f"call_{len([1 for line in lines if json.loads(line)['type'] == 'tool/call']) + 1}"

    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_event(seq, "user/message", {"content": CRASH_PROMPT}))
        handle.write(
            _event(
                seq + 1,
                "assistant/message",
                {
                    "content": CRASH_PREAMBLE,
                    "usage": {},
                    "tool_calls": [
                        {"id": call_id, "name": CRASH_TOOL, "arguments": CRASH_ARGUMENTS}
                    ],
                },
            )
        )
        # 到这里就停：工具已经开始执行（记录已落盘），进程没了。
        handle.write(
            _event(
                seq + 2,
                "tool/call",
                {"call_id": call_id, "name": CRASH_TOOL, "arguments": CRASH_ARGUMENTS},
            )
        )

    print(f"  {session_id}  追加了一次没有结果的 {CRASH_TOOL}({call_id})")
    return call_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seed_demo", description="造演示数据")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--sessions", type=int, default=2)
    parser.add_argument(
        "--no-crash",
        action="store_true",
        help="不制造崩溃场景（恢复面板会是空的）",
    )
    args = parser.parse_args(argv)

    print("造会话与对话（走 HTTP，跟界面同一条路）：")
    sessions = seed(args.base_url, args.sessions)

    if not args.no_crash:
        # 挑界面首屏会打开的那个（会话列表按 id 排序，取第一个），
        # 否则截图上看不到恢复面板里的内容
        target = sorted(sessions)[0]
        print("制造一次「已受理、结果未知」（直接写日志）：")
        append_crashed_turn(args.data_dir, target)
        print(f"\n截图时打开会话 {target}。")
        print("界面上刷新一下即可重新读日志。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
