"""
benchmark_wal.py
----------------
Benchmarks the Write-Ahead Log (WAL) under 'always' and 'batch' fsync modes
to measure the real durability vs throughput trade-off.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.persistence.wal import WALWriter, WalEventType


def benchmark_mode(name: str, fsync_mode: str, num_records: int, batch_size: int = 100):
    tmp_dir = tempfile.mkdtemp(prefix="wal_bench_")
    wal_path = os.path.join(tmp_dir, f"bench_{fsync_mode}.wal")

    sample_payload = {
        "order_id": 12345,
        "symbol": "AAPL",
        "side": "BUY",
        "price": 15000,
        "qty": 100,
        "participant_id": "FIRM_A",
    }

    latencies_ns: list[int] = []

    try:
        with WALWriter(wal_path, fsync_mode=fsync_mode, batch_size=batch_size) as writer:
            # Warm-up (discard 100 writes)
            for i in range(100):
                writer.write_record(i, WalEventType.NEW_ORDER, sample_payload)

            start_t = time.perf_counter_ns()
            for i in range(num_records):
                t0 = time.perf_counter_ns()
                writer.write_record(i + 100, WalEventType.NEW_ORDER, sample_payload)
                t1 = time.perf_counter_ns()
                latencies_ns.append(t1 - t0)
            end_t = time.perf_counter_ns()

        total_s = (end_t - start_t) / 1_000_000_000
        ops_per_sec = num_records / total_s if total_s > 0 else float("inf")
        latencies_us = sorted(v / 1_000 for v in latencies_ns)
        mean_us = sum(latencies_us) / len(latencies_us)
        p50_us = latencies_us[int(len(latencies_us) * 0.50)]
        p99_us = latencies_us[int(len(latencies_us) * 0.99)]
        max_us = max(latencies_us)

        print(f"[{name.upper()}] Mode: fsync='{fsync_mode}' (N={num_records:,})")
        print(f"  Throughput : {ops_per_sec:>12,.0f} ops/sec")
        print(f"  Mean       : {mean_us:>12.2f} µs")
        print(f"  p50        : {p50_us:>12.2f} µs")
        print(f"  p99        : {p99_us:>12.2f} µs")
        print(f"  Max        : {max_us:>12.2f} µs")
        print("-" * 50)
        return {
            "mode": fsync_mode,
            "ops_per_sec": ops_per_sec,
            "mean_us": mean_us,
            "p50_us": p50_us,
            "p99_us": p99_us,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run():
    print("=" * 50)
    print("WAL Durability vs Throughput Benchmark")
    print("=" * 50)
    # 'always' mode: 2,000 records (each triggers an OS fsync barrier)
    benchmark_mode("Strict Durability (Fsync Always)", "always", num_records=2_000)
    # 'batch' mode: 50,000 records (fsync every 100 records)
    benchmark_mode("Amortized Batch (Fsync Batch=100)", "batch", num_records=50_000, batch_size=100)
    print("=" * 50)


if __name__ == "__main__":
    run()

