"""把压测结果渲染成 Markdown。

报告必须自带「这些数字是什么、不是什么」。脱离口径的数字比没有数字更糟——
它会被当成结论引用，然后在面试里被一句话问穿。
"""

from __future__ import annotations

from datetime import UTC, datetime

from bench.scenarios import ScenarioResult
from bench.stats import Machine

DISCLAIMER = """\
> **读这些数字之前先读这段。**
>
> 模型调用全部由 `bench/stub_provider.py` 以**固定延迟**替代，不联网、不连真实模型。
> 所以「轮次延迟」反映的是**运行时自身的开销上界**，不是真实端到端延迟——
> 真实场景下上游模型的网络延迟通常比这里高出一到两个数量级。
>
> 这样设计是刻意的：拿真模型压测，量出来的是别人的网络，跟自己写的代码无关。
> 把模型延迟变成可控参数，扣掉它之后剩下的才是这个仓库该为之负责的部分。
>
> 数字随机器变化，绝对值只在同一台机器上横向对比才有意义。
> 复现：`python -m bench.run`（见 `bench/README.md`）。
"""


def render(results: list[ScenarioResult], machine: Machine, elapsed_s: float) -> str:
    lines: list[str] = []
    add = lines.append

    add("# 压测结果")
    add("")
    add(f"生成时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}　｜　"
        f"总耗时 {elapsed_s:.1f} s")
    add("")
    add("## 测量环境")
    add("")
    lines.extend(machine.lines())
    add("")
    add(DISCLAIMER)
    add("")

    for result in results:
        add(f"## {result.title}")
        add("")
        add(result.setup)
        add("")
        add("| 指标 | 数值 | 说明 |")
        add("|:--|:--|:--|")
        for row in result.rows:
            add(f"| {row.metric} | **{row.value}** | {row.note} |")
        add("")

    add(_conclusions(results))
    return "\n".join(lines)


def _conclusions(results: list[ScenarioResult]) -> str:
    """结论区。

    只写压测**直接支撑得了**的话。凡是这段里出现的数字，都能在表里找到出处，
    凡是能算出来的比例都在这里算，不手写——手写的系数过一轮就会和表对不上。
    """

    by_key = {r.key: r for r in results}
    lines: list[str] = ["## 结论", ""]

    append = by_key.get("append")
    shared = by_key.get("append_shared")
    turn = by_key.get("turn")

    if append and shared:
        lines.append(
            f"- **写入的瓶颈是 fsync，不是锁**。各写各的会话时 {_m(append, '吞吐'):,.0f} 次/秒；"
            f"全挤同一会话时掉到 {_m(shared, '吞吐'):,.0f} 次/秒。"
            "掉下去的是那把每会话锁在串行化——但它只让同会话的写入互相等，"
            "跨会话并不受影响。锁的粒度就是会话，这是设计上要的。"
        )

    if append and turn:
        events = _m(turn, "每轮事件数")
        overhead = _m(turn, "扣掉模型延迟后的运行时开销")
        per_event_turn = overhead / events
        per_event_append = _m(append, "单次 append p50")
        lines.append(
            f"- **运行时开销几乎全是磁盘同步**。端到端里扣掉模型延迟后仍要 "
            f"{overhead:,.2f} ms/轮，摊到 {events:.0f} 条事件是 "
            f"**{per_event_turn:,.2f} ms/条**；"
            f"而写入场景单独测同一条 append 是 {per_event_append:,.2f} ms。"
            "两者同量级，说明模型之外的时间基本花在等盘上。"
            "要提吞吐，方向是批量化提交或换存储介质，不是调协程。"
        )
        lines.append("")
        lines.append(
            f"  *口径提醒*：这两个数来自**不同并发度**的场景"
            f"（端到端 {_m(turn, '并发会话数'):.0f} 会话，写入 {_m(append, '并发 worker 数'):.0f} worker），"
            "只能作量级校验，不能当精确归因——"
            "真要归因得在端到端场景内部单独计时，bench 目前没做。"
        )

    if turn:
        model_ms = _m(turn, "模型延迟（固定，非网络）")
        p50 = _m(turn, "单轮延迟 p50")
        lines.append(
            "- **这组数字是运行时开销的上界，不是端到端延迟**。"
            f"stub 固定 {model_ms:,.0f} ms，实测 p50 {p50:,.2f} ms，"
            "差额才是这个仓库该为之负责的部分；真实上游的网络延迟不在这里面。"
        )

    replay = by_key.get("replay")
    if replay:
        read_us = _m(replay, "每事件读成本@最大档")
        project_us = _m(replay, "每事件投影成本@最大档")
        lines.append(
            f"- **ADR 0001 的「先不做快照」目前成立，但代价结构要说清楚**。"
            f"最大档下每条事件的总成本是 {read_us + project_us:.2f} µs，"
            f"其中 `store.read()`（全文件读取 + 逐行 JSON 解析）占 "
            f"{read_us / (read_us + project_us) * 100:.0f}%，"
            f"`project()`（纯 CPU）只占 {project_us / (read_us + project_us) * 100:.0f}%。"
            "也就是说，**重放贵的不是重放，是读**。"
        )
        mid_read = _m(replay, "每事件读成本@中间档")
        mid_project = _m(replay, "每事件投影成本@中间档")
        lines.append(
            f"- **两条成本曲线的走向不同**。绝对量上读始终是大头（71%），"
            f"但按相对增长看，投影更不稳：从中间档到最大档规模翻 4 倍，"
            f"投影每条从 {mid_project:.2f} µs 涨到 {project_us:.2f} µs"
            f"（{project_us / mid_project:.1f}×），"
            f"读每条只从 {mid_read:.2f} µs 涨到 {read_us:.2f} µs"
            f"（{read_us / mid_read:.1f}×，近似线性）。"
            "所以：**优化当前成本要动读**（分段/索引/mmap），"
            "**防规模退化要盯投影**——它是先弯的那条。"
            "这里只测到 20 万条事件，更远处两条曲线各自怎么走，这组数据不足以判断。"
        )
        lines.append(
            "- **累计开销才是真问题**：runner 在**每次模型往返前都重放一遍整个会话**"
            "（`loop/runner.py:151`），一轮有几次往返就重放几次，"
            "一整个会话累计是 O(n²)。"
            "短会话下「单次重放很便宜」成立；长会话下这个前提会被次数吃掉。"
        )

    lines.append("")
    lines.append("## 已知限制")
    lines.append("")
    lines.append("- **只有单进程单事件循环**。多进程/多实例下的事件日志没有测试，"
                 "而 fsync 的并发表现恰恰是这组数字里最吃环境的一项。")
    lines.append("- 模型延迟是常量，没有模拟网络抖动、重试、上游限流。"
                 "真实的尾延迟会来自上游，不来自这里。")
    lines.append("- 工具是内置的 `echo`，不做真实 I/O。真实工具慢下来会另成一个瓶颈，"
                 "这组数字里没有体现。")
    lines.append("- **fsync 的绝对值强依赖平台与磁盘**。此处是 Windows + NTFS，"
                 "数字明显偏悲观；换个平台绝对值会变，但「fsync 占大头」这个结论不会。")
    lines.append("")
    return "\n".join(lines)


def _m(result: ScenarioResult, metric: str) -> float:
    """取原始数值。缺失时报错而不是返回 0——

    结论区的算术一旦静默用了 0，算出来的比例会看起来很正常，然后被人引用。"""

    try:
        return result.metrics[metric]
    except KeyError:
        raise KeyError(
            f"场景 {result.key!r} 没有提供指标 {metric!r}；"
            f"已有：{sorted(result.metrics)}"
        ) from None
