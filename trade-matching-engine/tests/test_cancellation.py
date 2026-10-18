"""
test_cancellation.py
--------------------
Unit tests for order cancellation behaviour and empty level cleanup.
"""

from __future__ import annotations

import pytest
from src.engine.events import CancelAck, CancelReject
from src.engine.order import Side


# ---------------------------------------------------------------------------
# Cancel existing order
# ---------------------------------------------------------------------------

def test_cancel_existing_resting_order_removes_it_from_book(
    engine, make_limit_order, make_cancel_order
):
    """Cancelling a live resting order removes it from the order book.

    Post a limit buy, then cancel it. The order book bid side should be
    empty afterwards and no further fills should occur against it.
    """
    order = make_limit_order(side="buy", price=10000, qty=10)
    engine.submit(order)
    book = engine.get_book(order["symbol"])

    assert order["order_id"] in book.orders_map
    assert book.best_bid == 10000

    cancel_cmd = make_cancel_order(order_id=order["order_id"], symbol=order["symbol"])
    res = engine.cancel(cancel_cmd)

    assert isinstance(res, CancelAck)
    assert res.order_id == order["order_id"]
    assert res.cancelled_qty == 10
    assert order["order_id"] not in book.orders_map
    assert book.best_bid is None


def test_cancel_returns_acknowledgement(engine, make_limit_order, make_cancel_order):
    """A successful cancel produces a cancel-acknowledgement response.

    The engine should return (or emit) a CancelAck event with the matching
    order_id and remaining quantity.
    """
    order = make_limit_order(side="sell", price=10050, qty=25)
    engine.submit(order)

    cancel_cmd = make_cancel_order(order_id=order["order_id"], symbol=order["symbol"])
    res = engine.cancel(cancel_cmd)

    assert isinstance(res, CancelAck)
    assert res.order_id == order["order_id"]
    assert res.symbol == "TEST"
    assert res.side == Side.SELL
    assert res.cancelled_qty == 25


def test_cancel_partially_filled_order(engine, make_limit_order, make_cancel_order):
    """Cancelling a partially filled order removes only its remaining quantity.

    Post buy qty=10, partially fill qty=4, then cancel. The order book should
    be empty and the fill report should reflect qty=4 filled, qty=6 cancelled.
    """
    buy_order = make_limit_order(side="buy", price=10000, qty=10)
    engine.submit(buy_order)

    # Aggressive crossing sell order for qty=4
    sell_order = make_limit_order(side="sell", price=10000, qty=4)
    trades = engine.submit(sell_order)
    assert len(trades) == 1
    assert trades[0].qty == 4

    # Cancel the resting remainder (qty=6)
    cancel_cmd = make_cancel_order(order_id=buy_order["order_id"], symbol=buy_order["symbol"])
    res = engine.cancel(cancel_cmd)

    assert isinstance(res, CancelAck)
    assert res.order_id == buy_order["order_id"]
    assert res.cancelled_qty == 6

    # Book is completely empty
    book = engine.get_book("TEST")
    assert buy_order["order_id"] not in book.orders_map
    assert book.best_bid is None


# ---------------------------------------------------------------------------
# Cancel non-existent order
# ---------------------------------------------------------------------------

def test_cancel_non_existent_order_returns_reject(engine, make_cancel_order):
    """Attempting to cancel an unknown order_id returns a cancel-reject.

    The engine must not crash and must return or emit a CancelReject error message.
    """
    cancel_cmd = make_cancel_order(order_id=999999, symbol="TEST")
    res = engine.cancel(cancel_cmd)

    assert isinstance(res, CancelReject)
    assert res.order_id == 999999
    assert "not found" in res.reason.lower()


def test_cancel_already_filled_order_returns_reject(
    engine, make_limit_order, make_cancel_order
):
    """Cancelling a fully filled (and removed) order returns a cancel-reject.

    Post and fully fill an order, then attempt to cancel the same order_id.
    Engine should handle gracefully and return a cancel-reject.
    """
    buy_order = make_limit_order(side="buy", price=10000, qty=10)
    engine.submit(buy_order)

    # Completely fill the resting buy order
    sell_order = make_limit_order(side="sell", price=10000, qty=10)
    engine.submit(sell_order)

    cancel_cmd = make_cancel_order(order_id=buy_order["order_id"], symbol=buy_order["symbol"])
    res = engine.cancel(cancel_cmd)

    assert isinstance(res, CancelReject)
    assert res.order_id == buy_order["order_id"]
    assert "filled" in res.reason.lower()


def test_cancel_already_cancelled_order_returns_reject(
    engine, make_limit_order, make_cancel_order
):
    """Cancelling an order that was previously cancelled returns a cancel-reject.

    Ensures the engine does not double-cancel or corrupt internal state.
    """
    buy_order = make_limit_order(side="buy", price=10000, qty=10)
    engine.submit(buy_order)

    cancel_cmd = make_cancel_order(order_id=buy_order["order_id"], symbol=buy_order["symbol"])
    res1 = engine.cancel(cancel_cmd)
    assert isinstance(res1, CancelAck)

    # Attempting to cancel a second time must return CancelReject
    res2 = engine.cancel(cancel_cmd)
    assert isinstance(res2, CancelReject)
    assert res2.order_id == buy_order["order_id"]
    assert "cancelled" in res2.reason.lower()


# ---------------------------------------------------------------------------
# Empty level cleanup
# ---------------------------------------------------------------------------

def test_empty_price_level_removed_after_cancel(
    engine, make_limit_order, make_cancel_order
):
    """Cancelling the last order at a price level removes that level entirely.

    Post a single limit order then cancel it. The price level should no
    longer appear in the order book depth snapshot.
    """
    order = make_limit_order(side="buy", price=10000, qty=10)
    engine.submit(order)
    book = engine.get_book(order["symbol"])
    assert 10000 in book.bids

    cancel_cmd = make_cancel_order(order_id=order["order_id"], symbol=order["symbol"])
    engine.cancel(cancel_cmd)

    # Price level 10000 must be pruned from bids
    assert 10000 not in book.bids
    snapshot = engine.get_l2_snapshot("TEST")
    assert len(snapshot["bids"]) == 0


def test_empty_price_level_removed_after_fill(engine, make_limit_order, make_market_order):
    """Fully filling the last order at a price level removes that level.

    Ensures the sorted structure stays clean and does not retain ghost levels
    with zero quantity.
    """
    order = make_limit_order(side="buy", price=10000, qty=10)
    engine.submit(order)
    book = engine.get_book(order["symbol"])
    assert 10000 in book.bids

    market_sell = make_market_order(side="sell", qty=10)
    engine.submit(market_sell)

    # Price level 10000 must be pruned from bids after complete fill
    assert 10000 not in book.bids
    snapshot = engine.get_l2_snapshot("TEST")
    assert len(snapshot["bids"]) == 0
