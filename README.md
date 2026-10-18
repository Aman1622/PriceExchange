# In-Memory Limit Order Book & Trade Matching Engine

A deterministic, high-throughput in-memory Limit Order Book (LOB) and trade matching engine implemented in Python 3.11, featuring Price-Time Priority (FIFO), Self-Trade Prevention (STP), binary Write-Ahead Logging (WAL) with CRC32 checksums, atomic state snapshots, and asynchronous WebSocket market data streaming.

---

## Architecture

```
                       +-----------------------------------+
                       |         Inbound Traffic           |
                       |  (CLI / REST / Matching Ingress)  |
                       +-----------------+-----------------+
                                         |
                                         v
                         +-------------------------------+
                         |        Matching Engine        |
                         | - Sequence Number Generator   |
                         | - Self-Trade Prevention (STP) |
                         | - Order Routing & Matching    |
                         +-------+---------------+-------+
                                 |               |
                 [Pre-Mutation]  |               |  [Event Fan-out]
                                 v               v
        +--------------------------+           +-----------------------------+
        |   Write-Ahead Log (WAL)  |           |      Thread-Safe Queue      |
        | - Binary framing (WAL1)  |           |        (queue.Queue)        |
        | - CRC32 checksums        |           +--------------+--------------+
        | - Configurable fsync     |                          |
        +--------------------------+                          v
                                               +-----------------------------+
                                               |   WebSocket Server Thread   |
                                               | - Dedicated asyncio loop    |
                                               | - /ws/market-data/{symbol}  |
                                               | - /ws/trades/{symbol}       |
                                               +-----------------------------+
                                 |
                                 v
        +--------------------------------------------------------------+
        |                  Order Book (Per Symbol)                     |
        |                                                              |
        |   orders_map: dict[order_id -> Order]   (O(1) direct access) |
        |                                                              |
        |   Bids (SortedDict: Descending)   Asks (SortedDict: Ascend)  |
        |   +--------------------------+    +----------------------+   |
        |   | Price 10100 -> PriceLevel|    | Price 10150 -> Level |   |
        |   | Price 10050 -> PriceLevel|    | Price 10200 -> Level |   |
        |   +--------------------------+    +----------------------+   |
        |                  |                           |               |
        |                  v                           v               |
        |       [Intrusive Doubly-Linked List: FIFO Order Queue]       |
        |       Head (Oldest) <---> Order <---> Tail (Newest)          |
        +--------------------------------------------------------------+
                                 |
                          [Every 60s / Stop]
                                 v
        +--------------------------------------------------------------+
        |                  Snapshot Manager                            |
        |   Write state -> tmp file -> fsync -> os.replace (atomic)   |
        +--------------------------------------------------------------+
```

---

## Data Structures and Complexity

| Operation | Time Complexity | Underlying Mechanism |
| :--- | :--- | :--- |
| **Insert Order (Existing Level)** | $\mathcal{O}(1)$ | Hash table lookup in `SortedDict` + append to doubly-linked list tail |
| **Insert Order (New Level)** | $\mathcal{O}(\log M)$ | $M$ = unique price levels. B-tree bisect in `sortedcontainers.SortedDict` |
| **Cancel Order** | $\mathcal{O}(1)$ avg | $\mathcal{O}(1)$ `orders_map` lookup + $\mathcal{O}(1)$ intrusive unlink ($\mathcal{O}(\log M)$ if level empty) |
| **Modify Order (Reduce Qty)** | $\mathcal{O}(1)$ | In-place volume decrement; retains time priority in FIFO queue |
| **Modify Order (Increase Qty)** | $\mathcal{O}(1)$ | Unlinks order from queue and appends to tail (forfeits time priority) |
| **Best Bid / Best Ask Peek** | $\mathcal{O}(1)$ | Fast boundary access (`bids.peekitem(-1)`, `asks.peekitem(0)`) |
| **L2 Depth Snapshot** | $\mathcal{O}(D)$ | Iterates top $D$ cached volume price levels |
| **Matching Loop Execution** | $\mathcal{O}(K)$ | $K$ = number of matched price levels drained from queue heads |

---

## Durability & Persistence Trade-offs

A limit order book cannot rely exclusively on volatile RAM. The persistence layer balances I/O sync barriers against processing throughput:

1. **Write-Ahead Principle**:
   Records are framed with binary magic bytes (`WAL1`), length, CRC32, sequence ID, and JSON payload, then committed to disk **before** in-memory state is altered.
2. **`fsync="always"` (Strict Durability)**:
   Forces an explicit hardware flush (`os.fsync`) for every single transaction.
   - *Guarantee*: Zero committed transactions lost on sudden power failure.
   - *Throughput*: $\approx 30,300 \text{ ops/sec}$ (bounded by storage controller sync latency: $\approx 32.8 \text{ µs/op}$).
3. **`fsync="batch"` (High Throughput)**:
   Buffers writes in the OS page cache and executes `os.fsync` every $B=100$ records or on periodic timer.
   - *Guarantee*: Up to the last 100 transactions may be lost on physical power loss; zero loss on application-level crashes.
   - *Throughput*: $\approx 270,400 \text{ ops/sec}$ (matching in-memory engine speed: $\approx 3.6 \text{ µs/op}$).
4. **Atomic Snapshots**:
   State checkpoints serialize resting orders to a `.tmp` file, flush and sync, then call POSIX `os.replace`. If a crash occurs mid-write, the existing snapshot remains intact.

---

## Benchmark Results

*Measured on Apple Silicon ARM64, Python 3.11.13, 100,000 deterministic orders (5,000 warm-up + 95,000 timed ops, 65% limit / 25% market / 10% cancel).*

### In-Memory Matching Engine Latency
```
Total time            :      346,504.7 µs  (0.347 s)
Throughput            :        274,167 orders/sec
Mean latency          :          3.580 µs
p50 latency           :          2.875 µs
p90 latency           :          6.750 µs
p99 latency           :         10.083 µs
Max latency           :        531.625 µs
```

### Write-Ahead Log (WAL) Durability Benchmark
```
[Strict Durability (Fsync Always)]
  Throughput :       30,329 ops/sec
  Mean       :        32.87 µs
  p50        :        31.79 µs
  p99        :        50.58 µs

[Amortized Batch (Fsync Batch=100)]
  Throughput :      270,444 ops/sec
  Mean       :         3.63 µs
  p50        :         2.25 µs
  p99        :        37.21 µs
```

---

## Running the Project

### Local Environment
```bash
# 1. Create and activate virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the full test suite (65 tests)
pytest

# 4. Run the latency benchmark
python benchmarks/benchmark_engine.py

# 5. Run the WAL persistence benchmark
python benchmarks/benchmark_wal.py

# 6. Start the matching engine daemon
python src/main.py --symbols AAPL MSFT GOOG --port 8765
```

### Docker & Docker Compose
```bash
# Build and run container with persistent ./data volume mount
docker compose up --build
```
