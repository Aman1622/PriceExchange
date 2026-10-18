"""
test_matching.py
----------------
Unit tests for core order-matching logic (FIFO, partial fills, multi-level
walks, maker price execution, and market orders).
"""

from __future__ import annotations

import pytest
from src.engine.events import OrderAccepted, OrderRejected, Trade
from src.engine.order import Side


# ---------------------------------------------------------------------------
# FIFO ordering
# ---------------------------------------------------------------------------

def test_fifo_ordering_same_price_level(engine, make_limit_order):
    """Earlier resting orders at the same price level fill before later ones.

    Two buy orders posted at the same price; a crossing sell should fill the
    first buy (by arrival time) before touching the second.
    """
    buy1 = make_limit_order(side="buy", price=10000, qty=10)
    buy2 = make_limit_order(side="buy", price=10000, qty=10)
    engine.submit(buy1)
    engine.submit(buy2)

    # Aggressive sell for 10 shares crosses at 10000
    sell = make_limit_order(side="sell", price=10000, qty=10)
    events = engine.submit(sell)

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    # First buy order must be matched (FIFO priority)
    assert trades[0].maker_order_id == buy1["order_id"]
    assert trades[0].taker_order_id == sell["order_id"]
    assert trades[0].qty == 10
    assert trades[0].price == 10000

    # Second buy order must still be resting on the book
    book = engine.get_book("TEST")
    assert buy1["order_id"] not in book.orders_map
    assert buy2["order_id"] in book.orders_map
    assert book.get_order(buy2["order_id"]).qty == 10


# ---------------------------------------------------------------------------
# Partial fill
# ---------------------------------------------------------------------------

def test_partial_fill_leaves_remainder_on_book(engine, make_limit_order):
    """A resting order that is partially filled remains on the book.

    Post a buy order for qty=10 then a sell order for qty=6. The buy order
    should survive with qty=4 remaining; the sell order should be fully filled.
    """
    buy = make_limit_order(side="buy", price=10000, qty=10)
    engine.submit(buy)

    sell = make_limit_order(side="sell", price=10000, qty=6)
    events = engine.submit(sell)

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    assert trades[0].qty == 6

    book = engine.get_book("TEST")
    resting_buy = book.get_order(buy["order_id"])
    assert resting_buy is not None
    assert resting_buy.qty == 4
    assert book.best_bid == 10000
    assert book.bids[10000].total_volume == 4


def test_partial_fill_reported_correctly(engine, make_limit_order):
    """Partial-fill trade reports carry the correct filled quantity and price."""
    buy = make_limit_order(side="buy", price=10050, qty=20)
    engine.submit(buy)

    sell = make_limit_order(side="sell", price=10050, qty=7)
    events = engine.submit(sell)

    assert len(events) == 1
    trade = events[0]
    assert isinstance(trade, Trade)
    assert trade.symbol == "TEST"
    assert trade.maker_order_id == buy["order_id"]
    assert trade.taker_order_id == sell["order_id"]
    assert trade.price == 10050
    assert trade.qty == 7
    assert trade.maker_side == Side.BUY
    assert trade.seq > 0


# ---------------------------------------------------------------------------
# Multi-level fill
# ---------------------------------------------------------------------------

def test_multi_level_fill_walks_the_book(engine, make_limit_order, make_market_order):
    """An aggressive order that consumes multiple price levels is matched fully.

    Post resting sells at prices 10_000, 10_001, and 10_002 (qty=5 each).
    A buy market order for qty=15 should fill across all three levels.
    """
    sell1 = make_limit_order(side="sell", price=10000, qty=5)
    sell2 = make_limit_order(side="sell", price=10001, qty=5)
    sell3 = make_limit_order(side="sell", price=10002, qty=5)
    engine.submit(sell1)
    engine.submit(sell2)
    engine.submit(sell3)

    market_buy = make_market_order(side="buy", qty=15)
    events = engine.submit(market_buy)

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 3
    assert [t.price for t in trades] == [10000, 10001, 10002]
    assert [t.qty for t in trades] == [5, 5, 5]

    book = engine.get_book("TEST")
    assert len(book.asks) == 0
    assert book.best_ask is None


