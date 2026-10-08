"""计时与统计。

压测的结论全靠这里的口径，所以口径要写死、要统一：

- 报 p50/p95/p99，不报平均值。平均值会被长尾藏住，而长尾才是并发场景的痛点。
- 每个场景重复多轮，取中位数。单轮结果会被机器上别的东西干扰。
- 先跑一轮预热再计时。第一次跑要付 import、页缓存、连接建立的钱，
  那不是稳态性能。
"""

from __future__ import annotations

import gc
import math
import os
import platform
import statistics
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field


def percentile(sorted_values: Sequence[float], p: float) -> float:
    """线性插值分位数。``sorted_values`` 必须已升序。"""

    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * p
    low, high = math.floor(k), math.ceil(k)
    if low == high:
        return float(sorted_values[int(k)])
    return float(sorted_values[low] * (high - k) + sorted_values[high] * (k - low))


@dataclass(slots=True)
class Sample:
    """一组耗时样本，单位毫秒。"""

    label: str
    values_ms: list[float] = field(default_factory=list)

    def add(self, ms: float) -> None:
        self.values_ms.append(ms)

    def extend(self, values: Sequence[float]) -> None:
        self.values_ms.extend(values)

    @property
    def count(self) -> int:
        return len(self.values_ms)

    @property
    def median(self) -> float:
        return statistics.median(self.values_ms) if self.values_ms else float("nan")

    def summary(self) -> dict[str, float]:
        ordered = sorted(self.values_ms)
        return {
            "n": float(len(ordered)),
            "p50": percentile(ordered, 0.50),
            "p95": percentile(ordered, 0.95),
            "p99": percentile(ordered, 0.99),
            "max": float(ordered[-1]) if ordered else float("nan"),
            "mean": statistics.fmean(ordered) if ordered else float("nan"),
        }


async def timed(coro: Awaitable[object]) -> float:
    start = time.perf_counter()
    await coro
    return (time.perf_counter() - start) * 1000.0


def time_call(fn: Callable[[], object], *, repeats: int = 1) -> float:
    """同步函数计时，单位毫秒。取 ``repeats`` 次里的最小值——

    最小值最接近「这段代码本身的成本」，不会被机器上的噪声抬高。
    """

    best = math.inf
    for _ in range(max(1, repeats)):
        start = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - start) * 1000.0)
    return best


def warmup() -> None:
    """预热，并把上一阶段留下的垃圾回收掉，避免 GC 落在计时窗口里。"""

    gc.collect()


@dataclass(frozen=True, slots=True)
class Machine:
    os: str
    cpu: str
    cores: int
    python: str

    @classmethod
    def current(cls) -> Machine:
        return cls(
            os=f"{platform.system()} {platform.release()}",
            cpu=platform.processor() or "unknown",
            cores=os.cpu_count() or 0,
            python=sys.version.split()[0],
        )

    def lines(self) -> list[str]:
        return [
            f"- OS: {self.os}",
            f"- CPU: {self.cpu}",
            f"- 逻辑核心数: {self.cores}",
            f"- Python: {self.python}",
        ]
