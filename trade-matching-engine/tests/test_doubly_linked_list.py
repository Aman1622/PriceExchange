"""
tests/test_doubly_linked_list.py
--------------------------------
Unit tests for the intrusive doubly-linked list (PriceLevel).
"""

import pytest
from src.engine.order import Order, Side
from src.engine.doubly_linked_list import PriceLevel


def test_empty_price_level():
    """Verify state of an empty PriceLevel."""
    level = PriceLevel(price=10000)
    assert level.price == 10000
    assert level.head is None
    assert level.tail is None
    assert level.order_count == 0
    assert level.total_volume == 0
    assert level.peek() is None
    assert level.is_empty()
    assert len(level) == 0
    assert list(level) == []


def test_append_single_order():
    """Verify appending a single order correctly updates head, tail, and aggregates in O(1)."""
    level = PriceLevel(price=10000)
    order = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=50)

    level.append(order)

    assert level.order_count == 1
    assert level.total_volume == 50
    assert level.head is order
    assert level.tail is order
    assert level.peek() is order
    assert order.prev is None
    assert order.next is None
    assert not level.is_empty()
    assert len(level) == 1


def test_append_multiple_orders_preserves_fifo():
    """Verify orders appended in sequence preserve FIFO (time priority)."""
    level = PriceLevel(price=10000)
    o1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    o2 = Order(order_id=2, symbol="AAPL", side=Side.BUY, price=10000, qty=20)
    o3 = Order(order_id=3, symbol="AAPL", side=Side.BUY, price=10000, qty=30)

    level.append(o1)
    level.append(o2)
    level.append(o3)

    assert level.order_count == 3
    assert level.total_volume == 60
    assert level.head is o1
    assert level.tail is o3
    assert level.peek() is o1

    # Check forward links
    assert o1.prev is None
    assert o1.next is o2
    assert o2.prev is o1
    assert o2.next is o3
    assert o3.prev is o2
    assert o3.next is None

    # Check iterator order
    assert list(level) == [o1, o2, o3]


def test_append_mismatched_price_raises_error():
    """Appending an order with a price different from the level price must fail."""
    level = PriceLevel(price=10000)
    wrong_order = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10050, qty=10)

    with pytest.raises(ValueError, match="does not match PriceLevel"):
        level.append(wrong_order)


def test_remove_only_order():
    """Removing the only order leaves the level empty."""
    level = PriceLevel(price=10000)
    o1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    level.append(o1)

    level.remove(o1)

    assert level.order_count == 0
    assert level.total_volume == 0
    assert level.head is None
    assert level.tail is None
    assert level.is_empty()
    assert o1.prev is None
    assert o1.next is None


def test_remove_head_order():
    """Removing the head order advances head to next order."""
    level = PriceLevel(price=10000)
    o1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    o2 = Order(order_id=2, symbol="AAPL", side=Side.BUY, price=10000, qty=20)
    o3 = Order(order_id=3, symbol="AAPL", side=Side.BUY, price=10000, qty=30)
    level.append(o1)
    level.append(o2)
    level.append(o3)

    level.remove(o1)

    assert level.order_count == 2
    assert level.total_volume == 50
    assert level.head is o2
    assert level.tail is o3
    assert o2.prev is None
    assert o2.next is o3
    assert o1.prev is None
    assert o1.next is None
    assert list(level) == [o2, o3]


def test_remove_tail_order():
    """Removing the tail order retreats tail to previous order."""
    level = PriceLevel(price=10000)
    o1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    o2 = Order(order_id=2, symbol="AAPL", side=Side.BUY, price=10000, qty=20)
    o3 = Order(order_id=3, symbol="AAPL", side=Side.BUY, price=10000, qty=30)
    level.append(o1)
    level.append(o2)
    level.append(o3)

    level.remove(o3)

    assert level.order_count == 2
    assert level.total_volume == 30
    assert level.head is o1
    assert level.tail is o2
    assert o2.prev is o1
    assert o2.next is None
    assert o3.prev is None
    assert o3.next is None
    assert list(level) == [o1, o2]


def test_remove_middle_order():
    """Removing a middle order relinks predecessor and successor directly in O(1)."""
    level = PriceLevel(price=10000)
    o1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    o2 = Order(order_id=2, symbol="AAPL", side=Side.BUY, price=10000, qty=20)
    o3 = Order(order_id=3, symbol="AAPL", side=Side.BUY, price=10000, qty=30)
    level.append(o1)
    level.append(o2)
    level.append(o3)

    level.remove(o2)

    assert level.order_count == 2
    assert level.total_volume == 40
    assert level.head is o1
    assert level.tail is o3
    assert o1.next is o3
    assert o3.prev is o1
    assert o2.prev is None
    assert o2.next is None
    assert list(level) == [o1, o3]


def test_update_order_qty():
    """Updating order quantity adjusts total_volume without altering queue position."""
    level = PriceLevel(price=10000)
    o1 = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    o2 = Order(order_id=2, symbol="AAPL", side=Side.BUY, price=10000, qty=20)
    level.append(o1)
    level.append(o2)

    # Partial fill on head order from 10 down to 4
    level.update_order_qty(o1, new_qty=4)

    assert o1.qty == 4
    assert level.total_volume == 24
    assert level.head is o1
    assert level.peek() is o1

    with pytest.raises(ValueError, match="cannot be negative"):
        level.update_order_qty(o1, new_qty=-1)

