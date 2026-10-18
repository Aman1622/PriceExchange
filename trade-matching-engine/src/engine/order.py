"""
order.py
--------
Represents an individual order and trading side enum in the matching engine.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Side(str, Enum):
    """Represents the trading side of an order: BUY or SELL.

    Time Complexity:
        O(1) comparison and hashing.

    Why it is designed this way:
        Subclassing both str and Enum allows seamless string comparisons and
        JSON serialization without losing type safety or enum identity checks.
    """
    BUY = "BUY"
    SELL = "SELL"

    @classmethod
    def from_str(cls, value: str) -> Side:
        """Parse a string into a Side enum.

        Time Complexity:
            O(1).

        Why it is designed this way:
            Normalizes case and common synonyms ('buy', 'bid', 'sell', 'ask')
            at the network/API boundary so engine internals deal only with canonical enums.
        """
        normalized = value.strip().upper()
        if normalized in ("BUY", "BID"):
            return cls.BUY
        if normalized in ("SELL", "ASK"):
            return cls.SELL
        raise ValueError(f"Invalid order side: {value!r}. Expected BUY or SELL.")


class Order:
    """Represents an order in the matching engine.

    Attributes:
        order_id: Unique integer identifier.
        symbol: Ticker symbol (e.g. 'AAPL').
        side: Side.BUY or Side.SELL.
        price: Integer tick price (e.g., 10050 = $100.50). Never float.
        qty: Remaining quantity open to be matched.
        initial_qty: Original submitted quantity.
        timestamp: High-resolution timestamp or logical sequence number for time priority.
        participant_id: ID of the submitting trader/account (used for self-trade prevention).
        prev: Pointer to previous Order in the intrusive doubly-linked list.
        next: Pointer to next Order in the intrusive doubly-linked list.

    Time Complexity:
        Attribute read/write: O(1).
        Instantiation: O(1).

    Why it is designed this way:
        1. __slots__ eliminates the per-instance __dict__, reducing memory
           from ~150 bytes to ~50 bytes per order. Essential for holding millions
           of open orders in memory.
        2. Intrusive doubly-linked list pointers ('prev' and 'next') live directly
           on the Order, enabling O(1) removal from a PriceLevel given only an
           Order reference, avoiding extra wrapper node allocations.
        3. Integer tick prices eliminate IEEE-754 floating-point inaccuracies
           and precision drift.
    """

    __slots__ = (
        "order_id",
        "symbol",
        "side",
        "price",
        "qty",
        "initial_qty",
        "timestamp",
        "participant_id",
        "prev",
        "next",
    )

    def __init__(
        self,
        order_id: int,
        symbol: str,
        side: Side | str,
        price: int,
        qty: int,
        timestamp: int = 0,
        participant_id: str = "",
        initial_qty: Optional[int] = None,
    ) -> None:
        if qty <= 0:
            raise ValueError(f"Quantity must be strictly positive, got {qty}")
        if price < 0:
            raise ValueError(f"Price cannot be negative, got {price}")

        self.order_id: int = order_id
        self.symbol: str = symbol
        self.side: Side = side if isinstance(side, Side) else Side.from_str(side)
        self.price: int = price
        self.qty: int = qty
        self.initial_qty: int = initial_qty if initial_qty is not None else qty
        self.timestamp: int = timestamp
        self.participant_id: str = participant_id

        # Intrusive doubly-linked list pointers inside a PriceLevel
        self.prev: Optional[Order] = None
        self.next: Optional[Order] = None

    @property
    def is_filled(self) -> bool:
        """Check if order has been completely matched.

        Time Complexity: O(1).
        Why: Read-only property simplifying matching loop termination checks.
        """
        return self.qty == 0

    def __repr__(self) -> str:
        return (
            f"Order(id={self.order_id}, {self.symbol}, {self.side.value}, "
            f"px={self.price}, qty={self.qty}/{self.initial_qty})"
        )

