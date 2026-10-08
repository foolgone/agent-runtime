"""命令行入口。

``agent-runtime-chat``：本地多轮对话，工具调用过程直接打在终端上。
``agent-runtime-serve``：起 HTTP 服务。

两者的区别只在最外层，中间那条 runner -> store -> registry 的链路完全一样。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence

from dotenv import load_dotenv

from agent_runtime.config import ConfigError, Settings
from agent_runtime.errors import AgentRuntimeError
from agent_runtime.llm.registry import build_provider
from agent_runtime.loop.runner import AgentRunner, TextChunk, ToolFinished, ToolStarted
from agent_runtime.session.store import EventStore
from agent_runtime.tools.registry import build_default_registry

BANNER = "agent-runtime chat — /help 看命令，/exit 退出"


def _build_runner(args: argparse.Namespace, settings: Settings) -> AgentRunner:
    registry = build_default_registry(include_writes=args.allow_writes)
    return AgentRunner(
        provider=build_provider(settings),
        store=EventStore(settings.data_dir / "sessions"),
        registry=registry,
        max_tool_rounds=settings.max_tool_rounds,
        tool_timeout_s=settings.tool_timeout_s,
    )


async def _chat(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    runner = _build_runner(args, settings)

    session_id = args.session or uuid.uuid4().hex[:16]
    created = await runner.ensure_session(session_id)
    print(BANNER)
    print(f"session: {session_id} ({'新建' if created else '继续'})")
    print(f"tools:   {', '.join(runner.tools)}")
    print()

    if args.recover:
        await _print_recovery(runner, session_id)

    while True:
        try:
            # 丢到线程里读，别让 input() 阻塞事件循环
            line = (await asyncio.to_thread(input, "> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not line:
            continue
        if line in ("/exit", "/quit"):
            return 0
        if line == "/help":
            print("/history  显示重放出来的消息列表")
            print("/recover  检查有没有「已受理、结果未知」的工具调用")
            print("/exit     退出")
            continue
        if line == "/history":
            for message in await runner.history(session_id):
                print(f"  {message.role:9} {message.content[:120]}")
            continue
        if line == "/recover":
            await _print_recovery(runner, session_id)
            continue

        try:
            async for event in runner.run_turn(session_id, line):
                _render(event)
        except AgentRuntimeError as exc:
            print(f"\n[错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        print()


def _render(event: object) -> None:
    if isinstance(event, TextChunk):
        print(event.text, end="", flush=True)
    elif isinstance(event, ToolStarted):
        print(f"\n  → {event.name}({event.arguments})")
    elif isinstance(event, ToolFinished):
        mark = "✗" if event.is_error else "✓"
        detail = event.content.replace("\n", " ")[:100]
        print(f"  {mark} {event.name} [{event.duration_ms}ms] {detail}")


async def _print_recovery(runner: AgentRunner, session_id: str) -> None:
    result = await runner.recover(session_id)
    print(f"state: {result.stop_reason}, rounds={result.rounds}, tool_calls={result.tool_calls}")
    for call in result.unfinished:
        print(f"  ! 已受理未收尾: {call.name}({call.arguments}) id={call.id}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-runtime-chat", description="本地多轮对话")
    parser.add_argument("--session", help="继续指定会话；不传则新建")
    parser.add_argument("--recover", action="store_true", help="启动时检查未收尾的工具调用")
    parser.add_argument("--allow-writes", action="store_true", help="把写操作工具也放进允许集合")
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        return asyncio.run(_chat(args))
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        print("参考 .env.example，至少需要 AGENT_MODEL 和 AGENT_API_KEY。", file=sys.stderr)
        return 2


def serve(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-runtime-serve", description="启动 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    load_dotenv()
    uvicorn.run(
        "agent_runtime.api.app:app_from_env",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
