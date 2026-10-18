# Resume Bullets

Use these tailored resume bullets highlighting real measured metrics from Stages 4 and 5:

---

### Option 1 (Systems & High-Throughput Focus)
- **Engineered an in-memory Limit Order Book and trade matching engine in Python 3.11 achieving 274,167 orders/sec throughput and 2.88 µs median latency (p99 of 10.08 µs) across a 100,000-order benchmark workload.** Designed an intrusive doubly-linked list with memory-slotted `__slots__` structures and `SortedDict` to deliver $\mathcal{O}(1)$ cancellations, price-time priority, and Self-Trade Prevention.

### Option 2 (Persistence & Reliability Focus)
- **Architected a high-performance binary Write-Ahead Log (WAL) and crash-recovery engine featuring CRC32 checksum framing, atomic snapshotting via POSIX `os.replace`, and torn-write detection.** Benchmarked persistence trade-offs, achieving 30,329 ops/sec (32.87 µs mean latency) under strict synchronous `fsync` and 270,444 ops/sec (3.63 µs mean latency) under amortized batching.

### Option 3 (Full-Stack Exchange Architecture Focus)
- **Built a deterministic exchange trading engine and streaming market data system with sub-microsecond in-memory matching and real-time WebSocket fan-out.** Integrated atomic snapshot checkpointing and WAL replay that restored state consistency across a 10,000-order crash simulation while isolating WebSocket broadcast I/O on a dedicated background event loop.

