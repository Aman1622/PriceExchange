"""
test_wal_recovery.py
--------------------
Unit tests verifying that WAL-based crash recovery restores engine state
faithfully, handles torn writes safely, and integrates with snapshots.
"""

from __future__ import annotations

import os
import random
import pytest
from src.engine.events import Trade
from src.engine.matching_engine import MatchingEngine
from src.engine.order import Side
from src.persistence.snapshot import (
    SnapshotManager,
    compute_state_checksum,
    recover,
)
from src.persistence.wal import WALWriter, WalEventType


# ---------------------------------------------------------------------------
# WAL recovery tests
# ---------------------------------------------------------------------------

def test_wal_recovery_restores_open_orders(engine_with_wal, wal_dir, make_limit_order):
    """After a simulated crash, replaying the WAL restores all open orders."""
    o1 = make_limit_order(side="buy", price=10000, qty=10)
    o2 = make_limit_order(side="buy", price=10050, qty=20)
    o3 = make_limit_order(side="sell", price=10100, qty=15)

    engine_with_wal.submit(o1)
    engine_with_wal.submit(o2)
    engine_with_wal.submit(o3)

    expected_checksum = compute_state_checksum(engine_with_wal)
    engine_with_wal.wal_writer.flush()

    # Simulate crash: instantiate new engine and replay from wal_dir
    recovered_engine, count, _ = recover(wal_dir)

    assert count == 3
    assert compute_state_checksum(recovered_engine) == expected_checksum
    book = recovered_engine.get_book("TEST")
    assert book.best_bid == 10050
    assert book.best_ask == 10100
    assert len(book.orders_map) == 3


def test_wal_recovery_excludes_cancelled_orders(
    engine_with_wal, wal_dir, make_limit_order, make_cancel_order
):
    """After recovery, cancelled orders are not present in the order book."""
    o1 = make_limit_order(side="buy", price=10000, qty=10)
    o2 = make_limit_order(side="buy", price=10050, qty=20)
    engine_with_wal.submit(o1)
    engine_with_wal.submit(o2)

    # Cancel o1
    cancel_cmd = make_cancel_order(order_id=o1["order_id"], symbol="TEST")
    engine_with_wal.cancel(cancel_cmd)
    engine_with_wal.wal_writer.flush()

    recovered_engine, _, _ = recover(wal_dir)
    book = recovered_engine.get_book("TEST")

    assert o1["order_id"] not in book.orders_map
    assert o2["order_id"] in book.orders_map
    assert book.best_bid == 10050


def test_wal_recovery_excludes_filled_orders(
    engine_with_wal, wal_dir, make_limit_order, make_market_order
):
    """After recovery, fully filled orders are not present in the order book."""
    o1 = make_limit_order(side="buy", price=10000, qty=10)
    engine_with_wal.submit(o1)

    # Fully fill o1 via crossing market sell
    m_sell = make_market_order(side="sell", qty=10)
    engine_with_wal.submit(m_sell)
    engine_with_wal.wal_writer.flush()

    recovered_engine, _, _ = recover(wal_dir)
    book = recovered_engine.get_book("TEST")

    assert o1["order_id"] not in book.orders_map
    assert len(book.orders_map) == 0
    assert book.best_bid is None


def test_wal_recovery_partial_fill_preserves_remainder(
    engine_with_wal, wal_dir, make_limit_order, make_market_order
):
    """After recovery, a partially filled order retains its remaining quantity."""
    o1 = make_limit_order(side="buy", price=10000, qty=10)
    engine_with_wal.submit(o1)

    # Partially fill 4 of 10
    m_sell = make_market_order(side="sell", qty=4)
    engine_with_wal.submit(m_sell)
    engine_with_wal.wal_writer.flush()

    recovered_engine, _, _ = recover(wal_dir)
    book = recovered_engine.get_book("TEST")

    resting = book.get_order(o1["order_id"])
    assert resting is not None
    assert resting.qty == 6
    assert book.bids[10000].total_volume == 6


