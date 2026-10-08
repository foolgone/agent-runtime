"""压测场景。

三个场景各自隔离一层成本，避免把不同来源的耗时混在一起：

- ``append``  只压事件日志写入。这是唯一有同步 I/O 的地方（fsync），
              也是最容易成为瓶颈的地方。
- ``replay``  只压读取与重放。验证 ADR 0001 里「重放是 O(n)，先不做快照」
              这个判断在什么规模上还成立。
- ``turn``    端到端一轮对话。模型延迟是固定参数，扣掉它剩下的才是运行时开销。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.loop.runner import AgentRunner
from agent_runtime.session.projection import project
from agent_runtime.session.store import EventStore
from agent_runtime.tools.registry import build_default_registry
from bench.stats import Sample, time_call, timed
from bench.stub_provider import StubProvider

# 一轮对话（1 次工具往返）大约产生的事件条数，用来把「每条事件成本」折算成「每轮成本」
EVENTS_PER_TURN = 5


@dataclass(slots=True)
class Row:
    metric: str
    value: str
    note: str = ""


@dataclass(slots=True)
class ScenarioResult:
    key: str
    title: str
    setup: str
    rows: list[Row] = field(default_factory=list)
    samples: dict[str, Sample] = field(default_factory=dict)
    # 原始数值，供报告算比例用。不放这儿的话，报告就得去解析上面那些格式化过的字符串，
    # 一旦显示格式改了，结论区的算术会跟着悄悄错掉。
    metrics: dict[str, float] = field(default_factory=dict)

    def add(self, metric: str, value: str, note: str = "", *, raw: float | None = None) -> None:
        self.rows.append(Row(metric, value, note))
        if raw is not None:
            self.metrics[metric] = raw


# --------------------------------------------------------------------------- 1. 写入

async def scenario_append(
    *, sessions: int, events_per_session: int, same_session: bool = False
) -> ScenarioResult:
    """并发追加事件，测事件日志的写入吞吐。

    ``same_session=True`` 时所有 worker 挤在同一个会话上，
    这时每会话那把锁会串行化写入——用来判断锁本身是不是瓶颈。
    """

    root = Path(tempfile.mkdtemp(prefix="bench-append-"))
    store = EventStore(root)
    latencies: list[float] = []

    async def worker(session_id: str) -> None:
        for index in range(events_per_session):
            latencies.append(
                await timed(store.append(session_id, "user/message", {"content": f"m{index}"}))
            )

    target = "shared" if same_session else ""
    try:
        start = time.perf_counter()
        await asyncio.gather(*(worker(target or f"s{n}") for n in range(sessions)))
        elapsed = time.perf_counter() - start

        total = sessions * events_per_session
        sample = Sample(label="append latency", values_ms=latencies)
        summary = sample.summary()

        result = ScenarioResult(
            key="append_shared" if same_session else "append",
            title="1b. 写入争用（全部挤同一会话）" if same_session else "1. 事件日志写入",
            setup=(
                f"{sessions} 个 worker × 每个 {events_per_session} 次 append，"
                f"共 {total} 次；"
                + ("**全部写同一个会话**（锁串行化）" if same_session else "各写各的会话")
                + "。每次 append 都 fsync。"
            ),
            samples={"append": sample},
        )
        result.add(
            "吞吐",
            f"{total / elapsed:,.0f} 次/秒",
            f"总耗时 {elapsed * 1000:,.0f} ms",
            raw=total / elapsed,
        )
        result.add("单次 append p50", f"{summary['p50']:.3f} ms", raw=summary["p50"])
        result.add(
            "单次 append p95",
            f"{summary['p95']:.3f} ms",
            "长尾主要来自 fsync 排队",
            raw=summary["p95"],
        )
        result.add("单次 append p99", f"{summary['p99']:.3f} ms", raw=summary["p99"])
        result.add("并发 worker 数", f"{sessions}", raw=float(sessions))
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- 2. 重放

def _write_session_file(path: Path, events: int) -> None:
    """直接生成日志文件。

    这里测的是「读 + 重放」，不是「写」，所以绕开逐条 append 的 fsync——
    否则光是造数据就要花掉比测量本身更长的时间。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    counter = 0
    with path.open("w", encoding="utf-8") as handle:
        while counter < events:
            for record in (
                {"type": "user/message", "data": {"content": "问题"}},
                {
                    "type": "assistant/message",
                    "data": {
                        "content": "让我查一下。",
                        "usage": {"input_tokens": 120, "output_tokens": 30},
                        "tool_calls": [{"id": f"c{counter}", "name": "echo", "arguments": "{}"}],
                    },
                },
                {"type": "tool/call", "data": {"call_id": f"c{counter}", "name": "echo", "arguments": "{}"}},
                {
                    "type": "tool/result",
                    "data": {"call_id": f"c{counter}", "name": "echo", "content": "ok", "is_error": False},
                },
                {"type": "turn/end", "data": {"stop_reason": "stop"}},
            ):
                counter += 1
                if counter > events:
                    break
                handle.write(json.dumps({"seq": counter, "ts": "", **record}, ensure_ascii=False) + "\n")


