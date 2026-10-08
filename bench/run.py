"""压测入口。

    python -m bench.run            # 默认 quick
    python -m bench.run --full     # 全量，慢
    python -m bench.run --out bench/RESULTS.md

不装 pytest-benchmark 之类的框架，理由是这个仓库要压的东西很窄：
三块各测各的，加一层框架只会让「到底测了什么」变得不透明。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from bench.report import render
from bench.scenarios import ScenarioResult, scenario_append, scenario_replay, scenario_turn
from bench.stats import Machine, warmup


@dataclass(frozen=True, slots=True)
class Preset:
    """规模参数。quick 用于本地随手跑，full 用于出报告。"""

    append_sessions: int
    append_events: int
    replay_sizes: tuple[int, ...]
    replay_repeats: int
    concurrency: int
    turns_per_session: int
    tool_rounds: int
    latency_ms: float


QUICK = Preset(
    append_sessions=16,
    append_events=200,
    replay_sizes=(500, 2_000, 8_000),
    replay_repeats=5,
    concurrency=8,
    turns_per_session=3,
    tool_rounds=1,
    latency_ms=20.0,
)

FULL = Preset(
    append_sessions=64,
    append_events=500,
    replay_sizes=(1_000, 10_000, 50_000, 200_000),
    replay_repeats=7,
    concurrency=32,
    turns_per_session=6,
    tool_rounds=1,
    latency_ms=50.0,
)


async def run_all(preset: Preset, *, quiet: bool = False) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []

    async def step(label: str, coro) -> ScenarioResult:
        if not quiet:
            print(f"  · {label} ...", end="", flush=True)
        started = time.perf_counter()
        result = await coro
        if not quiet:
            print(f" {time.perf_counter() - started:.1f}s")
        return result

    warmup()
    results.append(
        await step(
            "事件日志写入",
            scenario_append(sessions=preset.append_sessions, events_per_session=preset.append_events),
        )
    )
    results.append(
        await step(
            "写入争用（同一会话）",
            scenario_append(
                sessions=preset.append_sessions,
                events_per_session=preset.append_events,
                same_session=True,
            ),
        )
    )
    results.append(
        await step(
            "读取与重放",
            scenario_replay(sizes=preset.replay_sizes, repeats=preset.replay_repeats),
        )
    )
    results.append(
        await step(
            "端到端轮次",
            scenario_turn(
                concurrency=preset.concurrency,
                turns_per_session=preset.turns_per_session,
                tool_rounds=preset.tool_rounds,
                latency_ms=preset.latency_ms,
            ),
        )
    )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench", description="agent-runtime 压测")
    parser.add_argument("--full", action="store_true", help="全量规模（默认 quick）")
    parser.add_argument("--out", type=Path, default=None, help="把 Markdown 报告写到文件")
    args = parser.parse_args(argv)

    preset = FULL if args.full else QUICK
    print(f"bench: {'full' if args.full else 'quick'} 预设，模型调用全部 stub。")

    started = time.perf_counter()
    results = asyncio.run(run_all(preset))
    elapsed = time.perf_counter() - started

    markdown = render(results, Machine.current(), elapsed)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")
        print(f"报告已写入 {args.out}")
    else:
        sys.stdout.write("\n" + markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
