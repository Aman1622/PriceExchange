"""
matching_engine.py
------------------
Coordinates order routing, matching loops, trade generation, and cancellation
across symbol order books.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union
from src.engine.events import (
    CancelAck,
    CancelReject,
    OrderAccepted,
    OrderRejected,
    Trade,
)
from src.engine.order import Order, Side
from src.engine.order_book import OrderBook


class MatchingEngine:
    """Core matching engine managing order books across instruments.

    Attributes:
        _books: Mapping from symbol string to OrderBook instance.
        _order_history: Tracks terminated order status ('FILLED', 'CANCELLED')
                        to provide accurate cancellation rejection reasons.
        _seq: Monotonically increasing sequence number generator.

    Time Complexity:
        submit limit order: O(K) where K = matched levels, + O(log M) if resting.
        submit market order: O(K) where K = consumed levels.
        cancel order: O(1) hash map lookup + O(1) list unlink + O(log M) if level pruned.

    Why it is designed this way:
        1. Monotonic sequence numbers: Every event receives an incrementing sequence ID,
           enabling deterministic WAL event-sourcing and replay recovery.
        2. Price-time priority: Limit orders cross against the opposite book's best
           price level, matching oldest resting orders first (FIFO).
        3. Maker price execution: Trades execute at the resting maker's price.
        4. Self-Trade Prevention (STP - Cancel-Newest): When an incoming order would cross
           against an order from the same participant_id, the incoming order's remaining
           qty is cancelled. We chose Cancel-Newest because it protects resting liquidity
           that other participants may already be evaluating, prevents resting book
           churn, and executes deterministically in O(1).
    """

    def __init__(self, wal_writer: Optional[Any] = None) -> None:
        """Initialise matching engine with optional WAL writer and sequence counter."""
        self._books: Dict[str, OrderBook] = {}
        self._order_history: Dict[int, str] = {}
        self._seq: int = 0
        self.wal_writer = wal_writer

    def _next_seq(self) -> int:
        """Generate next monotonic sequence number (O(1))."""
        self._seq += 1
        return self._seq

    @property
    def current_seq(self) -> int:
        """Return the most recently issued sequence number."""
        return self._seq

    def create_book(self, symbol: str) -> OrderBook:
        """Create and register a new OrderBook for the given symbol (O(1))."""
        if symbol not in self._books:
            self._books[symbol] = OrderBook(symbol)
        return self._books[symbol]

    def get_book(self, symbol: str) -> OrderBook:
        """Retrieve the OrderBook for a symbol, creating it if not present (O(1))."""
        return self.create_book(symbol)

    def get_l2_snapshot(self, symbol: str, depth: int = 10) -> Dict[str, Any]:
        """Extract a Level 2 depth snapshot for the given symbol (O(depth))."""
        return self.get_book(symbol).get_l2_snapshot(depth)

    def cancel(self, cancel_data: Dict[str, Any]) -> Union[CancelAck, CancelReject]:
        """Cancel an open resting order by order_id.

        Time Complexity:
            O(1) lookup + O(1) list unlink + O(log M) empty level pruning.
        """
        order_id = cancel_data["order_id"]
        symbol = cancel_data.get("symbol", "")

        # Validation: non-positive cancel quantity
        if "qty" in cancel_data and cancel_data["qty"] <= 0:
            return CancelReject(self._next_seq(), order_id, symbol, "Quantity must be strictly positive")

        book = self._books.get(symbol)
        if book is None:
            for b in self._books.values():
                if order_id in b.orders_map:
                    book = b
                    symbol = b.symbol
                    break

        if book is None or order_id not in book.orders_map:
            hist_status = self._order_history.get(order_id)
            if hist_status == "FILLED":
                return CancelReject(self._next_seq(), order_id, symbol, "Order already filled")
            if hist_status == "CANCELLED":
                return CancelReject(self._next_seq(), order_id, symbol, "Order already cancelled")
            return CancelReject(self._next_seq(), order_id, symbol, "Order not found")

        if self.wal_writer is not None:
            # WAL write-ahead before applying cancellation
            self.wal_writer.write_record(self._next_seq(), 2, cancel_data)

        cancelled_order = book.cancel_order(order_id)
        assert cancelled_order is not None
        self._order_history[order_id] = "CANCELLED"

        return CancelAck(
            seq=self._next_seq(),
            order_id=order_id,
            symbol=symbol,
            side=cancelled_order.side,
            cancelled_qty=cancelled_order.qty,
        )

    def submit(self, order_data: Dict[str, Any]) -> List[Any]:
        """Submit an order (limit or market) or cancel request for execution.

        Time Complexity:
            O(K) where K is number of matched price levels.
        """
        order_type = order_data.get("type", "limit").lower()
        if order_type == "cancel":
            return [self.cancel(order_data)]

        if self.wal_writer is not None:
            # WAL write-ahead before engine mutation
            self.wal_writer.write_record(self._next_seq(), 1, order_data)

        order_id = order_data["order_id"]
        symbol = order_data["symbol"]
        qty = order_data.get("qty", 0)
        timestamp = order_data.get("timestamp", 0)
        participant_id = order_data.get("participant_id", "")

        # Input validation: reject non-positive quantities
        if qty <= 0:
            return [OrderRejected(self._next_seq(), order_id, symbol, "Quantity must be strictly positive")]

        try:
            side = Side.from_str(order_data["side"])
        except (ValueError, KeyError) as err:
            return [OrderRejected(self._next_seq(), order_id, symbol, str(err))]

        price = order_data.get("price")
        if order_type == "limit":
            if price is None or price <= 0:
                return [OrderRejected(self._next_seq(), order_id, symbol, "Price must be strictly positive")]

        book = self.get_book(symbol)
        if order_id in book.orders_map or order_id in self._order_history:
            return [OrderRejected(self._next_seq(), order_id, symbol, f"Duplicate order_id {order_id}")]

        events: List[Any] = []
        rem_qty = qty

        # ── Unified Matching Loop (Price-Time Priority) ────────────────────
        is_buy = (side == Side.BUY)
        opp_side = book.asks if is_buy else book.bids
        maker_side = Side.SELL if is_buy else Side.BUY

        while rem_qty > 0 and opp_side:
            best_px = book.best_ask if is_buy else book.best_bid
            assert best_px is not None
            if order_type == "limit":
                if (is_buy and price < best_px) or (not is_buy and price > best_px):  # type: ignore[operator]
                    break  # Order does not cross spread

            level = book.best_ask_level if is_buy else book.best_bid_level
            assert level is not None
            maker = level.peek()
            assert maker is not None

            # Self-Trade Prevention: cancel incoming order's remaining qty
            if participant_id and maker.participant_id and participant_id == maker.participant_id:
                events.append(OrderRejected(
                    self._next_seq(), order_id, symbol,
                    "Self-trade prevention triggered: incoming order cancelled",
                ))
                return events

            trade_qty = min(rem_qty, maker.qty)
            trade_price = maker.price  # Executed at maker's resting price

            rem_qty -= trade_qty
            new_maker_qty = maker.qty - trade_qty

            if new_maker_qty == 0:
                book.remove_order(maker)
                self._order_history[maker.order_id] = "FILLED"
            else:
                level.update_order_qty(maker, new_maker_qty)

            events.append(Trade(
                seq=self._next_seq(),
                symbol=symbol,
                maker_order_id=maker.order_id,
                taker_order_id=order_id,
                price=trade_price,
                qty=trade_qty,
                maker_side=maker_side,
                timestamp=timestamp,
            ))

        # ── Post-Match Handling ────────────────────────────────────────────
        if order_type == "limit" and rem_qty > 0:
            resting_order = Order(
                order_id=order_id,
                symbol=symbol,
                side=side,
                price=price,  # type: ignore[arg-type]
                qty=rem_qty,
                timestamp=timestamp,
                participant_id=participant_id,
                initial_qty=qty,
            )
            book.add_order(resting_order)
            events.append(OrderAccepted(
                seq=self._next_seq(),
                order_id=order_id,
                symbol=symbol,
                side=side,
                price=price,  # type: ignore[arg-type]
                qty=rem_qty,
                timestamp=timestamp,
            ))
        elif rem_qty == 0:
            self._order_history[order_id] = "FILLED"
        elif order_type == "market" and not events:
            # Market order on empty book: rejected gracefully
            events.append(OrderRejected(
                self._next_seq(), order_id, symbol, "Market order rejected: no available liquidity"
            ))

        return events
