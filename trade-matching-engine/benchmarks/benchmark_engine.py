"""
benchmark_engine.py
-------------------
Latency benchmark for the matching engine.

Methodology
-----------
* Pre-generates 100,000 orders (limit / market / cancel) from a fixed random
  seed so results are reproducible across runs.
* Prices are integer ticks (no floating-point arithmetic in order generation).
* Runs a 5,000-order warm-up phase (JIT / branch-predictor warm-up) whose
  timings are discarded.
* Times every subsequent call individually with ``time.perf_counter_ns``.
* Prints: total time, throughput (orders/sec), mean, p50, p90, p99, and max
  latency – all in microseconds.

Usage
-----
    python benchmarks/benchmark_engine.py

Note: ``build_engine()`` is intentionally left as a TODO stub.  Fill it in
once the engine is implemented.
"""

from __future__ import annotations

import os
import random
import sys
import time
from typing import Any

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Engine stub – fill this in once the engine module is ready
# ---------------------------------------------------------------------------

def build_engine() -> Any:
    """Return a fully-initialised matching engine instance with the BENCH book."""
    from src.engine import MatchingEngine
    engine = MatchingEngine()
    engine.create_book(_SYMBOL)
    return engine


# ---------------------------------------------------------------------------
# Order generation
# ---------------------------------------------------------------------------

# Seed ensures reproducible benchmarks across runs.
_RNG_SEED = 42

# Tick range for limit-order prices (integer ticks, e.g. cents or basis points)
_MIN_TICK = 9_000
_MAX_TICK = 11_000

_SYMBOL = "BENCH"
_TOTAL_ORDERS = 100_000
_WARMUP_ORDERS = 5_000


def _generate_orders(n: int, seed: int) -> list[dict]:
    """Return *n* randomised order dicts with integer tick prices."""
    rng = random.Random(seed)
    order_types = ("limit", "market", "cancel")
    # Weights: mostly limit orders, some market, few cancels
    weights = (0.65, 0.25, 0.10)

    orders: list[dict] = []
    order_id_counter = 1

    for _ in range(n):
        kind = rng.choices(order_types, weights=weights, k=1)[0]
        side = rng.choice(("buy", "sell"))
        qty = rng.randint(1, 100)

        if kind == "limit":
            orders.append(
                {
                    "type": "limit",
                    "symbol": _SYMBOL,
                    "order_id": order_id_counter,
                    "side": side,
                    "price": rng.randint(_MIN_TICK, _MAX_TICK),  # integer ticks
                    "qty": qty,
                }
            )
            order_id_counter += 1
        elif kind == "market":
            orders.append(
                {
                    "type": "market",
                    "symbol": _SYMBOL,
                    "order_id": order_id_counter,
                    "side": side,
                    "qty": qty,
                }
            )
            order_id_counter += 1
        else:  # cancel
            # Cancel a random earlier order (may or may not exist – engine handles it)
            cancel_target = rng.randint(1, max(1, order_id_counter - 1))
            orders.append(
                {
                    "type": "cancel",
                    "symbol": _SYMBOL,
                    "order_id": cancel_target,
                }
            )

    return orders


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def _percentile(sorted_values: list[int], pct: float) -> float:
    """Return the *pct* percentile (0-100) of a pre-sorted list."""
    if not sorted_values:
        return 0.0
    idx = (pct / 100.0) * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def run_benchmark() -> None:
    print("=" * 60)
    print("Trade Matching Engine – Latency Benchmark")
    print("=" * 60)
    print(f"Generating {_TOTAL_ORDERS:,} orders (seed={_RNG_SEED}, symbol={_SYMBOL})…")

    all_orders = _generate_orders(_TOTAL_ORDERS, seed=_RNG_SEED)
    warmup_orders = all_orders[:_WARMUP_ORDERS]
    bench_orders = all_orders[_WARMUP_ORDERS:]

    print("Building engine…")
    engine = build_engine()
    print("Building engine… done.")

    # ── Warm-up ────────────────────────────────────────────────────────────
    print(f"Running {_WARMUP_ORDERS:,}-order warm-up (timings discarded)…")
    for order in warmup_orders:
        engine.submit(order)

    # ── Timed benchmark ────────────────────────────────────────────────────
    n = len(bench_orders)
    print(f"Timing {n:,} orders…")

    latencies_ns: list[int] = []
    bench_start = time.perf_counter_ns()

    for order in bench_orders:
        t0 = time.perf_counter_ns()
        engine.submit(order)
        t1 = time.perf_counter_ns()
        latencies_ns.append(t1 - t0)

    bench_end = time.perf_counter_ns()

    # ── Statistics ─────────────────────────────────────────────────────────
    total_ns = bench_end - bench_start
    total_us = total_ns / 1_000
    total_s = total_ns / 1_000_000_000
    orders_per_sec = n / total_s if total_s > 0 else float("inf")

    latencies_us = sorted(v / 1_000 for v in latencies_ns)
    mean_us = sum(latencies_us) / len(latencies_us) if latencies_us else 0.0

    print()
    print(f"{'Total time':<22}: {total_us:>14,.1f} µs  ({total_s:.3f} s)")
    print(f"{'Throughput':<22}: {orders_per_sec:>14,.0f} orders/sec")
    print(f"{'Mean latency':<22}: {mean_us:>14.3f} µs")
    print(f"{'p50 latency':<22}: {_percentile(latencies_us, 50):>14.3f} µs")
    print(f"{'p90 latency':<22}: {_percentile(latencies_us, 90):>14.3f} µs")
    print(f"{'p99 latency':<22}: {_percentile(latencies_us, 99):>14.3f} µs")
    print(f"{'Max latency':<22}: {max(latencies_us, default=0.0):>14.3f} µs")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_benchmark()

