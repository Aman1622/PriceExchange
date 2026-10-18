# SDE-2 Systems Interview Guide: Limit Order Book & Matching Engine

This guide prepares an engineer (especially with a Python/Django or web background) to explain, defend, and deep-dive into this high-performance matching engine in an SDE-2 systems architecture interview.

---

## 1. Project Explanations

### The 60-Second Elevator Pitch
> *"I built a deterministic in-memory Limit Order Book and trade matching engine in Python 3.11 that processes ~274,000 orders per second with a median latency of 2.88 microseconds.
>
> It enforces strict Price-Time priority using a two-tier data structure: prices are indexed in a chunked B-tree (`SortedDict`), while orders at each price level queue in an intrusive doubly-linked list. By embedding intrusive pointers directly onto memory-slotted `Order` instances and maintaining an auxiliary hash map, order cancellations execute in $\mathcal{O}(1)$ time.
>
> For durability, inbound transactions are committed to an append-only binary Write-Ahead Log (WAL) with CRC32 checksums before memory state is mutated. The engine supports atomic state snapshotting via POSIX `os.replace` and guarantees clean recovery even when simulating crashes with torn mid-record writes."*

---

### The 3-Minute Architectural Deep-Dive
> *"Most web engineers are accustomed to stateless architectures backed by relational databases like PostgreSQL. But in financial exchanges, relational databases cannot satisfy the microsecond latency or FIFO time priority requirements of double auctions.
>
> To solve this, I designed a stateful, in-memory matching engine:
>
> 1. **Core Data Structures**:
>    At the heart of the engine is the Order Book. Bids and asks are managed in `SortedDict` structures that maintain sorted price ticks in $\mathcal{O}(\log M)$ time. Within each price level, orders are stored in an intrusive doubly-linked list (`PriceLevel`). Standard Python lists have an $\mathcal{O}(N)$ cost for arbitrary deletions (such as cancellations in the middle of a queue). With our intrusive doubly-linked list and an `orders_map` hash table, any resting order can be located and unlinked in strictly $\mathcal{O}(1)$ time.
>
> 2. **Memory & Cache Optimization**:
>    Python objects typically carry a `__dict__` hash table costing ~150–200 bytes per instance. Using `__slots__` flattens object attributes to fixed memory offsets, cutting per-order memory by ~70% down to ~50 bytes and maximizing CPU L1/L2 cache locality. Prices are strictly positive integer ticks (cents or basis points) to prevent IEEE-754 floating-point inaccuracies.
>
> 3. **The Matching Engine & Priority Rules**:
>    Incoming orders cross against opposite resting liquidity. Aggressive orders execute at the maker's price, granting price improvement. The engine implements Self-Trade Prevention (Cancel-Newest) to reject wash trades, and updates order quantities in-place when size is reduced (preserving queue priority) while moving orders to the tail if size is increased.
>
> 4. **Durability & Crash Recovery**:
>    Because state lives in RAM, persistence is critical. We use a binary Write-Ahead Log with a 12-byte header containing a magic signature, payload length, and CRC32 checksum. Events are committed to the WAL before memory is modified. We benchmarked both strict `fsync="always"` (~30k ops/sec) and amortized `fsync="batch"` (~270k ops/sec). State snapshots are written to temporary files and swapped using atomic POSIX `os.replace`. Recovery loads the latest snapshot, streams the WAL delta, and halts safely if it encounters a torn write from sudden power loss."*

---

## 2. Life of an Order: Walkthrough

### Scenario A: New Resting Limit Order (Passive Maker)
1. **Ingress**: Trader submits `BUY 10 AAPL @ 15000` (price in cents: \$150.00).
2. **Validation**: Engine validates that `qty > 0`, `price > 0`, and `order_id` is unique.
3. **Write-Ahead Logging**: An append-only WAL record is serialized with framing `[WAL1 | length | crc32 | seq=101 | NEW_ORDER | payload]` and committed to disk.
4. **Crossing Check**: Engine checks the best ask (`book.best_ask`). The best ask is `15050` (\$150.50). Since `bid (15000) < best_ask (15050)`, the order does not cross.
5. **Book Placement**:
   - Engine inspects `book.bids[15000]`.
   - If level exists, it appends the order to the doubly-linked list tail in $\mathcal{O}(1)$. If new, it creates the level in $\mathcal{O}(\log M)$.
   - It registers the order in `book.orders_map[order_id]` in $\mathcal{O}(1)$.
6. **Notification**: Emits an `OrderAccepted` event with sequence number 101.

