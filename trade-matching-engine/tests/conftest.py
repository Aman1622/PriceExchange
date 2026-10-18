"""
conftest.py
-----------
Shared pytest fixtures for the trade matching engine test suite.

All fixtures are stubs – fill in once the engine and persistence layers exist.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Engine fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def engine():
    """Return a fresh, empty matching engine instance with TEST book created."""
    from src.engine import MatchingEngine
    eng = MatchingEngine()
    eng.create_book("TEST")
    return eng


# ---------------------------------------------------------------------------
# Persistence / WAL fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wal_dir(tmp_path):
    """Return a temporary directory for WAL and snapshot files.

    Uses pytest's built-in ``tmp_path`` fixture so each test gets an
    isolated, automatically cleaned-up directory.
    """
    wal = tmp_path / "wal"
    wal.mkdir()
    return wal


@pytest.fixture
def engine_with_wal(wal_dir):
    """Return an engine wired to a temporary WAL directory."""
    from src.engine import MatchingEngine
    from src.persistence.wal import WALWriter
    wal_file = wal_dir / "engine.wal"
    writer = WALWriter(wal_file, fsync_mode="always")
    eng = MatchingEngine(wal_writer=writer)
    eng.create_book("TEST")
    yield eng
    writer.close()


# ---------------------------------------------------------------------------
# Order factory helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def make_limit_order():
    """Factory that returns a limit-order dict.

    Usage::

        order = make_limit_order(side="buy", price=10050, qty=10)
    """
    _counter = {"n": 0}

    def _factory(
        symbol: str = "TEST",
        side: str = "buy",
        price: int = 10_000,
        qty: int = 10,
    ) -> dict:
        _counter["n"] += 1
        return {
            "type": "limit",
            "symbol": symbol,
            "order_id": _counter["n"],
            "side": side,
            "price": price,  # integer ticks
            "qty": qty,
        }

    return _factory


@pytest.fixture
def make_market_order():
    """Factory that returns a market-order dict."""
    _counter = {"n": 1_000_000}

    def _factory(
        symbol: str = "TEST",
        side: str = "buy",
        qty: int = 10,
    ) -> dict:
        _counter["n"] += 1
        return {
            "type": "market",
            "symbol": symbol,
            "order_id": _counter["n"],
            "side": side,
            "qty": qty,
        }

    return _factory


@pytest.fixture
def make_cancel_order():
    """Factory that returns a cancel-order dict targeting a given order_id."""

    def _factory(order_id: int, symbol: str = "TEST") -> dict:
        return {
            "type": "cancel",
            "symbol": symbol,
            "order_id": order_id,
        }

    return _factory

