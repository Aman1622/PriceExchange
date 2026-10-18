"""
tests/test_order.py
-------------------
Unit tests for the Order data structure and Side enum.
"""

import pytest
from src.engine.order import Order, Side


def test_order_creation_valid():
    """Verify order instantiation with valid integer tick prices and quantities."""
    order = Order(
        order_id=1,
        symbol="AAPL",
        side=Side.BUY,
        price=15000,
        qty=100,
        timestamp=123456789,
        participant_id="TRADER_1",
    )
    assert order.order_id == 1
    assert order.symbol == "AAPL"
    assert order.side == Side.BUY
    assert order.price == 15000
    assert order.qty == 100
    assert order.initial_qty == 100
    assert order.timestamp == 123456789
    assert order.participant_id == "TRADER_1"
    assert order.prev is None
    assert order.next is None
    assert not order.is_filled


def test_order_uses_slots():
    """Verify that Order instances do NOT have a __dict__, proving memory efficiency via __slots__."""
    order = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    assert not hasattr(order, "__dict__"), "Order must use __slots__ without instance __dict__"


def test_order_side_from_string():
    """Verify string coercion for BUY and SELL."""
    assert Side.from_str("buy") == Side.BUY
    assert Side.from_str("BUY") == Side.BUY
    assert Side.from_str("bid") == Side.BUY
    assert Side.from_str("sell") == Side.SELL
    assert Side.from_str("SELL") == Side.SELL
    assert Side.from_str("ask") == Side.SELL

    with pytest.raises(ValueError, match="Invalid order side"):
        Side.from_str("HOLD")


def test_order_string_side_passed_to_init():
    """Verify that Order.__init__ accepts valid string side and converts to Side enum."""
    order = Order(order_id=2, symbol="AAPL", side="sell", price=15050, qty=50)
    assert order.side == Side.SELL


def test_order_rejects_zero_quantity():
    """Zero quantity orders must be rejected immediately."""
    with pytest.raises(ValueError, match="Quantity must be strictly positive"):
        Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=0)


def test_order_rejects_negative_quantity():
    """Negative quantity orders must be rejected."""
    with pytest.raises(ValueError, match="Quantity must be strictly positive"):
        Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=-10)


def test_order_rejects_negative_price():
    """Negative prices must be rejected."""
    with pytest.raises(ValueError, match="Price cannot be negative"):
        Order(order_id=1, symbol="AAPL", side=Side.BUY, price=-100, qty=10)


def test_order_is_filled_property():
    """Verify is_filled property correctly detects complete fill."""
    order = Order(order_id=1, symbol="AAPL", side=Side.BUY, price=10000, qty=10)
    assert not order.is_filled
    order.qty = 0
    assert order.is_filled