### Scenario B: Aggressive Crossing Order (Active Taker)
1. **Ingress**: Trader submits `BUY 15 AAPL @ 15050`.
2. **WAL Commit**: Written to WAL before matching.
3. **Matching Loop**:
   - Engine sees resting asks at `15050` with resting orders: `Order #1 (qty=10)` and `Order #2 (qty=10)`.
   - Engine inspects `Order #1` at the head of level `15050`.
   - Self-trade check: `incoming.participant_id != maker.participant_id`.
   - Execution price is the **maker's price** (`15050`).
   - Match 1: Fills 10 shares against `Order #1`. `Order #1` is completely filled and unlinked from the book in $\mathcal{O}(1)$. A `Trade` event is emitted.
   - Remaining incoming quantity is 5 shares.
   - Match 2: Engine advances to `Order #2` (now head of level `15050`). Fills 5 shares.
   - `Order #2` quantity is decremented in-place from 10 to 5 via `level.update_order_qty` (retains its head priority). A second `Trade` event is emitted.
4. **Completion**: Incoming order remaining quantity is 0; marked as `FILLED`.

### Scenario C: Cancellation
1. **Ingress**: Trader submits `CANCEL order_id=205`.
2. **WAL Commit**: Cancel intent is logged to WAL.
3. **Lookup & Removal**:
   - Engine queries `book.orders_map.get(205)` in $\mathcal{O}(1)$.
   - Finds resting `Order` reference.
   - Calls `level.remove(order)` in $\mathcal{O}(1)$ via intrusive pointers:
     ```python
     order.prev.next = order.next
     order.next.prev = order.prev
     ```
   - Decrements `level.total_volume` and `level.order_count` in $\mathcal{O}(1)$.
   - If `level.is_empty()`, deletes `15050` from `book.bids` in $\mathcal{O}(\log M)$ to prevent ghost levels.
   - Pops `205` from `orders_map`.
4. **Notification**: Emits `CancelAck` with sequence number and cancelled quantity.

---

## 3. 15 Likely Interview Questions & Strong Answers

### Q1: What are your exact time complexities? Can you defend them?
> **Answer**:
> - **Insert into an existing price level**: $\mathcal{O}(1)$. Key lookup in `SortedDict` is $\mathcal{O}(1)$ average / $\mathcal{O}(\log M)$ worst-case; appending to the doubly-linked list tail is strictly $\mathcal{O}(1)$.
> - **Insert into a new price level**: $\mathcal{O}(\log M)$ where $M$ is the number of distinct price levels (typically a few dozen to a few hundred around the spread).
> - **Cancellation**: $\mathcal{O}(1)$ average. `orders_map` hash lookup is $\mathcal{O}(1)$, intrusive node unlinking is $\mathcal{O}(1)$. If the price level becomes empty, removing the key from `SortedDict` is $\mathcal{O}(\log M)$.
> - **Modify (reduce qty)**: $\mathcal{O}(1)$. Decrements order quantity and price level aggregate volume in-place without moving position.
> - **Modify (increase qty)**: $\mathcal{O}(1)$. Unlinks from current position and appends to tail of the same price level.
> - **Matching**: $\mathcal{O}(K)$ where $K$ is the number of price levels consumed.

### Q2: Why use a doubly-linked list inside each price level instead of Python's built-in `list`?
> **Answer**:
> In an active order book, cancellations and partial fills happen anywhere in the queue—not just at the front or back. A Python `list` is a contiguous dynamic array. Calling `list.remove(order)` or `del list[i]` requires an $\mathcal{O}(N)$ memory shift of all subsequent elements. Under heavy depth (e.g. 5,000 orders at the bid), repeated cancellations would degrade matching performance to $\mathcal{O}(N)$. A doubly-linked list gives guaranteed $\mathcal{O}(1)$ deletion anywhere in the queue when given the node reference.

### Q3: What is an "intrusive" linked list, and why is it better than a standard linked list?
> **Answer**:
> In a traditional linked list, you allocate a wrapper object `Node` that points to the payload: `node.value = order`. This creates two objects per order, doubling heap allocations, doubling GC overhead, and causing pointer chasing across fragmented memory.
> In an **intrusive** linked list, the `Order` struct *itself* contains `prev` and `next` pointers. The order *is* the node. This eliminates wrapper allocations and allows direct $\mathcal{O}(1)$ unlinking directly from the `Order` instance returned by `orders_map[order_id]`.

### Q4: Why did you use `__slots__`? What are the actual trade-offs?
> **Answer**:
> By default, Python class instances store attributes in an internal dictionary (`__dict__`). This costs ~150–200 bytes per object and requires hash lookups for attribute access.
> By defining `__slots__`, Python allocates a fixed array of pointers directly on the C-level object struct, eliminating `__dict__` and `__weakref__`.
> - **Pros**: Cuts memory consumption from ~150 bytes to ~50 bytes per order (~70% reduction) and speeds up attribute access.
> - **Cons/Trade-offs**: Prevents dynamically attaching arbitrary attributes at runtime and complicates multiple inheritance. In a high-performance matching engine, this rigidity is an advantage.

