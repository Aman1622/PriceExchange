"""
test_edge_cases.py
------------------
Unit tests for boundary conditions, invalid inputs, self-trade prevention,
duplicate IDs, and rapid state churn.
"""

from __future__ import annotations

import sys
import pytest
from src.engine.events import CancelReject, OrderRejected, Trade
from src.engine.order import Side


# ---------------------------------------------------------------------------
# Input validation – zero quantity
# ---------------------------------------------------------------------------

def test_zero_qty_order_is_rejected(engine, make_limit_order):
    """An order submitted with qty=0 must be rejected before touching the book.

    The engine should return an OrderRejected event; book state must remain empty.
    """
    order = make_limit_order(price=10000, qty=0)
    events = engine.submit(order)

    assert len(events) == 1
    assert isinstance(events[0], OrderRejected)
    assert "positive" in events[0].reason.lower()

    book = engine.get_book("TEST")
    assert len(book) == 0


def test_zero_qty_cancel_is_rejected(engine, make_cancel_order):
    """A cancel message with qty=0 (if qty is present) is rejected."""
    cancel_cmd = make_cancel_order(order_id=1, symbol="TEST")
    cancel_cmd["qty"] = 0
    res = engine.cancel(cancel_cmd)

    assert isinstance(res, CancelReject)
    assert "positive" in res.reason.lower()


# ---------------------------------------------------------------------------
# Input validation – negative or zero price
# ---------------------------------------------------------------------------

def test_negative_price_limit_order_is_rejected(engine, make_limit_order):
    """A limit order with a negative tick price must be rejected."""
    order = make_limit_order(price=-100, qty=10)
    events = engine.submit(order)

    assert len(events) == 1
    assert isinstance(events[0], OrderRejected)
    assert "price" in events[0].reason.lower()

    book = engine.get_book("TEST")
    assert len(book) == 0


def test_zero_price_limit_order_is_rejected(engine, make_limit_order):
    """A limit order with price=0 must be rejected.

    Zero is not a valid price (prices are strictly positive integer ticks).
    """
    order = make_limit_order(price=0, qty=10)
    events = engine.submit(order)

    assert len(events) == 1
    assert isinstance(events[0], OrderRejected)
    assert "price" in events[0].reason.lower()

    book = engine.get_book("TEST")
    assert len(book) == 0


# ---------------------------------------------------------------------------
# Self-trade prevention
# ---------------------------------------------------------------------------

def test_self_trade_same_participant_id_is_prevented(engine, make_limit_order):
    """Two orders from the same participant that would cross must not match.

    STP Cancel-Newest cancels the incoming order's remaining quantity and
    preserves the resting maker order untouched.
    """
    maker = make_limit_order(side="buy", price=10000, qty=10)
    maker["participant_id"] = "FIRM_A"
    engine.submit(maker)

    # Same participant attempts to cross with sell
    taker = make_limit_order(side="sell", price=10000, qty=10)
    taker["participant_id"] = "FIRM_A"
    events = engine.submit(taker)

    # Must reject taker order with STP notice; zero trades executed
    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 0

    rejects = [e for e in events if isinstance(e, OrderRejected)]
    assert len(rejects) == 1
    assert "self-trade" in rejects[0].reason.lower()

    # Maker order must still be resting intact on the book
    book = engine.get_book("TEST")
    resting_maker = book.get_order(maker["order_id"])
    assert resting_maker is not None
    assert resting_maker.qty == 10


def test_self_trade_different_participants_is_allowed(engine, make_limit_order):
    """Crossing orders from different participants must match normally."""
    maker = make_limit_order(side="buy", price=10000, qty=10)
    maker["participant_id"] = "FIRM_A"
    engine.submit(maker)

    taker = make_limit_order(side="sell", price=10000, qty=10)
    taker["participant_id"] = "FIRM_B"
    events = engine.submit(taker)

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    assert trades[0].qty == 10
    assert trades[0].price == 10000


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------

def test_large_qty_order_does_not_overflow(engine, make_limit_order):
    """Orders with very large quantities (sys.maxsize) are handled safely."""
    huge_qty = sys.maxsize
    order = make_limit_order(price=10000, qty=huge_qty)
    events = engine.submit(order)

    book = engine.get_book("TEST")
    resting = book.get_order(order["order_id"])
    assert resting is not None
    assert resting.qty == huge_qty
    assert book.bids[10000].total_volume == huge_qty


def test_duplicate_order_id_is_rejected(engine, make_limit_order):
    """Submitting a second order with an already-seen order_id is rejected."""
    order1 = make_limit_order(price=10000, qty=10)
    engine.submit(order1)

    # Submit second order with identical order_id
    order2 = make_limit_order(price=10050, qty=20)
    order2["order_id"] = order1["order_id"]
    events = engine.submit(order2)

    assert len(events) == 1
    assert isinstance(events[0], OrderRejected)
    assert "duplicate" in events[0].reason.lower()

    # Original order must be unaffected
    book = engine.get_book("TEST")
    assert book.get_order(order1["order_id"]).qty == 10
    assert book.get_order(order1["order_id"]).price == 10000


def test_book_remains_consistent_after_rapid_insert_and_cancel(
    engine, make_limit_order, make_cancel_order
):
    """Alternating rapid inserts and cancels leave the book in a consistent state."""
    for i in range(1, 1001):
        order = make_limit_order(price=10000 + (i % 50), qty=10)
        engine.submit(order)
        cancel_cmd = make_cancel_order(order_id=order["order_id"], symbol="TEST")
        engine.cancel(cancel_cmd)

    book = engine.get_book("TEST")
    assert len(book.bids) == 0
    assert len(book.asks) == 0
    assert len(book.orders_map) == 0
    assert book.best_bid is None
    assert book.best_ask is None
