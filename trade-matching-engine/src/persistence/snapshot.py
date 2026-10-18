"""
snapshot.py
-----------
Atomic state snapshotting and WAL crash recovery for the Limit Order Book.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from src.engine.events import Trade
from src.engine.matching_engine import MatchingEngine
from src.engine.order import Order, Side
from src.persistence.wal import WALReader, WalEventType

logger = logging.getLogger(__name__)


def compute_state_checksum(engine: MatchingEngine) -> str:
    """Compute a deterministic cryptographic SHA-256 digest of engine state.

    Time Complexity:
        O(N) where N is total number of open and historical orders.

    Why it is designed this way:
        Produces a unique bit-level fingerprint of the entire order book state
        (symbols, prices, queues, order remainders, and terminal order statuses).
        Used during snapshot verification and crash recovery tests to prove that
        recovered state is 100% equal to pre-crash state without approximations.
    """
    hasher = hashlib.sha256()

    # Hash open orders in deterministic order (symbol -> side -> price -> FIFO)
    for symbol in sorted(engine._books.keys()):
        book = engine._books[symbol]
        hasher.update(symbol.encode("utf-8"))

        # Bids: descending price order
        for price, level in reversed(book.bids.items()):
            for order in level:
                entry = f"B:{price}:{order.order_id}:{order.qty}:{order.initial_qty}"
                hasher.update(entry.encode("utf-8"))

        # Asks: ascending price order
        for price, level in book.asks.items():
            for order in level:
                entry = f"A:{price}:{order.order_id}:{order.qty}:{order.initial_qty}"
                hasher.update(entry.encode("utf-8"))

    # Hash terminal order history
    for order_id in sorted(engine._order_history.keys()):
        hist_entry = f"H:{order_id}:{engine._order_history[order_id]}"
        hasher.update(hist_entry.encode("utf-8"))

    return hasher.hexdigest()


class SnapshotManager:
    """Manages atomic creation and loading of order book snapshots.

    Time Complexity:
        save_snapshot: O(N) where N is total open orders.
        load_latest_snapshot: O(N) JSON parsing.

    Why it is designed this way:
        Atomic write pattern (temp file -> fsync -> os.replace):
        Writing directly to a target snapshot file risks file corruption if the OS
        crashes mid-write. Writing to a temporary file on the same filesystem, syncing
        to disk, and atomically replacing via POSIX os.replace() guarantees that
        the target snapshot file is always 100% complete and valid.
    """

    def __init__(self, data_dir: Union[str, Path]) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def save_snapshot(
        self,
        engine: MatchingEngine,
        filename: str = "snapshot.json",
    ) -> Path:
        """Create an atomic snapshot of all open orders and sequence state."""
        target_path = self.data_dir / filename

        # Extract all resting orders
        orders_data: List[Dict[str, Any]] = []
        for symbol, book in sorted(engine._books.items()):
            for order in book.orders_map.values():
                orders_data.append({
                    "order_id": order.order_id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "price": order.price,
                    "qty": order.qty,
                    "initial_qty": order.initial_qty,
                    "timestamp": order.timestamp,
                    "participant_id": order.participant_id,
                })

        snapshot_dict = {
            "last_seq": engine.current_seq,
            "checksum": compute_state_checksum(engine),
            "orders": orders_data,
            "order_history": {str(k): v for k, v in engine._order_history.items()},
        }

        # Atomic write: write to temp file, sync, then atomically replace
        temp_file = tempfile.NamedTemporaryFile(
            dir=self.data_dir,
            prefix="snap_",
            suffix=".tmp",
            delete=False,
            mode="w",
            encoding="utf-8",
        )
        try:
            json.dump(snapshot_dict, temp_file, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_file.close()

            # Atomic rename replaces target atomically in POSIX
            os.replace(temp_file.name, target_path)
            logger.info("Saved atomic snapshot to %s (seq=%d)", target_path, engine.current_seq)
            return target_path
        except Exception:
            if os.path.exists(temp_file.name):
                os.unlink(temp_file.name)
            raise

    def load_latest_snapshot(self, filename: str = "snapshot.json") -> Optional[Dict[str, Any]]:
        """Load and parse the snapshot file if present on disk."""
        target_path = self.data_dir / filename
        if not target_path.exists():
            return None

        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data


def recover(
    data_dir: Union[str, Path],
    wal_file: Optional[Union[str, Path]] = None,
    verify_checksum: bool = True,
) -> Tuple[MatchingEngine, int, List[Trade]]:
    """Recover matching engine state from latest snapshot + subsequent WAL records.

    Time Complexity:
        O(S) snapshot load + O(W) WAL replay, where S = open orders, W = WAL records.

    Why it is designed this way:
        Provides fast deterministic crash recovery. Instead of replaying millions of
        historical records from time zero, the engine loads the compact resting order
        snapshot in O(S) and only replays the few uncommitted transactions with
        seq > snapshot_seq.

    Returns:
        Tuple of (recovered_engine, replayed_records_count, emitted_trades)
    """
    data_path = Path(data_dir)
    engine = MatchingEngine(wal_writer=None)
    snap_mgr = SnapshotManager(data_path)
    snapshot = snap_mgr.load_latest_snapshot()

    snapshot_seq = 0
    if snapshot is not None:
        engine._seq = snapshot["last_seq"]
        engine._order_history = {int(k): v for k, v in snapshot["order_history"].items()}

        for od in snapshot["orders"]:
            order = Order(
                order_id=od["order_id"],
                symbol=od["symbol"],
                side=Side.from_str(od["side"]),
                price=od["price"],
                qty=od["qty"],
                timestamp=od.get("timestamp", 0),
                participant_id=od.get("participant_id", ""),
                initial_qty=od.get("initial_qty", od["qty"]),
            )
            engine.get_book(order.symbol).add_order(order)

        if verify_checksum:
            current_cs = compute_state_checksum(engine)
            if current_cs != snapshot["checksum"]:
                raise ValueError(
                    f"Snapshot corrupted: checksum mismatch ({current_cs} != {snapshot['checksum']})"
                )
        snapshot_seq = snapshot["last_seq"]

    # Replay WAL events with seq > snapshot_seq
    target_wal = Path(wal_file) if wal_file else data_path / "engine.wal"
    replayed_count = 0
    emitted_trades: List[Trade] = []

    if target_wal.exists():
        reader = WALReader(target_wal)
        for record in reader:
            if record.seq <= snapshot_seq:
                continue  # Skip events already frozen in the snapshot

            if record.event_type == WalEventType.NEW_ORDER:
                events = engine.submit(record.payload)
                emitted_trades.extend([e for e in events if isinstance(e, Trade)])
                replayed_count += 1
            elif record.event_type == WalEventType.CANCEL:
                engine.cancel(record.payload)
                replayed_count += 1

            if record.seq > engine.current_seq:
                engine._seq = record.seq

    return engine, replayed_count, emitted_trades