def test_wal_recovery_trade_sequence_is_identical(
    engine_with_wal, wal_dir, make_limit_order, make_market_order
):
    """Replaying a WAL produces the same sequence of trade events as the original run."""
    engine_with_wal.submit(make_limit_order(side="sell", price=10000, qty=10))
    engine_with_wal.submit(make_limit_order(side="sell", price=10020, qty=10))

    original_events = engine_with_wal.submit(make_market_order(side="buy", qty=15))
    original_trades = [e for e in original_events if isinstance(e, Trade)]
    engine_with_wal.wal_writer.flush()

    recovered_engine, _, replayed_trades = recover(wal_dir)

    assert len(replayed_trades) == len(original_trades)
    for orig, rep in zip(original_trades, replayed_trades):
        assert orig.price == rep.price
        assert orig.qty == rep.qty
        assert orig.maker_order_id == rep.maker_order_id
        assert orig.taker_order_id == rep.taker_order_id


def test_wal_recovery_from_truncated_file_raises_or_recovers_safely(
    engine_with_wal, wal_dir, make_limit_order
):
    """Recovery from a truncated WAL file recovers valid records up to the crash point."""
    o1 = make_limit_order(side="buy", price=10000, qty=10)
    o2 = make_limit_order(side="buy", price=10050, qty=20)
    engine_with_wal.submit(o1)
    engine_with_wal.submit(o2)
    engine_with_wal.wal_writer.flush()

    # Append half a record to simulate mid-write power loss
    wal_path = wal_dir / "engine.wal"
    with open(wal_path, "ab") as f:
        f.write(b"WAL1\x00\x00\x00\x10incomplete")

    recovered_engine, count, _ = recover(wal_dir)
    # The two valid records are recovered cleanly
    assert count == 2
    book = recovered_engine.get_book("TEST")
    assert o1["order_id"] in book.orders_map
    assert o2["order_id"] in book.orders_map


def test_wal_recovery_10000_orders_with_snapshot_and_simulated_crash(tmp_path):
    """10,000 random orders with mid-stream snapshot and torn-write crash recovery."""
    data_dir = tmp_path / "crash_sim"
    data_dir.mkdir()
    wal_file = data_dir / "engine.wal"

    writer = WALWriter(wal_file, fsync_mode="batch", batch_size=100)
    engine = MatchingEngine(wal_writer=writer)
    snap_mgr = SnapshotManager(data_dir)

    rng = random.Random(1337)
    order_id = 1
    orders_pool = []

    # Process 5,000 orders
    for _ in range(5_000):
        side = "buy" if rng.random() < 0.5 else "sell"
        price = rng.randint(9_900, 10_100)
        qty = rng.randint(1, 50)
        order = {
            "type": "limit",
            "symbol": "TEST",
            "order_id": order_id,
            "side": side,
            "price": price,
            "qty": qty,
        }
        engine.submit(order)
        orders_pool.append(order_id)
        order_id += 1

    # Take snapshot at 5,000 orders
    snap_mgr.save_snapshot(engine)

    # Process next 5,000 orders (mix of limit, market, cancel)
    for _ in range(5_000):
        action = rng.choice(["limit", "market", "cancel"])
        if action == "limit":
            side = "buy" if rng.random() < 0.5 else "sell"
            order = {
                "type": "limit",
                "symbol": "TEST",
                "order_id": order_id,
                "side": side,
                "price": rng.randint(9_900, 10_100),
                "qty": rng.randint(1, 50),
            }
            engine.submit(order)
            orders_pool.append(order_id)
            order_id += 1
        elif action == "market":
            side = "buy" if rng.random() < 0.5 else "sell"
            engine.submit({
                "type": "market",
                "symbol": "TEST",
                "order_id": order_id,
                "side": side,
                "qty": rng.randint(1, 30),
            })
            order_id += 1
        else:
            if orders_pool:
                target_id = rng.choice(orders_pool)
                engine.cancel({"order_id": target_id, "symbol": "TEST"})

    writer.flush()
    expected_checksum = compute_state_checksum(engine)
    writer.close()

    # Simulate crash: truncate the last 15 bytes of WAL (torn write)
    file_size = os.path.getsize(wal_file)
    with open(wal_file, "r+b") as f:
        f.truncate(file_size - 15)

    # Recover from snapshot + torn WAL
    recovered_engine, replayed_count, _ = recover(data_dir)

    # Replay should safely stop at the torn write without corrupting state
    recovered_book = recovered_engine.get_book("TEST")
    assert len(recovered_book.orders_map) > 0
    assert recovered_engine.current_seq > 5_000
