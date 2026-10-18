"""
tests/test_order_book.py
------------------------
Unit tests for the OrderBook data structure.
"""

import pytest
from src.engine.order import Order, Side
from src.engine.order_book import OrderBook


def test_order_book_init():
    """Verify empty OrderBook initialization."""
    book = OrderBook("AAPL")
    assert book.symbol == "AAPL"
    assert len(book.bids) == 0
    assert len(book.asks) == 0
    assert len(book.orders_map) == 0
    assert book.best_bid is None
    assert book.best_ask is None


def test_order_book_add_orders():
    """Verify adding orders to bids and asks sides."""
    book = OrderBook("AAPL")
    b1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    b2 = Order(order_id=2, symbol="AAPL", side=Side.BUY, price=10050, qty=20)
    a1 = Order(order_id=3, symbol="AAPL", side=Side.SELL, price=10100, qty=15)

    book.add_order(b1)
    book.add_order(b2)
    book.add_order(a1)

    assert len(book) == 3
    assert book.best_bid == 10050  # Highest buy price
    assert book.best_ask == 10100  # Lowest sell price
    assert book.get_order(1) is b1
    assert book.get_order(2) is b2
    assert book.get_order(3) is a1


def test_order_book_rejects_duplicate_order_id():
    """Adding an order with an existing order_id raises ValueError."""
    book = OrderBook("AAPL")
    b1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    b2 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=20)

    book.add_order(b1)
    with pytest.raises(ValueError, match="already exists in book"):
        book.add_order(b2)


def test_order_book_cancel_order_prunes_empty_level():
    """Cancelling the only order at a level removes the level from SortedDict."""
    book = OrderBook("AAPL")
    b1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    book.add_order(b1)

    assert 10000 in book.bids
    cancelled = book.cancel_order(1)

    assert cancelled is b1
    assert 1 not in book.orders_map
    assert 10000 not in book.bids
    assert book.best_bid is None


def test_order_book_modify_order_reduce_keeps_priority():
    """Reducing order quantity adjusts total volume in-place and preserves queue priority."""
    book = OrderBook("AAPL")
    b1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=20)
    b2 = Order(order_id=2, symbol="AAPL", side=Side.BUY, price=10000, qty=30)
    book.add_order(b1)
    book.add_order(b2)

    level = book.bids[10000]
    assert level.head is b1
    assert level.total_volume == 50

    # Reduce b1 quantity from 20 to 10
    updated = book.modify_order(order_id=1, new_qty=10)

    assert updated is b1
    assert b1.qty == 10
    assert level.total_volume == 40
    # b1 must still be head (queue priority preserved!)
    assert level.head is b1
    assert level.tail is b2


def test_order_book_modify_order_increase_moves_to_tail():
    """Increasing order quantity forfeits queue priority, moving order to tail of level."""
    book = OrderBook("AAPL")
    b1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    b2 = Order(order_id=2, symbol="AAPL", side=Side.BUY, price=10000, qty=20)
    book.add_order(b1)
    book.add_order(b2)

    level = book.bids[10000]
    assert level.head is b1
    assert level.tail is b2

    # Increase b1 quantity from 10 to 50: must lose time priority and move to tail
    updated = book.modify_order(order_id=1, new_qty=50)

    assert updated is b1
    assert b1.qty == 50
    assert level.total_volume == 70
    assert level.head is b2  # b2 is now the head
    assert level.tail is b1  # b1 moved to tail


def test_order_book_modify_order_zero_cancels():
    """Modifying quantity to 0 cancels the order and removes the level if empty."""
    book = OrderBook("AAPL")
    b1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    book.add_order(b1)

    res = book.modify_order(order_id=1, new_qty=0)
    assert res is None
    assert 1 not in book.orders_map
    assert 10000 not in book.bids


def test_order_book_get_l2_snapshot():
    """L2 snapshot returns bids descending and asks ascending with aggregated volumes."""
    book = OrderBook("AAPL")
    book.add_order(Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10))
    book.add_order(Order(order_id=2, symbol="AAPL", side=Side.BUY, price=10000, qty=15))
    book.add_order(Order(order_id=3, symbol="AAPL", side=Side.BUY, price=10050, qty=30))

    book.add_order(Order(order_id=4, symbol="AAPL", side=Side.SELL, price=10100, qty=20))
    book.add_order(Order(order_id=5, symbol="AAPL", side=Side.SELL, price=10150, qty=40))

    snap = book.get_l2_snapshot(depth=5)
    assert snap["symbol"] == "AAPL"
    # Bids descending: 10050 (30), then 10000 (25)
    assert snap["bids"] == [(10050, 30), (10000, 25)]
    # Asks ascending: 10100 (20), then 10150 (40)
    assert snap["asks"] == [(10100, 20), (10150, 40)]

