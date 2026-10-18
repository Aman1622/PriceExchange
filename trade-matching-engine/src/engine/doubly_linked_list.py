"""
doubly_linked_list.py
---------------------
Intrusive doubly-linked list representing a FIFO PriceLevel in the order book.
"""

from __future__ import annotations

from typing import Iterator, Optional
from src.engine.order import Order


class PriceLevel:
    """A FIFO queue of orders at a specific integer price tick.

    Maintains an intrusive doubly-linked list of Order objects. Orders at the
    same limit price queue here in arrival order to enforce Price-Time Priority
    (FIFO: First-In, First-Out).

    Attributes:
        price: Integer tick price for all orders in this level.
        head: Oldest resting order at this level (next to match).
        tail: Newest resting order at this level.

    Time Complexity:
        append: O(1)
        remove: O(1)
        peek: O(1)
        total_volume: O(1)
        order_count: O(1)

    Why it is designed this way:
        1. Intrusive list: The Order itself holds 'prev' and 'next' references.
           When an order is cancelled or filled, we look it up in the book's hash map
           in O(1) and unlink it from this list in O(1) without having to traverse
           the list or allocate separate node wrappers.
        2. FIFO matching: Matching engine pops from head (oldest); new orders append
           to tail (newest). Both head unlinking and tail appending are O(1).
        3. Running aggregates: Total volume and order count are tracked via cached
           counters updated during append/remove, avoiding O(N) traversals on L2
           snapshot generation or depth queries.
    """

    __slots__ = ("price", "head", "tail", "_order_count", "_total_volume")

    def __init__(self, price: int) -> None:
        """Initialise an empty price level at the given integer tick price.

        Time Complexity: O(1).
        Why: Allocates constant-size slot variables with zero heap overhead.
        """
        self.price: int = price
        self.head: Optional[Order] = None
        self.tail: Optional[Order] = None
        self._order_count: int = 0
        self._total_volume: int = 0

    @property
    def order_count(self) -> int:
        """Return the number of resting orders at this price level.

        Time Complexity: O(1).
        Why: Running counter avoids traversing the linked list.
        """
        return self._order_count

    @property
    def total_volume(self) -> int:
        """Return the aggregate resting quantity at this price level.

        Time Complexity: O(1).
        Why: Running sum updated on insert, fill, and cancel, enabling instant L2 calculation.
        """
        return self._total_volume

    def peek(self) -> Optional[Order]:
        """Inspect the oldest resting order without removing it.

        Time Complexity: O(1).
        Why: The matching engine checks the head of the best price level to
        determine match eligibility before mutating book state.
        """
        return self.head

    def append(self, order: Order) -> None:
        """Append an order to the tail of this price level (FIFO order).

        Time Complexity: O(1).
        Why: Enforces time priority by placing incoming resting orders at the end
        of the queue. Pointer updates and counter increments are strictly O(1).
        """
        if order.price != self.price:
            raise ValueError(
                f"Order price {order.price} does not match PriceLevel price {self.price}"
            )

        # Clear any stale links on the incoming order
        order.prev = self.tail
        order.next = None

        if self.tail is not None:
            # Relink old tail forward pointer to the incoming order
            self.tail.next = order
        else:
            # List was empty; new order is both head and tail
            self.head = order

        self.tail = order
        self._order_count += 1
        self._total_volume += order.qty

    def remove(self, order: Order) -> None:
        """Remove an arbitrary order from this price level in O(1) time.

        Time Complexity: O(1).
        Why: Because Order contains intrusive pointers, unlinking only requires
        relinking order.prev and order.next without searching the list.

        Args:
            order: The Order instance to remove. Must belong to this level.
        """
        # Pointer relinking: update predecessor or advance head
        if order.prev is not None:
            order.prev.next = order.next
        else:
            # Order was head; advance head pointer to next order
            self.head = order.next

        # Pointer relinking: update successor or retreat tail
        if order.next is not None:
            order.next.prev = order.prev
        else:
            # Order was tail; retreat tail pointer to previous order
            self.tail = order.prev

        # Nullify pointers on the detached order to prevent memory leaks / dangling refs
        order.prev = None
        order.next = None

        self._order_count -= 1
        self._total_volume -= order.qty

    def update_order_qty(self, order: Order, new_qty: int) -> None:
        """Update an order's quantity in-place, adjusting the level's total volume.

        Time Complexity: O(1).
        Why: When an order is partially filled or reduced, its quantity is updated
        in-place without changing its position in the FIFO queue, preserving time priority.
        """
        if new_qty < 0:
            raise ValueError(f"Order quantity cannot be negative, got {new_qty}")
        delta = new_qty - order.qty
        order.qty = new_qty
        self._total_volume += delta

    def is_empty(self) -> bool:
        """Return True if this price level has zero resting orders.

        Time Complexity: O(1).
        Why: Used by OrderBook to detect when an empty price level should be pruned.
        """
        return self._order_count == 0

    def __len__(self) -> int:
        return self._order_count

    def __iter__(self) -> Iterator[Order]:
        """Iterate through orders in FIFO order from head to tail.

        Time Complexity: O(N) where N is order count at this level.
        Why: Generator iteration simplifies debugging, inspection, and snapshotting.
        """
        curr = self.head
        while curr is not None:
            nxt = curr.next  # Cache next pointer in case curr is mutated during iteration
            yield curr
            curr = nxt

    def __repr__(self) -> str:
        return (
            f"PriceLevel(px={self.price}, orders={self._order_count}, "
            f"vol={self._total_volume})"
        )