async def scenario_replay(*, sizes: Sequence[int], repeats: int = 5) -> ScenarioResult:
    """测读取与重放的成本随事件数的增长。"""

    root = Path(tempfile.mkdtemp(prefix="bench-replay-"))
    store = EventStore(root)
    result = ScenarioResult(
        key="replay",
        title="2. 读取与重放",
        setup=(
            "直接生成日志文件（绕开逐条 fsync），分别计 "
            "`store.read()`（I/O + JSON 解析）与 `project()`（纯 CPU）。"
            f"每个规模取 {repeats} 次最小值。"
        ),
    )

    # (规模, 读 ms, 投影 ms)。结论区的「读比投影贵」「大到后面会弯」都从这里取数，
    # 所以先攒原始值再渲染，别让报告回头去解析已经格式化过的字符串。
    measured: list[tuple[int, float, float]] = []

    try:
        for size in sizes:
            session_id = f"s{size}"
            _write_session_file(store.path_for(session_id), size)

            read_ms = min([await timed(store.read(session_id)) for _ in range(repeats)])

            events = await store.read(session_id)
            project_ms = time_call(lambda events=events: project(events), repeats=repeats)
            total_ms = read_ms + project_ms

            measured.append((size, read_ms, project_ms))
            result.add(
                f"{size:,} 条事件",
                f"读 {read_ms:,.2f} ms + 投影 {project_ms:,.2f} ms = {total_ms:,.2f} ms",
                f"每条 {total_ms / size * 1000:.2f} µs"
                f"（读 {read_ms / size * 1000:.2f} + 投影 {project_ms / size * 1000:.2f}）",
            )
            result.samples.setdefault("read", Sample(label="read")).add(read_ms)
            result.samples.setdefault("project", Sample(label="project")).add(project_ms)

        smallest, middle, largest = measured[0], measured[len(measured) // 2], measured[-1]

        result.add(
            "每事件读成本",
            f"{smallest[1] / smallest[0] * 1000:.2f} → "
            f"{middle[1] / middle[0] * 1000:.2f} → "
            f"{largest[1] / largest[0] * 1000:.2f} µs",
            f"规模 {smallest[0]:,} → {middle[0]:,} → {largest[0]:,}；读是大头",
        )
        result.add(
            "每事件投影成本",
            f"{smallest[2] / smallest[0] * 1000:.2f} → "
            f"{middle[2] / middle[0] * 1000:.2f} → "
            f"{largest[2] / largest[0] * 1000:.2f} µs",
            "纯 CPU；**不是常数**——单条成本随规模上升，见结论",
        )

        read_us = largest[1] / largest[0] * 1000
        project_us = largest[2] / largest[0] * 1000
        turn_ms = (read_us + project_us) * EVENTS_PER_TURN / 1000
        result.add(
            "按一轮折算",
            f"{turn_ms:.3f} ms",
            f"一轮 {EVENTS_PER_TURN} 条事件 × 最大档的单条成本",
            raw=turn_ms,
        )

        result.metrics["每事件读成本@中间档"] = middle[1] / middle[0] * 1000
        result.metrics["每事件读成本@最大档"] = read_us
        result.metrics["每事件投影成本@中间档"] = middle[2] / middle[0] * 1000
        result.metrics["每事件投影成本@最大档"] = project_us
        result.metrics["每事件总成本@最大档"] = read_us + project_us

        result.add(
            "注意",
            "上表是「重放一次」的成本",
            "runner 每轮对话内的每一次模型往返都会重放一遍，"
            "所以一轮的实际累计开销是「往返次数 × 会话长度」，整个会话累计是 O(n²)"
            "——见报告结论",
        )
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- 3. 端到端

async def scenario_turn(
    *,
    concurrency: int,
    turns_per_session: int,
    tool_rounds: int,
    latency_ms: float,
) -> ScenarioResult:
    """端到端一轮对话的并发表现。

    这里是唯一能回答「高并发怎么样」的场景。模型延迟固定，
    所以结果是**运行时自身开销**的上界估计，不是真实端到端延迟。
    """

    root = Path(tempfile.mkdtemp(prefix="bench-turn-"))
    store = EventStore(root)
    provider = StubProvider(latency_ms=latency_ms, tool_rounds=tool_rounds)
    registry = build_default_registry(notes_dir=root / "notes")
    latencies: list[float] = []

    async def worker(index: int) -> None:
        runner = AgentRunner(
            provider=provider,
            store=store,
            registry=registry,
            max_tool_rounds=8,
        )
        session_id = f"s{index}"
        await runner.ensure_session(session_id)
        for turn in range(turns_per_session):
            start = time.perf_counter()
            async for _ in runner.run_turn(session_id, f"第 {turn} 个问题"):
                pass
            latencies.append((time.perf_counter() - start) * 1000.0)

    try:
        # 预热：让 import、字典扩容、首次文件创建都发生在计时窗口之外
        warm = AgentRunner(provider=provider, store=store, registry=registry)
        await warm.ensure_session("warmup")
        async for _ in warm.run_turn("warmup", "预热"):
            pass

        start = time.perf_counter()
        await asyncio.gather(*(worker(n) for n in range(concurrency)))
        elapsed = time.perf_counter() - start

        total_turns = concurrency * turns_per_session
        sample = Sample(label="turn latency", values_ms=latencies)
        summary = sample.summary()
        model_calls_per_turn = tool_rounds + 1
        model_ms = latency_ms * model_calls_per_turn
        events_per_turn = 2 + tool_rounds * 2 + 1

        result = ScenarioResult(
            key="turn",
            title="3. 端到端轮次（并发会话）",
            setup=(
                f"{concurrency} 个并发会话 × 每个 {turns_per_session} 轮 = {total_turns} 轮；"
                f"每轮 {tool_rounds} 次工具往返（每轮约 {events_per_turn} 条事件）。"
                f"**模型延迟固定为 {latency_ms:.0f} ms/次调用，不是真实网络。**"
            ),
            samples={"turn": sample},
        )
        result.add("总耗时", f"{elapsed:,.3f} s", raw=elapsed)
        result.add(
            "吞吐",
            f"{total_turns / elapsed:,.1f} 轮/秒",
            f"模型调用共 {provider.calls:,} 次",
            raw=total_turns / elapsed,
        )
        result.add("单轮延迟 p50", f"{summary['p50']:.2f} ms", raw=summary["p50"])
        result.add("单轮延迟 p95", f"{summary['p95']:.2f} ms", raw=summary["p95"])
        result.add("单轮延迟 p99", f"{summary['p99']:.2f} ms", raw=summary["p99"])
        result.add(
            "模型延迟（固定，非网络）",
            f"{model_ms:.0f} ms/轮",
            f"{latency_ms:.0f} ms × {model_calls_per_turn} 次调用",
            raw=model_ms,
        )
        result.add(
            "扣掉模型延迟后的运行时开销",
            f"p50 {summary['p50'] - model_ms:.2f} ms / p95 {summary['p95'] - model_ms:.2f} ms",
            f"p50 单轮延迟 − 固定模型延迟（{model_ms:.0f} ms）",
            raw=summary["p50"] - model_ms,
        )
        result.add(
            "每轮事件数",
            f"{events_per_turn} 条",
            "user/message + assistant/message + tool/call + tool/result + turn/end",
            raw=float(events_per_turn),
        )
        result.add("并发会话数", f"{concurrency}", raw=float(concurrency))
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)