### Q5: How does Python's Global Interpreter Lock (GIL) affect this engine?
> **Answer**:
> The GIL permits only one OS thread to execute Python bytecode at any given moment. In our engine:
> - The core matching loop and WAL writing run synchronously on the main thread to preserve strict deterministic event ordering.
> - The WebSocket fan-out server runs on its own background thread with its own `asyncio` event loop.
> - To prevent WebSocket network I/O from blocking the matching engine, communication occurs across an unbounded thread-safe `queue.Queue`.
> - However, under high CPU load, GIL contention between the WebSocket JSON serialization thread and the matching engine thread can introduce latency spikes. In production, we would run WebSocket broadcasting in a separate OS process or external gateway (e.g., in Go or Rust) consuming from IPC shared memory or a ring buffer.

### Q6: What is the real cost of `os.fsync()`, and why is "zero data loss" a dangerous claim?
> **Answer**:
> When a program calls `file.write()`, bytes are only copied to the operating system's page cache in volatile RAM. If the machine loses power 5 milliseconds later, those bytes are permanently lost.
> Calling `os.fsync(fd)` forces the disk controller to flush its cache down to non-volatile physical storage (NAND flash or platter).
> On modern NVMe SSDs, an `fsync` barrier takes between 30 µs and 2,000 µs. In our benchmarks, strict `fsync="always"` capped throughput at ~30,300 ops/sec (~32.9 µs latency). Claiming "sub-microsecond latency with zero data loss on a single node" is physically impossible on current hardware. We document the honest trade-off: `always` for strict durability (30k ops/sec) vs. `batch` for high throughput (270k ops/sec with bounded crash window).

### Q7: Explain your crash recovery mechanism.
> **Answer**:
> Crash recovery uses the **Checkpoint + WAL Replay** pattern:
> 1. The engine periodically (e.g. every 60s) takes an atomic snapshot of all open orders, sequence numbers, and order history.
> 2. On restart after a crash, `recover()` loads `snapshot.json` and restores the resting order books in $\mathcal{O}(\text{resting orders})$, setting `seq = snapshot['last_seq']`.
> 3. It streams the binary WAL file using `WALReader`. Any records with `seq <= snapshot_seq` are discarded (already baked into snapshot).
> 4. Transactions with `seq > snapshot_seq` are replayed through the engine in deterministic sequence order.
> 5. Recovery time is bounded by the resting book size plus the uncompacted WAL delta, rather than the entire historical trading volume.

### Q8: What is a "torn write", and how do you prevent it from corrupting the engine?
> **Answer**:
> A torn write occurs when power cuts or the operating system crashes while the disk controller is halfway through writing a record. For example, a 120-byte record might only have 40 bytes flushed to disk.
> We prevent corruption using binary framing and CRC32 verification:
> - Every WAL entry has a fixed 12-byte header: `[magic (4B) | length (4B) | crc32 (4B)]`.
> - When `WALReader` processes the file, it reads the header, validates the magic bytes (`b'WAL1'`), and reads exactly `length` bytes.
> - It calculates `zlib.crc32(body)` and compares it against `header.crc32`.
> - If the file ends prematurely or the CRC does not match, `WALReader` flags a torn write, halts replay immediately, and reports the last safe sequence number. It never passes corrupt bytes into the engine.

### Q9: Why are prices integer ticks instead of floats or `decimal.Decimal`?
> **Answer**:
> 1. **Floating-point drift**: IEEE-754 floating-point numbers cannot exactly represent most decimal fractions (e.g., `0.1 + 0.2 = 0.30000000000000004`). In an exchange, floating-point rounding errors lead to phantom cash discrepancies and failed settlement balancing.
> 2. **Performance**: Python's `decimal.Decimal` is an arbitrary-precision software decimal implementation. It is orders of magnitude slower than native integer operations and allocates heap objects.
> 3. **Integer ticks**: Real exchanges represent prices as integer multiples of the minimum tick size (e.g. 1 tick = 1 cent or 1/10th of a cent). Integer comparisons and hashing are exact, deterministic, and execute in single-cycle machine instructions.

### Q10: Why does every event receive a monotonically increasing sequence number?
> **Answer**:
> Sequence numbers establish an unambiguous, total order across all system events.
> 1. **Deterministic Replay**: Because the matching engine is single-threaded and deterministic, replaying events with sequence numbers $1 \dots N$ will always yield the identical order book state and trade sequence.
> 2. **Client Reconciliation**: WebSocket clients can inspect the sequence ID on market data updates. If a client receives sequence 104 after sequence 101, it instantly knows it dropped 2 messages and can request a snapshot refresh.

