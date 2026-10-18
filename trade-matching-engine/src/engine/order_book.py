"""
order_book.py
-------------
Maintains the bid and ask sides for a single instrument using SortedDict
and intrusive doubly-linked lists.
"""

from __future__ import annotations

from itertools import islice
from typing import Any, Dict, List, Optional, Tuple
from sortedcontainers import SortedDict

from src.engine.doubly_linked_list import PriceLevel
from src.engine.order import Order, Side


class OrderBook:
    """In-memory limit order book for a single trading symbol.

    Maintains bid and ask price levels sorted by price ticks, with secondary
    FIFO queues at each price level enforcing Price-Time Priority.

    Attributes:
        symbol: Ticker symbol (e.g. 'AAPL').
        bids: SortedDict mapping integer price -> PriceLevel (ascending keys).
        asks: SortedDict mapping integer price -> PriceLevel (ascending keys).
        orders_map: Hash map mapping order_id -> Order reference for O(1) lookups.

    Time Complexity:
        add_order:
            O(1) if price level exists.
            O(log M) if new price level must be inserted into SortedDict (M = unique prices).
        cancel_order:
            O(1) average hash map lookup + O(1) intrusive list unlink.
            O(log M) price level removal if level becomes empty.
        modify_order:
            Reduce quantity: O(1) (retains queue position).
            Increase quantity: O(1) (unlinked and appended to tail).
        get_l2_snapshot:
            O(D) where D is the requested depth.
        best_bid / best_ask:
            O(1) or O(log M) peek on SortedDict boundaries.

    Why it is designed this way:
        1. SortedDict uses a balanced B-tree-like chunked array structure in Python.
           It delivers faster traversal and lower memory overhead than pure balanced
           binary search trees (AVL/Red-Black), while maintaining price order.
        2. orders_map provides direct O(1) access to resting orders without searching
           price queues. Combined with intrusive pointers on Order, cancellations
           are strictly O(1) before empty-level pruning.
        3. Pruning empty price levels keeps memory proportional to active levels and
           guarantees that best_bid/best_ask queries always inspect populated levels.
    """

    __slots__ = ("symbol", "bids", "asks", "orders_map")

    def __init__(self, symbol: str) -> None:
        """Initialise an empty order book for the given symbol.

        Time Complexity: O(1).
        Why: Allocates empty sorted maps and lookup table.
        """
        self.symbol: str = symbol
        self.bids: SortedDict[int, PriceLevel] = SortedDict()
        self.asks: SortedDict[int, PriceLevel] = SortedDict()
        self.orders_map: Dict[int, Order] = {}

    @property
    def best_bid(self) -> Optional[int]:
        """Return the highest buy price in the book, or None if no bids.

        Time Complexity: O(1).
        Why: SortedDict keys are in ascending order; highest bid is at index -1.
        """
        if not self.bids:
            return None
        return self.bids.peekitem(-1)[0]

    @property
    def best_ask(self) -> Optional[int]:
        """Return the lowest sell price in the book, or None if no asks.

        Time Complexity: O(1).
        Why: Lowest ask is at index 0 of the ascending SortedDict.
        """
        if not self.asks:
            return None
        return self.asks.peekitem(0)[0]

    @property
    def best_bid_level(self) -> Optional[PriceLevel]:
        """Return the PriceLevel at the best bid, or None if bids is empty.

        Time Complexity: O(1).
        Why: Provides direct access to the oldest resting buy order for matching.
        """
        if not self.bids:
            return None
        return self.bids.peekitem(-1)[1]

    @property
    def best_ask_level(self) -> Optional[PriceLevel]:
        """Return the PriceLevel at the best ask, or None if asks is empty.

        Time Complexity: O(1).
        Why: Provides direct access to the oldest resting sell order for matching.
        """
        if not self.asks:
            return None
        return self.asks.peekitem(0)[1]

    def get_order(self, order_id: int) -> Optional[Order]:
        """Look up a resting order by its unique ID.

        Time Complexity: O(1) average.
        Why: Direct hash table lookup via orders_map.
        """
        return self.orders_map.get(order_id)

    def add_order(self, order: Order) -> None:
        """Add a resting limit order to the appropriate side of the book.

        Time Complexity:
            O(1) if price level exists.
            O(log M) if a new price level is created (M = active levels).

        Why:
            Maintains price-time priority: orders at the same price queue FIFO.

        Raises:
            ValueError: If an order with the same order_id already exists in the book.
        """
        if order.order_id in self.orders_map:
            raise ValueError(f"Order {order.order_id} already exists in book")

        side_dict = self.bids if order.side == Side.BUY else self.asks

        # If price level doesn't exist, allocate and insert into SortedDict
        if order.price not in side_dict:
            side_dict[order.price] = PriceLevel(order.price)

        # Append to the FIFO queue at this price level
        side_dict[order.price].append(order)
        self.orders_map[order.order_id] = order

    def remove_order(self, order: Order) -> None:
        """Unlink an order from its price level and remove it from the book.

        Time Complexity:
            O(1) unlink + O(log M) if the level becomes empty and is pruned.

        Why:
            Used when an order is completely filled or cancelled. Pruning empty
            levels prevents ghost price levels from degrading traversal performance.
        """
        side_dict = self.bids if order.side == Side.BUY else self.asks
        level = side_dict.get(order.price)
        if level is not None:
            level.remove(order)
            # Prune empty price level to maintain compact search space
            if level.is_empty():
                del side_dict[order.price]

        self.orders_map.pop(order.order_id, None)

    def cancel_order(self, order_id: int) -> Optional[Order]:
        """Cancel a resting order by order_id and remove it from the book.

        Time Complexity:
            O(1) lookup + O(1) unlink + O(log M) if price level is deleted.

        Why:
            Clients can cancel resting liquidity at any time. Look up in orders_map
            and unlinking from PriceLevel avoids searching through price queues.

        Returns:
            The cancelled Order, or None if order_id is not found in the book.
        """
        order = self.orders_map.get(order_id)
        if order is None:
            return None

        self.remove_order(order)
        return order

    def modify_order(self, order_id: int, new_qty: int) -> Optional[Order]:
        """Modify the quantity of a resting order.

        Rules:
            - If new_qty <= 0: The order is cancelled.
            - If new_qty < current_qty: In-place volume reduction; keeps queue priority.
            - If new_qty > current_qty: Loses queue priority; order is moved to tail.

        Time Complexity:
            Reduce: O(1).
            Increase: O(1) relinking.

        Why:
            Standard exchange priority rule: traders may reduce size without penalty,
            but increasing size counts as a new order and goes to the back of the queue.

        Returns:
            The updated Order, or None if order_id is not found.
        """
        order = self.orders_map.get(order_id)
        if order is None:
            return None

        if new_qty <= 0:
            self.cancel_order(order_id)
            return None

        side_dict = self.bids if order.side == Side.BUY else self.asks
        level = side_dict[order.price]

        if new_qty < order.qty:
            # Quantity reduction: keep priority in FIFO queue
            level.update_order_qty(order, new_qty)
        elif new_qty > order.qty:
            # Quantity increase: forfeit priority and move to tail
            level.remove(order)
            order.qty = new_qty
            level.append(order)

        return order

    def get_l2_snapshot(self, depth: int = 10) -> Dict[str, Any]:
        """Extract a Level 2 (market depth) aggregated snapshot.

        Format:
            {
                'symbol': str,
                'bids': [(price, total_volume), ...],  # Descending: highest first
                'asks': [(price, total_volume), ...],  # Ascending: lowest first
            }

        Time Complexity:
            O(depth) extraction.

        Why:
            Clients and UI feeds need aggregated price-level liquidity rather
            than individual order-level data.
        """
        # Bids: descending order (highest price first)
        bids_slice = [
            (price, level.total_volume)
            for price, level in islice(reversed(self.bids.items()), depth)
        ]
        # Asks: ascending order (lowest price first)
        asks_slice = [
            (price, level.total_volume)
            for price, level in islice(self.asks.items(), depth)
        ]
        return {
            "symbol": self.symbol,
            "bids": bids_slice,
            "asks": asks_slice,
        }

    def __len__(self) -> int:
        return len(self.orders_map)

    def __repr__(self) -> str:
        bb = self.best_bid
        ba = self.best_ask
        return (
            f"OrderBook(symbol={self.symbol!r}, orders={len(self.orders_map)}, "
            f"best_bid={bb}, best_ask={ba})"
        )

