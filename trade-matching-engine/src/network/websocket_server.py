"""
websocket_server.py
-------------------
Asyncio + websockets server that fans out engine events to connected clients.

Routes
------
  /ws/market-data/{symbol}   – order-book top-of-book / depth updates
  /ws/trades/{symbol}        – trade execution reports

Design
------
* Runs on its own dedicated thread + event loop so it never blocks the
  matching engine's thread.
* Consumes from a thread-safe ``queue.Queue`` that the engine (or any other
  producer) pushes dicts into.  Each dict must contain at minimum:
      {
          "channel": "market-data" | "trades",
          "symbol":  str,
          "payload": <JSON-serialisable object>
      }
* Does NOT import anything from src/engine – decoupled by design.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from collections import defaultdict
from typing import Dict, Set
from urllib.parse import urlparse

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Mapping: channel -> symbol -> set of connected websockets
_Subscribers = Dict[str, Dict[str, Set[WebSocketServerProtocol]]]


# ---------------------------------------------------------------------------
# WebSocketServer
# ---------------------------------------------------------------------------

class WebSocketServer:
    """Thread-safe WebSocket fan-out server.

    Parameters
    ----------
    event_queue:
        A ``queue.Queue`` shared with the engine.  Each item must be a dict
        with keys ``channel``, ``symbol``, and ``payload``.
    host:
        Bind address (default ``"0.0.0.0"``).
    port:
        Listening port (default ``8765``).
    """

    VALID_CHANNELS = frozenset({"market-data", "trades"})

    def __init__(
        self,
        event_queue: queue.Queue,
        host: str = "0.0.0.0",
        port: int = 8765,
    ) -> None:
        self._queue: queue.Queue = event_queue
        self._host = host
        self._port = port

        # subscribers[channel][symbol] -> set of websocket connections
        self._subscribers: _Subscribers = defaultdict(lambda: defaultdict(set))
        self._subscribers_lock = asyncio.Lock()  # created on the server loop

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._started_event = threading.Event()
        self._stop_requested = threading.Event()
        self._server = None  # websockets.server.Serve handle

    # ------------------------------------------------------------------
    # Public API (called from outside the server thread)
    # ------------------------------------------------------------------

    def start(self, wait_ready: bool = True) -> None:
        """Start the server in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._run_thread,
            name="ws-server",
            daemon=True,
        )
        self._thread.start()
        if wait_ready:
            self._started_event.wait(timeout=2.0)
        logger.info("WebSocket server thread started (host=%s port=%d)", self._host, self._port)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the server to shut down and wait for the thread to exit."""
        self._stop_requested.set()
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        # Push sentinel to unblock executor thread blocked on queue.get()
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=timeout)
            logger.info("WebSocket server thread stopped")

    # ------------------------------------------------------------------
    # Internal: background thread entry point
    # ------------------------------------------------------------------

    def _run_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    # ------------------------------------------------------------------
    # Internal: async server coroutines (run on the server event loop)
    # ------------------------------------------------------------------

    async def _serve(self) -> None:
        self._stop_event = asyncio.Event()
        self._subscribers_lock = asyncio.Lock()
        if self._stop_requested.is_set():
            self._stop_event.set()

        async with websockets.serve(
            self._handle_connection,
            self._host,
            self._port,
        ) as server:
            self._server = server
            self._started_event.set()
            logger.info("WebSocket server listening on ws://%s:%d", self._host, self._port)
            # Start the queue-drain task alongside the server
            drain_task = asyncio.create_task(self._drain_queue())
            try:
                await self._stop_event.wait()
            finally:
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass
        logger.info("WebSocket server shut down cleanly")

    async def _handle_connection(
        self, websocket: WebSocketServerProtocol
    ) -> None:
        """Parse the request path, subscribe the socket, then wait for disconnect."""
        channel, symbol = self._parse_path(websocket.request.path)
        if channel is None:
            await websocket.close(code=1008, reason=f"Invalid path: {websocket.request.path}")
            return

        async with self._subscribers_lock:
            self._subscribers[channel][symbol].add(websocket)

        logger.debug("Client subscribed: channel=%s symbol=%s remote=%s",
                     channel, symbol, websocket.remote_address)
        try:
            # Keep the connection open; we don't expect inbound messages.
            await websocket.wait_closed()
        finally:
            async with self._subscribers_lock:
                self._subscribers[channel][symbol].discard(websocket)
            logger.debug("Client unsubscribed: channel=%s symbol=%s", channel, symbol)

    async def _drain_queue(self) -> None:
        """Continuously drain the thread-safe queue and broadcast to subscribers."""
        loop = asyncio.get_running_loop()
        while True:
            # Offload blocking get() to a thread-pool so the event loop stays free.
            try:
                event = await loop.run_in_executor(None, self._blocking_get)
            except asyncio.CancelledError:
                break

            if event is None:
                # Sentinel: producer signalled shutdown
                break

            channel = event.get("channel")
            symbol = event.get("symbol")
            payload = event.get("payload")

            if channel not in self.VALID_CHANNELS or not symbol:
                logger.warning("Dropping malformed event: %s", event)
                continue

            message = json.dumps({"channel": channel, "symbol": symbol, "data": payload})

            async with self._subscribers_lock:
                recipients = set(self._subscribers[channel].get(symbol, set()))

            if recipients:
                await asyncio.gather(
                    *[self._send(ws, message) for ws in recipients],
                    return_exceptions=True,
                )

    def _blocking_get(self):
        """Block until an item is available, with timeout for responsiveness."""
        try:
            return self._queue.get(timeout=0.2)
        except queue.Empty:
            return {}  # empty dict is skipped by drain loop

    @staticmethod
    async def _send(websocket: WebSocketServerProtocol, message: str) -> None:
        try:
            await websocket.send(message)
        except websockets.exceptions.ConnectionClosed:
            pass  # cleaned up by _handle_connection

    @staticmethod
    def _parse_path(path: str) -> tuple[str | None, str | None]:
        """Return (channel, symbol) for a valid path, else (None, None).

        Valid paths:
            /ws/market-data/{symbol}
            /ws/trades/{symbol}
        """
        parsed = urlparse(path)
        parts = [p for p in parsed.path.split("/") if p]
        # Expected: ["ws", "<channel>", "<symbol>"]
        if len(parts) == 3 and parts[0] == "ws" and parts[1] in ("market-data", "trades"):
            return parts[1], parts[2]
        return None, None