### Q11: How does Self-Trade Prevention (STP) work, and why Cancel-Newest?
> **Answer**:
> Self-trading (wash trading) occurs when the same market participant submits orders on both sides of the book that match against each other, creating artificial volume and violating securities regulations.
> When an incoming order matches against a resting order, the engine checks `if incoming.participant_id == resting.participant_id`.
> We implement **Cancel-Newest (Cancel Taker)**: the incoming order's remaining quantity is immediately cancelled with an `OrderRejected` event.
> - *Why this choice*: Other market participants in the market may already be evaluating or crossing against the resting maker order. Cancelling resting liquidity creates market instability and punishes passive liquidity providers. Cancel-Newest penalizes the aggressive taker for submitting a conflicting order, leaves the resting book untouched, and executes in $\mathcal{O}(1)$ time.

### Q12: Why does reducing order quantity retain time priority, while increasing quantity moves the order to the tail?
> **Answer**:
> This is universal exchange protocol (e.g. NASDAQ, CME).
> - **Quantity Reduction**: Reducing order size does not disadvantage any trader queued behind you; it only decreases the volume ahead of them. Therefore, you retain your FIFO position.
> - **Quantity Increase**: If a participant could increase size while keeping time priority, they could post 1 share at the open, wait for the market to move, and increase to 100,000 shares ahead of everyone else. Increasing size is economically equivalent to posting a new order, so the engine unlinks the order and appends it to the tail.

### Q13: How would you scale this architecture to hundreds of symbols or multi-core machines?
> **Answer**:
> Because individual stock symbols (e.g., AAPL vs. MSFT) are completely independent markets, an order in AAPL never matches against an order in MSFT.
> 1. **Symbol Partitioning (Sharding)**: We can partition symbols across multiple independent engine processes or worker threads (e.g., Process 1 handles A–E, Process 2 handles F–J). Each process runs its own single-threaded matching loop, WAL writer, and book instances with zero lock contention.
> 2. **API Gateway / Router**: An ingress routing tier hashes incoming orders by symbol and dispatches them via IPC (e.g. Unix domain sockets or shared-memory ring buffers) to the corresponding symbol worker.

### Q14: What happens if a snapshot file is corrupted?
> **Answer**:
> In `snapshot.py`, every snapshot includes an embedded SHA-256 state checksum.
> 1. When `recover()` loads `snapshot.json`, it calculates the checksum over the reconstructed in-memory state and verifies that it matches `snapshot['checksum']`.
> 2. If the checksum fails (e.g. disk bit-rot or manual tampering), `recover()` rejects the snapshot and falls back to full replay from the beginning of the WAL.
> 3. Furthermore, because snapshots are written using atomic `os.replace`, partial writes never overwrite a valid snapshot file.

### Q15: If you were rewriting this from scratch in Go or C++, what would you design differently?
> **Answer**:
> 1. **Zero-Allocation Memory Arena**: In C++ or Go, we would pre-allocate a contiguous memory arena (`Order` pool). Reusing pre-allocated struct pointers avoids all runtime memory allocation and garbage collection pauses.
> 2. **LMAX Disruptor Pattern**: Instead of Python's `queue.Queue` (which uses threading locks and condition variables), we would use lock-free ring buffers with cache-line padded sequence pointers to pass events from network ingress to the matching core.
> 3. **Kernel Bypass / DPDK / Solarflare OpenOnload**: Instead of standard TCP/WebSocket sockets through the Linux kernel stack, we would use kernel-bypass networking to receive network packets directly in user-space memory, reducing network transit latency from microseconds to sub-microsecond levels.

---

## 4. Honest Limitations & How to Fix Them

### Limitation 1: Python GIL & Single-Core CPU Saturation
- **Reality**: While our in-memory engine achieves ~274k ops/sec on one core, the Python GIL prevents scaling matching throughput across multiple CPU cores within the same process.
- **Production Fix**: Shard by instrument across independent OS processes using a multi-process architecture, or rewrite the core matching loop in C++/Rust and expose it via C-bindings (`ctypes` or `pyo3`).

### Limitation 2: Memory Bounded by Physical RAM
- **Reality**: Because all resting orders reside in RAM, an extreme surge of resting liquidity (millions of quotes from high-frequency market makers) can exhaust node RAM.
- **Production Fix**: Implement participant quoting limits (max open orders per trader) and implement Level 3 offloading where deep orders far away from the spread (>5% out of the money) are compressed or persisted to local NVMe storage until the spread approaches them.

### Limitation 3: Thread-Safe Queue Contention
- **Reality**: Python's `queue.Queue` uses an underlying `threading.Lock()` with mutex acquire/release overhead on every event pushed to the WebSocket server.
- **Production Fix**: Replace `queue.Queue` with a lock-free single-producer single-consumer (SPSC) circular ring buffer or publish via IPC / shared memory.