def test_multi_level_fill_generates_multiple_trade_reports(engine, make_limit_order, make_market_order):
    """One incoming order matched across N levels produces N trade reports with monotonic seqs."""
    engine.submit(make_limit_order(side="sell", price=10000, qty=10))
    engine.submit(make_limit_order(side="sell", price=10020, qty=10))

    buy = make_market_order(side="buy", qty=15)
    events = engine.submit(buy)

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 2
    assert trades[0].qty == 10
    assert trades[0].price == 10000
    assert trades[1].qty == 5
    assert trades[1].price == 10020
    assert trades[0].seq < trades[1].seq


# ---------------------------------------------------------------------------
# Crossing limit order
# ---------------------------------------------------------------------------

def test_crossing_limit_order_matches_at_resting_price(engine, make_limit_order):
    """A limit buy price >= best ask matches at the resting ask price (maker-friendly).

    Resting sell at 10_000; incoming buy limit at 10_050 should match at
    the resting price (10_000), not the aggressor's price.
    """
    sell = make_limit_order(side="sell", price=10000, qty=10)
    engine.submit(sell)

    # Aggressive buy willing to pay up to 10050
    buy = make_limit_order(side="buy", price=10050, qty=10)
    events = engine.submit(buy)

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 1
    # Must execute at resting maker price 10000 (providing price improvement to buyer)
    assert trades[0].price == 10000
    assert trades[0].qty == 10


def test_non_crossing_limit_order_rests_on_book(engine, make_limit_order):
    """A limit order that does not cross the spread is added to the book."""
    sell = make_limit_order(side="sell", price=10050, qty=10)
    engine.submit(sell)

    # Buy at 10000 does not cross ask at 10050
    buy = make_limit_order(side="buy", price=10000, qty=10)
    events = engine.submit(buy)

    trades = [e for e in events if isinstance(e, Trade)]
    assert len(trades) == 0

    accepted = [e for e in events if isinstance(e, OrderAccepted)]
    assert len(accepted) == 1
    assert accepted[0].order_id == buy["order_id"]

    book = engine.get_book("TEST")
    assert book.best_bid == 10000
    assert book.best_ask == 10050


# ---------------------------------------------------------------------------
# Market order exhaustion
# ---------------------------------------------------------------------------

def test_market_order_exhausts_available_liquidity(engine, make_limit_order, make_market_order):
    """A market order larger than available liquidity fills what exists and is done.

    Three resting sell orders totalling qty=15; a market buy for qty=100 should
    consume all 15 and report the unfilled remainder according to engine policy.
    """
    for _ in range(3):
        engine.submit(make_limit_order(side="sell", price=10000, qty=5))

    market_buy = make_market_order(side="buy", qty=100)
    events = engine.submit(market_buy)

    trades = [e for e in events if isinstance(e, Trade)]
    total_filled = sum(t.qty for t in trades)
    assert total_filled == 15

    # Book is completely cleared of asks
    book = engine.get_book("TEST")
    assert len(book.asks) == 0
    # Market order unfilled remainder (85) does not rest on the book
    assert len(book.bids) == 0


def test_market_order_on_empty_book_is_rejected_or_returned(engine, make_market_order):
    """A market order submitted against an empty opposite side is handled gracefully."""
    market_buy = make_market_order(side="buy", qty=50)
    events = engine.submit(market_buy)

    # Handled cleanly without exception, returning rejection or empty event list
    assert all(isinstance(e, OrderRejected) for e in events) or len(events) == 0
    book = engine.get_book("TEST")
    assert len(book.bids) == 0
    assert len(book.asks) == 0
