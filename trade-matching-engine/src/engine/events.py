"""
events.py
---------
Monotonically sequenced event objects emitted by the matching engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from src.engine.order import Side


@dataclass(slots=True, frozen=True)
class OrderAccepted:
    """Emitted when an order passes validation and rests on the order book.

    Time Complexity: O(1) instantiation.
    Why: Monotonic seq number guarantees deterministic replay during WAL recovery.
    """
    seq: int
    order_id: int
    symbol: str
    side: Side
    price: int
    qty: int
    timestamp: int


@dataclass(slots=True, frozen=True)
class CancelAck:
    """Emitted when an open resting order is successfully cancelled.

    Time Complexity: O(1) instantiation.
    Why: Informs client of remaining unfilled quantity that was cancelled.
    """
    seq: int
    order_id: int
    symbol: str
    side: Side
    cancelled_qty: int


@dataclass(slots=True, frozen=True)
class CancelReject:
    """Emitted when a cancel request fails (e.g. unknown or already filled order).

    Time Complexity: O(1) instantiation.
    Why: Prevents silent failure; allows trading systems to reconcile state.
    """
    seq: int
    order_id: int
    symbol: str
    reason: str


@dataclass(slots=True, frozen=True)
class OrderRejected:
    """Emitted when an incoming order is rejected before touching the book.

    Time Complexity: O(1) instantiation.
    Why: Signals invalid input (negative price, zero qty, self-trade violation).
    """
    seq: int
    order_id: int
    symbol: str
    reason: str


@dataclass(slots=True, frozen=True)
class Trade:
    """Emitted when an aggressive taker order matches against a resting maker order.

    Time Complexity: O(1) instantiation.
    Why: Trade executes at the resting maker's price. Emitting both maker and
    taker IDs allows post-trade settlement and participant fill notifications.
    """
    seq: int
    symbol: str
    maker_order_id: int
    taker_order_id: int
    price: int
    qty: int
    maker_side: Side
    timestamp: int

