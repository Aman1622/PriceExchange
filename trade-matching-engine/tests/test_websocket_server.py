"""
test_websocket_server.py
------------------------
Unit tests for the background WebSocket fan-out server.
"""

from __future__ import annotations

import asyncio
import json
import queue
from unittest.mock import AsyncMock, MagicMock
from src.network.websocket_server import WebSocketServer


def test_path_parsing():
    """Verify URL path routing for market-data and trades channels."""
    parse = WebSocketServer._parse_path

    assert parse("/ws/market-data/AAPL") == ("market-data", "AAPL")
    assert parse("/ws/trades/MSFT") == ("trades", "MSFT")
    assert parse("/ws/invalid/AAPL") == (None, None)
    assert parse("/other/path") == (None, None)
    assert parse("/") == (None, None)
    assert parse("/ws/market-data") == (None, None)


import socket

def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def test_server_init_and_clean_start_stop():
    """Verify that WebSocketServer initializes, starts its thread, and stops cleanly."""
    q: queue.Queue = queue.Queue()
    port = _get_free_port()
    server = WebSocketServer(event_queue=q, host="127.0.0.1", port=port)
    assert server._host == "127.0.0.1"
    assert server._port == port
    assert server._queue is q

    # Start and stop lifecycle
    server.start()
    assert server._thread is not None
    assert server._thread.is_alive()

    server.stop(timeout=1.0)
    assert not server._thread.is_alive()


def test_drain_queue_fanout():
    """Verify queue consumption and fan-out to mocked subscriber sockets."""
    q: queue.Queue = queue.Queue()
    server = WebSocketServer(event_queue=q, host="0.0.0.0", port=8765)

    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock()

    async def _test():
        server._stop_event = asyncio.Event()
        server._subscribers_lock = asyncio.Lock()
        server._subscribers["trades"]["AAPL"].add(mock_ws)

        drain_task = asyncio.create_task(server._drain_queue())

        # Put trade event
        q.put({
            "channel": "trades",
            "symbol": "AAPL",
            "payload": {"price": 10050, "qty": 10},
        })

        # Sentinel to stop drain task
        q.put(None)
        await drain_task

        # Verify message was formatted and sent
        mock_ws.send.assert_awaited_once()
        sent_str = mock_ws.send.await_args[0][0]
        sent_data = json.loads(sent_str)
        assert sent_data["channel"] == "trades"
        assert sent_data["symbol"] == "AAPL"
        assert sent_data["data"] == {"price": 10050, "qty": 10}

    asyncio.run(_test())
